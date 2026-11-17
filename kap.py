"""KAP (kap.org.tr) ile konuşan kısım: ÖDA bildirim listesi ve bildirim detayı."""

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

BASE_URL = "https://www.kap.org.tr"
LIST_URL = BASE_URL + "/tr/api/disclosure/members/byCriteria"
DETAIL_URL = BASE_URL + "/tr/Bildirim/{}"

# Türkiye 2016'dan beri yaz/kış saati uygulamıyor, sabit UTC+3.
TR_TZ = timezone(timedelta(hours=3))

# KAP liste servisi tek sorguda en fazla bu kadar kayıt döndürüyor.
LIST_LIMIT = 2000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Referer": BASE_URL + "/tr/",
}


@dataclass
class Disclosure:
    index: int
    published: str  # "14.10.2026 15:32:21"
    company: str
    ticker: str
    subject: str
    summary: str
    disclosure_class: str
    is_fund: bool = False

    @property
    def url(self) -> str:
        return DETAIL_URL.format(self.index)

    @property
    def published_at(self):
        """Yayın zamanı (Türkiye saati); okunamazsa None."""
        try:
            return datetime.strptime(self.published, "%d.%m.%Y %H:%M:%S").replace(tzinfo=TR_TZ)
        except ValueError:
            return None

    @classmethod
    def from_api(cls, item: dict) -> "Disclosure":
        return cls(
            index=int(item["disclosureIndex"]),
            published=item.get("publishDate") or "",
            company=(item.get("kapTitle") or "").strip(),
            ticker=(item.get("stockCodes") or item.get("relatedStocks") or "").strip(),
            subject=(item.get("subject") or "").strip(),
            summary=_clean(item.get("summary") or ""),
            disclosure_class=item.get("disclosureClass") or "",
            is_fund=bool(item.get("fundCode")),
        )


@dataclass
class Detail:
    fields: list  # [(alan_kodu, etiket, değer), ...] — bildirim formundaki satırlar
    explanation: str  # "Açıklamalar" bölümündeki serbest metin

    def text(self) -> str:
        """Filtre ve tutar araması için bildirimin tüm Türkçe metni."""
        parts = [f"{label}: {value}" for _, label, value in self.fields if value]
        return "\n".join(parts)


class KapClient:
    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        retry = Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=None,  # POST'u da tekrar dene
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def list_oda(self, start: date, end: date) -> list:
        """İki tarih arasındaki (dahil) ÖDA bildirimlerini, en yeni en üstte döndürür."""
        body = {
            "fromDate": start.isoformat(),
            "toDate": end.isoformat(),
            "disclosureClass": "ODA",
        }
        r = self.session.post(LIST_URL, json=body, timeout=self.timeout)
        r.raise_for_status()
        items = r.json()
        if not isinstance(items, list):
            raise RuntimeError(f"KAP beklenmeyen yanıt döndürdü: {str(items)[:200]}")
        if len(items) >= LIST_LIMIT:
            log.warning("KAP %d kayıt sınırına takıldı, tarih aralığını daralt.", LIST_LIMIT)
        return [Disclosure.from_api(x) for x in items]

    def detail(self, index: int) -> Detail:
        r = self.session.get(DETAIL_URL.format(index), timeout=self.timeout)
        r.raise_for_status()
        return parse_detail(r.text)


# ---------------------------------------------------------------------------
# Bildirim detay sayfasını ayrıştırma
#
# kap.org.tr bir Next.js sitesi; bildirim formu (HTML tablo) sayfanın içine
# self.__next_f.push([1,"..."]) parçaları hâlinde gömülü. Parçaları birleştirip
# formun HTML'ini çıkarıyoruz. Formda her satır (tr.<form>-row-N) şu
# hücrelerden oluşuyor:
#   td.taxonomy-field-name-cell   -> "oda_AlanAdi|"
#   td.taxonomy-field-title       -> Türkçe/İngilizce etiket (olmayabilir)
#   td.taxonomy-context-value...  -> değer (content-tr / content-en kopyaları)
# ---------------------------------------------------------------------------

