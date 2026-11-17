"""KAP ÖDA bildirim botu.

KAP'taki Özel Durum Açıklamalarını (ÖDA) izler; yeni iş ilişkisi, sözleşme,
anlaşma, ihale, sipariş, varlık satışı gibi bildirimleri Telegram'a gönderir.
İki çalışma şekli var (ayarlar.toml -> gonderim):
    "gunluk": her gün belirlenen saatte tek bir özet mesajı
    "anlik":  her bildirim yayımlandığı anda ayrı mesaj

Kullanım:
    python bot.py               # sürekli çalışır (sunucuda servis olarak bu çalışır)
    python bot.py --tara 7      # son 7 günde nelerin yakalanacağını gösterir, göndermez
    python bot.py --onizle 1681807   # bir bildirimin Telegram mesajını ekrana basar
    python bot.py --ozet        # son 24 saatin özetini hemen Telegram'a gönderir
    python bot.py --chat-id     # Telegram chat ID'ni bulur
    python bot.py --test        # Telegram'a deneme mesajı gönderir
"""

import argparse
import logging
import os
import random
import re
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 ve öncesi
    import tomli as tomllib

from filtre import Filter
from kap import Disclosure, KapClient, TR_TZ
from kur import get_rates
from telegram_gonderici import Telegram, TelegramError, format_digest, format_message

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "ayarlar.toml"
EXAMPLE_CONFIG_PATH = ROOT / "ayarlar.example.toml"
DB_PATH = ROOT / "veri" / "kap.db"

MIN_INTERVAL = 15
MAX_SEND_ATTEMPTS = 10
MAX_DIGEST_DAYS = 3  # bot uzun süre kapalı kalırsa özet en fazla bu kadar geriye gider

log = logging.getLogger("kapbot")
stop_event = threading.Event()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        if EXAMPLE_CONFIG_PATH.exists():
            sys.exit(
                "ayarlar.toml bulunamadı. Örnek dosyayı kopyalayıp token ve chat ID'yi doldur:\n"
                "  cp ayarlar.example.toml ayarlar.toml   (Windows: copy ayarlar.example.toml ayarlar.toml)"
            )
        sys.exit("ayarlar.toml bulunamadı.")
    with open(CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)
    tg = cfg.setdefault("telegram", {})
    # Ortam değişkenleri dosyadaki değerleri ezer (token'ı dosyada tutmak istemeyenler için)
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        tg["bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        tg["chat_id"] = os.environ["TELEGRAM_CHAT_ID"].split(",")
    if isinstance(tg.get("chat_id"), (str, int)):
        tg["chat_id"] = [tg["chat_id"]]
    cfg.setdefault("kap", {})
    cfg.setdefault("filtre", {})
    return cfg


def make_telegram(cfg: dict) -> Telegram:
    tg = cfg["telegram"]
    return Telegram(tg.get("bot_token", ""), tg.get("chat_id", []), tg.get("sessiz_saatler", ""))


def send_mode(cfg: dict) -> str:
    mode = str(cfg["telegram"].get("gonderim", "anlik")).strip().lower()
    mode = {"anlık": "anlik", "günlük": "gunluk"}.get(mode, mode)
    if mode not in ("anlik", "gunluk"):
        raise ValueError(f'gonderim hatalı: {mode!r} ("gunluk" ya da "anlik" olmalı)')
    return mode


def digest_times(cfg: dict) -> list:
    """ozet_saati = "09:00" ya da ["09:00", "18:30"] -> [time(9, 0), ...]"""
    raw = cfg["telegram"].get("ozet_saati", "09:00")
    times = []
    for spec in [raw] if isinstance(raw, str) else raw:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(spec).strip())
        try:
            times.append(dtime(int(m.group(1)), int(m.group(2))))
        except (AttributeError, ValueError):
            raise ValueError(f'ozet_saati hatalı: {spec!r} (örnek: "09:00")') from None
    if not times:
        raise ValueError("ozet_saati boş olamaz")
    return sorted(times)


