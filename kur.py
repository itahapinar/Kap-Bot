"""TCMB döviz kurları: yabancı para tutarlarının TL karşılığını göstermek için."""

import logging
import time
import xml.etree.ElementTree as ET

import requests

log = logging.getLogger(__name__)

TCMB_URL = "https://www.tcmb.gov.tr/kurlar/today.xml"
CACHE_SECONDS = 3 * 3600
CURRENCIES = ("USD", "EUR", "GBP")

_cache = {"at": 0.0, "rates": None}


class Rates:
    def __init__(self, date: str, values: dict):
        self.date = date  # "14.10.2026" — kurun ait olduğu iş günü
        self.values = values  # {"USD": 49.3041, ...} — TCMB döviz alış kuru

    def to_tl(self, amount: float, currency: str):
        rate = self.values.get(currency)
        return amount * rate if rate else None

    def summary(self) -> str:
        """'1 USD = 49,30 TL · 1 EUR = 55,35 TL (TCMB 14.10.2026)'"""
        parts = [f"1 {c} = {_tr_number(v, 2)} TL" for c, v in self.values.items() if c != "GBP"]
        return " · ".join(parts) + f" (TCMB {self.date})"


def get_rates():
    """Güncel kurlar; alınamazsa None (mesajlar kursuz gönderilir)."""
    if _cache["rates"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["rates"]
    try:
        r = requests.get(TCMB_URL, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        values = {}
        for cur in root.findall("Currency"):
            code = cur.get("CurrencyCode")
            if code in CURRENCIES:
                unit = float(cur.findtext("Unit") or 1)
                values[code] = float(cur.findtext("ForexBuying")) / unit
        if not values:
            raise ValueError("kur listesi boş")
        rates = Rates(root.get("Tarih", ""), values)
    except Exception as e:
        log.warning("TCMB kurları alınamadı, TL karşılıkları gösterilmeyecek: %s", e)
        return _cache["rates"]  # eskisi varsa onu kullan
    _cache.update(at=time.time(), rates=rates)
    return rates


def _tr_number(value: float, decimals: int) -> str:
    """49.3041 -> '49,30'; 1234567 -> '1.234.567'"""
    s = f"{value:,.{decimals}f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def format_tl(value: float) -> str:
    """Okunaklı TL: 2.214.000.000 -> '2,21 milyar TL', 41.402.000 -> '41,4 milyon TL'."""
    if value >= 1e9:
        return f"{_tr_number(value / 1e9, 2)} milyar TL"
    if value >= 1e6:
        return f"{_tr_number(value / 1e6, 1)} milyon TL"
    return f"{_tr_number(value, 0)} TL"
