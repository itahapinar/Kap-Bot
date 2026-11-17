"""Telegram'a mesaj gönderme ve bildirimi mesaja çevirme."""

import html
import logging
import re
import time
from datetime import datetime
from datetime import time as dtime

import requests

from filtre import find_amount_items, fold
from kap import TR_TZ, Detail, Disclosure
from kur import Rates, format_tl

log = logging.getLogger(__name__)

# Mesajda gösterilmeyecek teknik alanlar (bildirim formundaki satır kodları)
_HIDDEN_FIELDS = {
    "UpdateAnnouncementFlag",
    "CorrectionAnnouncementFlag",
    "DateOfThePreviousNotificationAboutTheSameSubject",
    "DelayedAnnouncementFlag",
    "ExplanationTextBlock",
}
# Form alanlarının etiketine göre önem sırası (Türkçe karakterler sadeleştirilmiş hâliyle)
_IMPORTANT_LABELS = ("tutar", "bedel", "fiyat", "unvan", "adi", "taraf", "konusu", "sonucu",
                     "niteligi", "etkisi")
_BORING_LABELS = ("yonetim kurulu", "bagimsiz", "degerleme raporu", "iliskili taraf", "oy hak",
                  "(%)", "sermayesi", "daha once yapilan")
# Bunlar en önce gösterilir: karşı taraf, ihale sonucu, toplam tutar
_KEY_LABELS = ("toplam", "sonucu", "satin alan", "acan taraf", "ticaret unvani", "karsi taraf")
# Açıklama metninin sonundaki kalıp cümleler (mesajda yer kaplamasın)
_BOILERPLATE = ("saygiyla duyur", "bilgilerine sun", "bilgisine sun", "ingilizce cevirisi",
                "saygilarimizla", "kamuoyuna duyurulur")
# "Diğer bildirimler" satırında atlanacak yasal kalıp cümleler
_LEGAL_PHRASES = ("tebligi", "ilke karari", "ekte yer al", "sermaye piyasasi kurulu",
                  "hukmu kapsaminda", "asagidaki tablo")
# Cümle sonu: küçük harf/rakam/parantezden sonra nokta + büyük harfle başlayan cümle.
# "A.Ş. B grubu" gibi kısaltmalarda bölmez.
_SENTENCE_SPLIT = re.compile(r"(?<=[a-zçğıöşü0-9)]\.)\s+(?=[A-ZÇĞİÖŞÜ])|\n")
MAX_FIELDS = 6
MAX_EXPLANATION = 700
DIGEST_FIELDS = 3
DIGEST_EXPLANATION = 450
MAX_MESSAGE = 3800  # Telegram sınırı 4096 karakter


class TelegramError(Exception):
    pass


def parse_quiet_hours(spec: str):
    """'23:00-08:00' -> (time(23, 0), time(8, 0)). Boş ise None."""
    spec = (spec or "").strip()
    if not spec:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", spec)
    try:
        return dtime(int(m.group(1)), int(m.group(2))), dtime(int(m.group(3)), int(m.group(4)))
    except (AttributeError, ValueError):
        raise ValueError(f"sessiz_saatler hatalı: {spec!r} (örnek: \"23:00-08:00\")") from None