class Store:
    """Görülen bildirimleri tutar; aynı bildirim iki kez gönderilmez."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS bildirimler (
                idx INTEGER PRIMARY KEY,
                yayin TEXT, sirket TEXT, hisse TEXT, konu TEXT, ozet TEXT, sinif TEXT,
                eslesti INTEGER NOT NULL,
                sebep TEXT,
                gonderildi INTEGER NOT NULL DEFAULT 0,
                deneme INTEGER NOT NULL DEFAULT 0,
                eklendi TEXT
            )"""
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS durum (anahtar TEXT PRIMARY KEY, deger TEXT)")
        self.db.commit()

    def get_state(self, key: str):
        row = self.db.execute("SELECT deger FROM durum WHERE anahtar = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str):
        self.db.execute("INSERT OR REPLACE INTO durum VALUES (?, ?)", (key, value))
        self.db.commit()

    def is_empty(self) -> bool:
        return self.db.execute("SELECT 1 FROM bildirimler LIMIT 1").fetchone() is None

    def known(self, indexes) -> set:
        indexes = list(indexes)
        if not indexes:
            return set()
        marks = ",".join("?" * len(indexes))
        rows = self.db.execute(f"SELECT idx FROM bildirimler WHERE idx IN ({marks})", indexes)
        return {r[0] for r in rows}

    def add(self, d: Disclosure, matched: bool, reason: str, sent: bool):
        self.db.execute(
            "INSERT OR IGNORE INTO bildirimler VALUES (?,?,?,?,?,?,?,?,?,?,0,?)",
            (d.index, d.published, d.company, d.ticker, d.subject, d.summary,
             d.disclosure_class, int(matched), reason, int(sent),
             datetime.now(TR_TZ).isoformat(timespec="seconds")),
        )
        self.db.commit()

    def pending(self) -> list:
        rows = self.db.execute(
            "SELECT idx, yayin, sirket, hisse, konu, ozet, sinif FROM bildirimler "
            "WHERE eslesti = 1 AND gonderildi = 0 AND deneme < ? ORDER BY idx",
            (MAX_SEND_ATTEMPTS,),
        )
        return [Disclosure(*r) for r in rows]

    def mark_sent(self, index: int):
        self.db.execute("UPDATE bildirimler SET gonderildi = 1 WHERE idx = ?", (index,))
        self.db.commit()

    def mark_failed(self, index: int):
        self.db.execute("UPDATE bildirimler SET deneme = deneme + 1 WHERE idx = ?", (index,))
        self.db.commit()


def classify(kap: KapClient, flt: Filter, d: Disclosure, details: dict):
    """Bildirimin gönderilip gönderilmeyeceğine karar verir. Gerekirse metnini indirir."""
    matched, reason = flt.check(d)
    if not matched and flt.needs_body(d):
        try:
            details[d.index] = kap.detail(d.index)
            matched, reason = flt.check_text(details[d.index].text())
        except Exception as e:
            log.warning("Bildirim %s metni alınamadı: %s", d.index, e)
    return matched, reason


def poll_once(kap: KapClient, flt: Filter, tg: Telegram, store: Store, first_run: bool):
    now = datetime.now(TR_TZ)
    # Gece yarısı geçişinde son dakikalarda gelenleri kaçırmamak için 15 dk geriye bak
    items = kap.list_oda((now - timedelta(minutes=15)).date(), now.date())
    known = store.known(d.index for d in items)
    new = sorted((d for d in items if d.index not in known), key=lambda d: d.index)

    details = {}
    for d in new:
        matched, reason = classify(kap, flt, d, details)
        store.add(d, matched, reason, sent=first_run)
        if matched:
            log.info("YAKALANDI %s | %s | %s (%s)", d.ticker, d.subject, d.summary[:90], reason)

    if first_run:
        log.info("İlk çalıştırma: bugünkü %d bildirim kaydedildi (geçmiş gönderilmez). "
                 "Bundan sonra gelen yeni bildirimler gönderilecek.", len(new))
        return

    if not tg.configured:
        return
    for d in store.pending():
        detail = details.get(d.index)
        if detail is None:
            try:
                detail = kap.detail(d.index)
            except Exception as e:
                log.warning("Bildirim %s detayı alınamadı, özetle gönderiliyor: %s", d.index, e)
        try:
            tg.send(format_message(d, detail, get_rates()))
            store.mark_sent(d.index)
            log.info("Telegram'a gönderildi: %s %s", d.ticker, d.index)
        except Exception as e:
            store.mark_failed(d.index)
            log.error("Telegram gönderimi başarısız (%s): %s", d.index, e)
        time.sleep(1)


# ---------------------------------------------------------------------------
# Günlük özet
# ---------------------------------------------------------------------------

def _scheduled(now: datetime, times: list, previous: bool) -> datetime:
    """Şu andan önceki son (previous=True) ya da sonraki ilk planlı özet zamanı."""
    candidates = []
    for t in times:
        at = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if previous:
            candidates.append(at if at <= now else at - timedelta(days=1))
        else:
            candidates.append(at if at > now else at + timedelta(days=1))
    return max(candidates) if previous else min(candidates)


def collect_digest(kap: KapClient, flt: Filter, store, start: datetime, end: datetime):
    """[start, end) arasında yayımlanmış, daha önce gönderilmemiş bildirimleri sınıflandırır.
    (özete girecekler, veritabanına yazılacak tüm kayıtlar) döndürür."""
    items = [
        d for d in kap.list_oda(start.date(), end.date())
        if d.published_at is None or start <= d.published_at < end
    ]
    known = store.known(d.index for d in items) if store else set()
    new = sorted((d for d in items if d.index not in known), key=lambda d: d.index)

    details, entries, records = {}, [], []
    for d in new:
        matched, reason = classify(kap, flt, d, details)
        records.append((d, matched, reason))
        if not matched:
            continue
        if d.index not in details:
            try:
                details[d.index] = kap.detail(d.index)
            except Exception as e:
                log.warning("Bildirim %s detayı alınamadı, özetle gönderiliyor: %s", d.index, e)
            time.sleep(0.3)
        entries.append((d, details.get(d.index)))
    return entries, records


def send_digest(kap: KapClient, flt: Filter, tg: Telegram, store, start: datetime, end: datetime,
                short_subjects=()):
    entries, records = collect_digest(kap, flt, store, start, end)
    messages = format_digest(entries, start, end, get_rates(), short_subjects)
    for text in messages:
        if tg.configured:
            tg.send(text)
            time.sleep(1)
        else:
            log.info("ÖZET (Telegram ayarlanmamış):\n%s", text)
    if store:
        for d, matched, reason in records:
            store.add(d, matched, reason, sent=matched)
    log.info("Özet gönderildi: %d bildirim (%s – %s)", len(entries),
             f"{start:%d.%m %H:%M}", f"{end:%d.%m %H:%M}")


def maybe_send_digest(kap: KapClient, flt: Filter, tg: Telegram, store: Store, times: list,
                      short_subjects=()):
    now = datetime.now(TR_TZ)
    due = _scheduled(now, times, previous=True)
    last = store.get_state("son_ozet")
    if last is None:
        # İlk kurulum: ilk özet bir sonraki planlı saatte gelir ve son planlı
        # saatten (ör. bugün 09:00) itibaren yayımlananları kapsar.
        store.set_state("son_ozet", due.isoformat())
        log.info("İlk özet %s tarihinde gönderilecek (%s sonrası bildirimler).",
                 f"{_scheduled(now, times, previous=False):%d.%m %H:%M}", f"{due:%d.%m %H:%M}")
        return
    last = datetime.fromisoformat(last)
    if last >= due:
        return
    # Bot özet saatinde kapalıysa, açıldığında kaçırdığı özeti hemen gönderir.
    start = max(last, now - timedelta(days=MAX_DIGEST_DAYS))
    send_digest(kap, flt, tg, store, start, now, short_subjects)
    store.set_state("son_ozet", now.isoformat())


def run(cfg: dict):
    kap = KapClient()
    flt = Filter(cfg["filtre"])
    tg = make_telegram(cfg)
    store = Store(DB_PATH)
    interval = max(MIN_INTERVAL, int(cfg["kap"].get("kontrol_araligi_sn", 30)))
    mode = send_mode(cfg)

    if not tg.configured:
        log.warning("Telegram ayarlanmamış (ayarlar.toml). Yakalananlar sadece loga yazılacak.")
    if mode == "gunluk":
        times = digest_times(cfg)
        short_subjects = cfg["telegram"].get("ozette_kisa_konular", [])
        log.info("KAP Bot başladı (günlük özet): her gün %s'de özet gönderilecek.",
                 ", ".join(t.strftime("%H:%M") for t in times))
    else:
        log.info("KAP Bot başladı (anlık), %d sn'de bir kontrol edilecek.", interval)
        if tg.quiet_hours:
            start, end = tg.quiet_hours
            log.info("Sessiz saatler: %s-%s (mesajlar gelir, telefon ötmez).",
                     start.strftime("%H:%M"), end.strftime("%H:%M"))

    first_run = store.is_empty()
    errors = 0
    while not stop_event.is_set():
        try:
            if mode == "gunluk":
                maybe_send_digest(kap, flt, tg, store, times, short_subjects)
            else:
                poll_once(kap, flt, tg, store, first_run)
                first_run = False
            errors = 0
        except Exception as e:
            errors += 1
            log.error("KAP kontrolü başarısız (%d. kez üst üste): %s", errors, e)
        # Art arda hata olursa KAP'ı yormamak için bekleme süresini uzat (en fazla 10 dk)
        wait = interval if errors == 0 else min(600, interval * 2 ** min(errors, 5))
        stop_event.wait(wait + random.uniform(0, 3))
    log.info("KAP Bot durdu.")


# ---------------------------------------------------------------------------
# Komut satırı yardımcıları
# ---------------------------------------------------------------------------

def cmd_scan(cfg: dict, days: int):
    kap, flt = KapClient(), Filter(cfg["filtre"])
    today = datetime.now(TR_TZ).date()
    items = kap.list_oda(today - timedelta(days=days), today)
    oda = [d for d in items if d.disclosure_class == "ODA"]
    details, hits = {}, []
    for d in sorted(oda, key=lambda d: d.index):
        matched, reason = classify(kap, flt, d, details)
        if matched:
            hits.append((d, reason))
    for d, reason in hits:
        print(f"{d.published[:16]}  {d.ticker[:12]:<12} {d.subject[:28]:<28} {d.summary[:80]}  [{reason}]")
    print(f"\nSon {days} günde {len(oda)} ÖDA bildiriminden {len(hits)} tanesi yakalandı.")


def cmd_preview(cfg: dict, index: int):
    kap = KapClient()
    today = datetime.now(TR_TZ).date()
    for days in (3, 10, 30):
        d = next((x for x in kap.list_oda(today - timedelta(days=days), today) if x.index == index), None)
        if d:
            print(format_message(d, kap.detail(index), get_rates()))
            return
    sys.exit(f"{index} numaralı bildirim son 30 günün ÖDA listesinde bulunamadı.")


def cmd_chat_id(cfg: dict):
    tg = Telegram(cfg["telegram"].get("bot_token", ""), [])
    if not tg.token:
        sys.exit("Önce ayarlar.toml içine bot_token yaz.")
    updates = tg.get_updates()
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name")
    if not chats:
        print("Hiç mesaj yok. Telegram'da botuna bir mesaj yaz (ör. /start) ve tekrar çalıştır.\n"
              "Kanala göndereceksen botu kanala yönetici olarak ekle ve kanala bir mesaj at.")
    for chat_id, name in chats.items():
        print(f"chat_id = \"{chat_id}\"   ({name})")


def cmd_test(cfg: dict):
    tg = make_telegram(cfg)
    if not tg.configured:
        sys.exit("ayarlar.toml içinde bot_token ve chat_id dolu olmalı.")
    kap, flt = KapClient(), Filter(cfg["filtre"])
    today = datetime.now(TR_TZ).date()
    for d in kap.list_oda(today - timedelta(days=3), today):
        if flt.check(d)[0]:
            tg.send("🧪 <b>KAP Bot deneme mesajı</b> — son yakalanan bildirim:\n\n"
                    + format_message(d, kap.detail(d.index), get_rates()))
            print(f"Gönderildi: {d.ticker} {d.summary}")
            return
    tg.send("🧪 KAP Bot deneme mesajı: bağlantı çalışıyor.")
    print("Gönderildi.")


def cmd_digest(cfg: dict):
    """Son 24 saatin özetini hemen gönderir (Telegram ayarlı değilse ekrana basar).
    Deneme amaçlıdır; botun kayıtlarını ve bir sonraki özetin saatini etkilemez."""
    tg = make_telegram(cfg)
    end = datetime.now(TR_TZ)
    entries, _ = collect_digest(KapClient(), Filter(cfg["filtre"]), None, end - timedelta(days=1), end)
    messages = format_digest(entries, end - timedelta(days=1), end, get_rates(),
                             cfg["telegram"].get("ozette_kisa_konular", []))
    for text in messages:
        if tg.configured:
            tg.send(text)
            time.sleep(1)
        else:
            print(text + "\n")
    print(f"{len(entries)} bildirim, {len(messages)} mesaj" + (" gönderildi." if tg.configured else "."))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="KAP ÖDA bildirim botu")
    p.add_argument("--tara", type=int, metavar="GÜN", help="son N günü tara, ne yakalanacağını göster")
    p.add_argument("--onizle", type=int, metavar="NO", help="bir bildirimin mesajını ekrana bas")
    p.add_argument("--ozet", action="store_true", help="son 24 saatin özetini hemen gönder")
    p.add_argument("--chat-id", action="store_true", help="Telegram chat ID'ni bul")
    p.add_argument("--test", action="store_true", help="Telegram'a deneme mesajı gönder")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        cfg = load_config()
        # ayarları baştan doğrula (saat biçimleri vb.)
        make_telegram(cfg)
        send_mode(cfg)
        digest_times(cfg)
    except (ValueError, tomllib.TOMLDecodeError) as e:
        sys.exit(f"ayarlar.toml hatalı: {e}")

    try:
        if args.tara:
            cmd_scan(cfg, args.tara)
        elif args.onizle:
            cmd_preview(cfg, args.onizle)
        elif args.ozet:
            cmd_digest(cfg)
        elif args.chat_id:
            cmd_chat_id(cfg)
        elif args.test:
            cmd_test(cfg)
        else:
            signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
            signal.signal(signal.SIGINT, lambda *_: stop_event.set())
            run(cfg)
    except TelegramError as e:
        sys.exit(f"Telegram hatası: {e}")


if __name__ == "__main__":
    main()
