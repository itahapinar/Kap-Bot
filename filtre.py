"""Hangi bildirimin gönderileceğine karar veren kısım + metinden tutar bulma."""

import re

from kap import Disclosure

GENERIC_SUBJECT = "Özel Durum Açıklaması (Genel)"

_TR_UPPER_I = str.maketrans({"I": "ı", "İ": "i"})
_ASCII = str.maketrans("çğıöşüâîû", "cgiosuaiu")


def fold(text: str) -> str:
    """Türkçe karakter ve büyük/küçük harf farkını yok eder: 'SÖZLEŞME' -> 'sozlesme'."""
    text = text.translate(_TR_UPPER_I).lower().translate(_ASCII)
    return re.sub(r"\s+", " ", text).strip()


class Filter:
    def __init__(self, cfg: dict):
        self.always = {fold(s) for s in cfg.get("her_zaman_gonder", [])}
        self.never = {fold(s) for s in cfg.get("asla_gonderme", [])}
        self.keywords = [fold(s) for s in cfg.get("anahtar_kelimeler", []) if s.strip()]
        self.excluded = [fold(s) for s in cfg.get("haric_kelimeler", []) if s.strip()]
        self.only_tickers = {s.strip().upper() for s in cfg.get("sadece_hisseler", []) if s.strip()}
        self.skip_tickers = {s.strip().upper() for s in cfg.get("haric_hisseler", []) if s.strip()}
        self.skip_companies = [fold(s) for s in cfg.get("haric_sirketler", []) if s.strip()]
        self.read_body = bool(cfg.get("ozet_bossa_metne_bak", True))

    def check(self, d: Disclosure):
        """(gönderilsin_mi, sebep) döndürür. Sadece listedeki konu ve özete bakar."""
        if d.disclosure_class != "ODA" or d.is_fund:
            return False, ""
        tickers = {t.strip().upper() for t in d.ticker.split(",") if t.strip()}
        if self.only_tickers and not (tickers & self.only_tickers):
            return False, ""
        if tickers and tickers <= self.skip_tickers:
            return False, ""
        company = fold(d.company)
        if any(w in company for w in self.skip_companies):
            return False, ""

        subject = fold(d.subject)
        if subject in self.never:
            return False, ""
        if subject in self.always:
            return True, f"konu: {d.subject}"
        return self.check_text(d.summary)

    def check_text(self, text: str):
        t = fold(text)
        if not t:
            return False, ""
        for word in self.excluded:
            if word in t:
                return False, ""
        for word in self.keywords:
            if word in t:
                return True, f"kelime: {word}"
        return False, ""

    def needs_body(self, d: Disclosure) -> bool:
        """Özeti boş/anlamsız olan 'Genel' bildirimlerde karar için metni okumak gerekir."""
        if not self.read_body or d.subject != GENERIC_SUBJECT:
            return False
        s = fold(d.summary)
        return len(s) < 12 or s == fold(d.subject) or s in ("ozel durum aciklamasi", "aciklama")


# ---------------------------------------------------------------------------
# Tutar bulma: "10.200.000-ABD Doları", "1,75 Milyar TL", "USD 5.000.000" ...
# ---------------------------------------------------------------------------

_NUM = r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?"
_SCALE = r"milyar|milyon|bin"
_CUR_WORD = (
    r"TL(?![a-z])|TRY(?![a-z])|USD(?![a-z])|EUR(?![a-z])|GBP(?![a-z])"
    r"|ABD\s+Dolar\w*|Amerikan\s+Dolar\w*|Dolar\w*|T[üu]rk\s+Lira\w*|Lira\w*"
    r"|Euro\w*|Avro\w*|Sterlin\w*|\$|€|£"
)
_CUR_CODE = r"TL|TRY|USD|EUR|GBP|\$|€|£"

_AMOUNT_AFTER = re.compile(
    rf"(?<![\d.,])(?P<num>{_NUM})\s*(?P<scale>{_SCALE})?\.?[\s\-]*(?P<cur>{_CUR_WORD})",
    re.IGNORECASE,
)
_AMOUNT_BEFORE = re.compile(
    rf"(?P<cur>{_CUR_CODE})\s?(?P<num>{_NUM})(?![\d.,]\d)(?:\s*(?P<scale>{_SCALE})(?!\w))?",
    re.IGNORECASE,
)

_SCALE_VALUE = {"milyar": 1e9, "milyon": 1e6, "bin": 1e3}


def _currency_code(cur: str) -> str:
    c = fold(cur)
    if c in ("$",) or "dolar" in c or c == "usd":
        return "USD"
    if c in ("€",) or c.startswith(("euro", "avro", "eur")):
        return "EUR"
    if c in ("£",) or c.startswith(("sterlin", "gbp")):
        return "GBP"
    return "TL"


def _to_number(num: str) -> float:
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", num):
        num = num.replace(".", "").replace(",", ".")
    else:
        num = num.replace(",", ".")
    return float(num)


def find_amounts(text: str, limit: int = 3, minimum: float = 10_000):
    """Metindeki para tutarlarını ['10.200.000 USD', '1,75 milyar TL'] gibi döndürür."""
    return [label for label, _, _ in find_amount_items(text, limit, minimum)]


def find_amount_items(text: str, limit: int = 3, minimum: float = 10_000):
    """Metindeki para tutarları: [('10.200.000 USD', 10200000.0, 'USD'), ...]"""
    # Büyük İ/I'yi tek karakterlik karşılığıyla değiştir ki IGNORECASE düzgün çalışsın
    # (uzunluk değişmediği için eşleşme konumları orijinal metinle aynı kalır).
    text = text.replace(" ", " ")
    norm = text.replace("İ", "i").replace("I", "i").replace("ı", "i")

    found = []
    for pattern in (_AMOUNT_AFTER, _AMOUNT_BEFORE):
        for m in pattern.finditer(norm):
            num = m.group("num")
            scale = (m.group("scale") or "").lower()
            try:
                value = _to_number(num) * _SCALE_VALUE.get(scale, 1)
            except ValueError:
                continue
            if value < minimum:
                continue
            cur = _currency_code(m.group("cur"))
            label = f"{num} {scale} {cur}".replace("  ", " ")
            found.append((m.start(), value, cur, label))

    found.sort()
    result, seen = [], set()
    for _, value, cur, label in found:
        if (value, cur) in seen:
            continue
        seen.add((value, cur))
        result.append((label, value, cur))
        if len(result) >= limit:
            break
    return result