class Telegram:
    def __init__(self, token: str, chat_ids: list, quiet_hours: str = ""):
        self.token = token.strip()
        self.chat_ids = [str(c).strip() for c in chat_ids if str(c).strip()]
        self.quiet_hours = parse_quiet_hours(quiet_hours)
        self.session = requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_ids)

    def is_quiet_now(self) -> bool:
        if not self.quiet_hours:
            return False
        start, end = self.quiet_hours
        now = datetime.now(TR_TZ).time()
        if start <= end:
            return start <= now < end
        return now >= start or now < end  # gece yarısını geçen aralık, ör. 23:00-08:00

    def _call(self, method: str, **payload):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        for _ in range(4):
            try:
                r = self.session.post(url, json=payload, timeout=20)
            except requests.RequestException as e:
                # Hata mesajında URL (dolayısıyla token) geçebilir, loga yazmadan önce gizle.
                raise TelegramError(str(e).replace(self.token, "***")) from None
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 429:
                wait = int(data.get("parameters", {}).get("retry_after", 5))
                log.warning("Telegram hız sınırı, %d sn bekleniyor", wait)
                time.sleep(wait + 1)
                continue
            if data.get("ok"):
                return data.get("result")
            raise TelegramError(data.get("description") or f"HTTP {r.status_code}")
        raise TelegramError("Telegram hız sınırı aşılamadı")

    def send(self, text: str):
        # Sessiz saatlerde mesaj yine anında gider, sadece telefon ses/titreşim yapmaz.
        silent = self.is_quiet_now()
        for chat_id in self.chat_ids:
            try:
                self._call(
                    "sendMessage",
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    disable_notification=silent,
                )
            except TelegramError as e:
                if "parse" not in str(e).lower():
                    raise
                # HTML biçimlendirmesi reddedildiyse düz metin olarak gönder
                self._call("sendMessage", chat_id=chat_id, text=_strip_tags(text),
                           disable_web_page_preview=True, disable_notification=silent)

    def get_updates(self):
        return self._call("getUpdates") or []