_RSC_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_RSC_TEXT_ROW = re.compile(r"[0-9a-f]+:T([0-9a-f]+),")
_FIELD_TAG = re.compile(r">(?:oda|kap|dkb)_\w+\|<")
_FIELD_CODE = re.compile(r"(?:oda|kap|dkb)_(\w+)\|")
_FORM_ROW = re.compile(r"-row-\d+")  # ör. "oda-12000_New-Business-Relation-row-17"
_BROKEN_APOSTROPHE = re.compile(r"(?<=[A-Za-zÇĞİÖŞÜçğıöşü])\?(?=[A-Za-zÇĞİÖŞÜçğıöşü]{1,5}\b)")
_VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "col", "wbr", "source"}
_BLOCK_TAGS = {"p", "div", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6"}


class _FormParser(HTMLParser):
    """Bildirim formundaki satırları {code, label, value} olarak toplar.
    İngilizce kopyaları (content-en) atlar."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.stack = []  # açık etiketler: (etiket, gizli_mi)
        self.hidden_depth = 0
        self.mode = None  # şu an okunan hücre: "code" / "label" / "value"
        self.mode_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _VOID_TAGS:
            if tag == "br":
                self._add("\n")
            return
        cls = dict(attrs).get("class") or ""
        hidden = "content-en" in cls or tag in ("script", "style")
        self.stack.append((tag, hidden))
        if hidden:
            self.hidden_depth += 1

        if tag == "tr" and _FORM_ROW.search(cls):
            self.rows.append({"code": [], "label": [], "value": []})
            self.mode = None
        elif tag == "td" and self.rows and self.mode is None and not hidden:
            if "taxonomy-field-name-cell" in cls:
                self.mode = "code"
            elif "taxonomy-field-title" in cls:
                self.mode = "label"
            elif "taxonomy-context-value" in cls:
                self.mode = "value"
                self._add("\n")
            if self.mode:
                self.mode_depth = len(self.stack)
        elif tag in ("td", "th"):
            self._add(" ")  # metin içindeki tablolarda hücreler birbirine yapışmasın
        elif tag in _BLOCK_TAGS:
            self._add("\n")

    def handle_endtag(self, tag):
        if tag in _VOID_TAGS or not any(t == tag for t, _ in self.stack):
            return
        while self.stack:
            t, hidden = self.stack.pop()
            if hidden:
                self.hidden_depth -= 1
            if self.mode and len(self.stack) < self.mode_depth:
                self.mode = None
            if t == tag:
                break
        if tag in _BLOCK_TAGS:
            self._add("\n")

    def handle_data(self, data):
        self._add(data)

    def _add(self, text):
        if self.mode and self.hidden_depth == 0 and self.rows:
            self.rows[-1][self.mode].append(text)


def _clean(s: str) -> str:
    s = re.sub(r"[ \t\r ​]+", " ", s).strip()
    # KAP bazı metinlerde kesme işaretini "?" olarak veriyor: "Nuera?nın" -> "Nuera'nın"
    return _BROKEN_APOSTROPHE.sub("'", s)


def _clean_block(parts: list) -> str:
    lines = (_clean(line) for line in "".join(parts).split("\n"))
    return "\n".join(line for line in lines if line)


def _strip_english(value: str) -> str:
    """'Müşteri (Customer)' -> 'Müşteri', 'Hayır (No)' -> 'Hayır'."""
    return re.sub(r"\s*\((?:[A-Z][A-Za-z]*\s?)+\)$", "", value).strip()


def _form_html(page_html: str) -> str:
    chunks = _RSC_CHUNK.findall(page_html)
    data = "".join(json.loads(c) for c in chunks)
    m = _FIELD_TAG.search(data)
    if not m:
        return ""
    # Form, "30:T6f32,<html...>" şeklinde uzunluğu (UTF-8 bayt, hex) verilmiş bir
    # metin satırı içinde geliyor; tam olarak o kadarını alıyoruz.
    header = None
    for header in _RSC_TEXT_ROW.finditer(data, 0, m.start()):
        pass
    if header is None:
        return data[data.rfind("<table", 0, m.start()):]
    size = int(header.group(1), 16)
    return data[header.end():].encode("utf-8")[:size].decode("utf-8", "ignore")


def parse_detail(page_html: str) -> Detail:
    parser = _FormParser()
    parser.feed(_form_html(page_html))

    fields = []
    explanation = ""
    for row in parser.rows:
        m = _FIELD_CODE.search("".join(row["code"]))
        if not m:
            continue
        code = m.group(1)
        label = _clean_block(row["label"]).replace("\n", " ")
        value = _clean_block(row["value"])
        if not re.search(r"\w", value):  # "-", "." gibi boş geçilmiş alanlar
            value = ""
        elif code != "ExplanationTextBlock" and "\n" not in value:
            value = _strip_english(value)
        fields.append((code, label, value))
        if code == "ExplanationTextBlock":
            explanation = value
    return Detail(fields=fields, explanation=explanation)