def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _short(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(".,;: ") + "…"


def _pick_fields(detail: Detail, limit: int = MAX_FIELDS) -> list:
    """Formdaki en önemli `limit` alanı, formdaki sırasıyla döndürür."""
    scored = []
    for pos, (code, label, value) in enumerate(detail.fields):
        if not value or code in _HIDDEN_FIELDS or code.endswith(("Abstract", "Section")):
            continue
        key = fold(label)
        if any(w in key for w in _BORING_LABELS):
            continue
        if any(w in key for w in _KEY_LABELS):
            score = 2
        elif any(w in key for w in _IMPORTANT_LABELS):
            score = 1
        else:
            score = 0
        scored.append((-score, pos, label, value))
    best = sorted(scored)[:limit]
    return [(label, value) for _, _, label, value in sorted(best, key=lambda x: x[1])]


def _clean_explanation(text: str) -> str:
    """'Kamuoyuna saygıyla duyurulur' gibi kalıp kapanış satırlarını atar."""
    lines = []
    for line in text.split("\n"):
        key = fold(line)
        # Uzun paragrafların içinde asıl bilgi de olabileceği için sadece kısa satırları at
        if not key or (len(line) < 220 and any(b in key for b in _BOILERPLATE)):
            continue
        lines.append(line)
    return "\n".join(lines)


def _key_sentences(text: str, count: int = 2, limit: int = 340) -> str:
    """Açıklamanın yasal kalıp olmayan ilk cümleleri; hiç yoksa boş."""
    picked = []
    for sentence in _SENTENCE_SPLIT.split(_clean_explanation(text)):
        sentence = sentence.strip()
        # Kısa parçalar çoğunlukla metnin altındaki tablodan gelen tek tek sayılar
        if len(sentence) < 25 or any(p in fold(sentence) for p in _LEGAL_PHRASES):
            continue
        picked.append(sentence)
        if len(picked) >= count:
            break
    return _short(" ".join(picked), limit) if picked else ""


def _esc(s: str) -> str:
    return html.escape(s, quote=False)


def _amount_line(d: Disclosure, detail: Detail = None, rates: Rates = None) -> str:
    """'💰 40.000.000 EUR (≈ 2,21 milyar TL)'. Bildirim zaten TL karşılığını
    veriyorsa (ör. '10.200.000 USD · 502.901.820 TL') ayrıca çevirmez."""
    items = find_amount_items((detail.text() if detail else "") or d.summary)
    if not items:
        return ""
    has_tl = any(cur == "TL" for _, _, cur in items)
    parts = []
    for label, value, cur in items:
        tl = rates.to_tl(value, cur) if rates and cur != "TL" and not has_tl else None
        parts.append(f"{label} (≈ {format_tl(tl)})" if tl else label)
    return f"💰 {_esc(' · '.join(parts))}"


def format_message(d: Disclosure, detail: Detail = None, rates: Rates = None) -> str:
    lines = [f"📢 <b>{_esc(d.ticker or '-')}</b> · {_esc(d.subject)}"]
    if d.company:
        lines.append(f"<i>{_esc(d.company)}</i>")
    lines.append("")
    if d.summary and d.summary != d.subject:
        lines.append(f"📝 {_esc(d.summary)}")

    amounts = _amount_line(d, detail, rates)
    if amounts:
        lines.append(amounts)

    if detail:
        for label, value in _pick_fields(detail):
            lines.append(f"• <b>{_esc(_short(label, 70))}:</b> {_esc(_short(value, 200))}")
        explanation = _clean_explanation(detail.explanation)
        if explanation:
            lines.append("")
            lines.append(_esc(_short(explanation, MAX_EXPLANATION)))

    lines.append("")
    lines.append(f"🕒 {_esc(d.published[:16])} · <a href=\"{d.url}\">KAP'ta aç</a>")
    return "\n".join(lines)


def format_digest(entries: list, start: datetime, end: datetime, rates: Rates = None,
                  short_subjects=()) -> list:
    """Günlük özet. entries: [(Disclosure, Detail ya da None), ...]
    short_subjects başlıklarındaki bildirimler sonda tek satır olarak listelenir.
    Telegram'ın 4096 karakter sınırı yüzünden gerekirse birden fazla mesaja böler."""
    span = f"{start:%d.%m %H:%M} – {end:%d.%m %H:%M}"
    title = f"📋 <b>KAP Günlük Özet</b> · {end:%d.%m.%Y}"
    if not entries:
        return [f"{title}\n<i>{span}</i>\n\nKriterlere uyan bildirim yok."]

    short_keys = {fold(s) for s in short_subjects}
    main = [(d, det) for d, det in entries if fold(d.subject) not in short_keys]
    short = [(d, det) for d, det in entries if fold(d.subject) in short_keys]

    blocks = []
    for i, (d, detail) in enumerate(main, 1):
        when = f"{d.published[:5]} {d.published[11:16]}"  # "14.10 15:32"
        lines = [f"<b>{i}. {_esc(d.ticker or '-')}</b> · {_esc(d.subject)} · <i>{when}</i>"]
        if d.company:
            lines.append(f"<i>{_esc(d.company)}</i>")
        if d.summary and d.summary != d.subject:
            lines.append(f"📝 {_esc(d.summary)}")
        amounts = _amount_line(d, detail, rates)
        if amounts:
            lines.append(amounts)
        if detail:
            for label, value in _pick_fields(detail, DIGEST_FIELDS):
                lines.append(f"• <b>{_esc(_short(label, 60))}:</b> {_esc(_short(value, 150))}")
            explanation = _clean_explanation(detail.explanation)
            if explanation:
                lines.append(_esc(_short(explanation, DIGEST_EXPLANATION)))
        lines.append(f"<a href=\"{d.url}\">KAP'ta aç</a>")
        blocks.append("\n".join(lines))

    if short:
        lines = [f"<b>Diğer bildirimler ({len(short)})</b>"]
        for d, detail in short:
            when = f"{d.published[:5]} {d.published[11:16]}"
            # KAP başlıkları burada çoğu zaman "Pay Alım Satım Bildirimi" gibi genel;
            # asıl bilgi (kim, hangi hisse, ne kadar) metnin ilk cümlelerinde.
            text = _key_sentences(detail.explanation) if detail else ""
            money = ""
            if not text:
                text = _short(d.summary if d.summary and d.summary != d.subject else d.subject, 90)
                amounts = find_amount_items((detail.text() if detail else "") or d.summary, limit=1)
                money = f" · {_esc(amounts[0][0])}" if amounts else ""
            lines.append(f"• <b>{_esc(d.ticker or '-')}</b> · {_esc(text)}{money}"
                         f" · <i>{when}</i> · <a href=\"{d.url}\">KAP</a>")
        # Uzun listeler mesaj sınırında bölünebilsin diye 15'er satırlık parçalar
        for k in range(0, len(lines), 15):
            blocks.append("\n".join(lines[k:k + 15]))

    messages = []
    current = f"{title}\n<i>{span} · {len(entries)} bildirim</i>"
    if rates:
        current += f"\n<i>Kur: {_esc(rates.summary())}</i>"
    for block in blocks:
        if len(current) + len(block) + 2 > MAX_MESSAGE:
            messages.append(current)
            current = block
        else:
            current += "\n\n" + block
    messages.append(current)
    return messages
