"""Scrapers de cada web. Usan un navegador headless (Playwright) para esquivar
el anti-bot basico.

Cada web tiene su extractor:
  - Amazon       -> generico (enlaces /dp/ + precio cercano). Va fino.
  - MediaMarkt   -> a medida con los atributos data-test de sus tarjetas.
  - AliExpress   -> a medida: el precio viene dentro del texto del enlace.
  - Chollometro  -> a medida: agregador de chollos (article + thread-title/price).

Importante: el HTML de estas webs cambia a menudo. Si una deja de devolver
resultados, lo mas probable es que haya que retocar los selectores de aqui.
Lanza `python bot.py --once --debug` para volcar el HTML y reajustar.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup

log = logging.getLogger("scrapers")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


@dataclass
class Product:
    site: str
    title: str
    price: float          # en euros (0.0 si no se pudo leer)
    url: str
    raw_price: str = ""   # texto original del precio, para depurar

    def key(self) -> str:
        return self.url.split("?")[0]


# --------------------------------------------------------------------------- #
# Utilidades de parseo de precio
# --------------------------------------------------------------------------- #

def parse_price(text: str) -> float | None:
    """Convierte '1.299,99 €' o '$ 349.00' a float. Descarta valores absurdos."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d.,]", "", text)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value < 50 or value > 5000:
        return None
    return value


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


# Precio europeo: "469,00" o "1.299,00"; si no, un entero seguido de €.
_EU_PRICE = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})")
_INT_EURO = re.compile(r"(\d{2,4})\s*€")
# AliExpress mete el precio dentro del texto: "444 , 5 €", "483 , 98 €".
_ALI_PRICE = re.compile(r"(\d{2,4})\s*,\s*(\d{1,2})\s*€")


# --------------------------------------------------------------------------- #
# Extractores
# --------------------------------------------------------------------------- #

def extract_generic(html: str, base_url: str, site: str, url_substring: str) -> list[Product]:
    """Busca <a> cuyo href contenga `url_substring` y un precio en su contenedor."""
    soup = BeautifulSoup(html, "html.parser")
    products: list[Product] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        if url_substring not in a["href"]:
            continue
        url = urljoin(base_url, a["href"])
        clean = url.split("?")[0]
        if clean in seen:
            continue
        title = _text(a) or (a.find("img").get("alt", "") if a.find("img") else "")
        if not title:
            title = a.get("aria-label", "") or a.get("title", "")
        price, raw, node = None, "", a
        for _ in range(5):
            node = node.parent
            if node is None:
                break
            container_text = node.get_text(" ", strip=True)
            m = re.search(r"\d[\d.,]*\s*(?:€|EUR|\$)", container_text)
            if m:
                raw = m.group(0)
                price = parse_price(raw)
                if price:
                    break
        if title and price:
            seen.add(clean)
            products.append(Product(site, title.strip(), price, url, raw))
    return products


def extract_mediamarkt(html: str, base_url: str, site: str) -> list[Product]:
    soup = BeautifulSoup(html, "html.parser")
    products: list[Product] = []
    seen: set[str] = set()
    for card in soup.select('[data-test="mms-product-card"]'):
        t = card.select_one('[data-test="product-title"]')
        a = card.select_one('a[href*="/product/"]')
        p = card.select_one('[data-test="cofr-price product-price"]')
        if not (t and a and p):
            continue
        url = urljoin(base_url, a["href"])
        clean = url.split("?")[0]
        if clean in seen:
            continue
        price_text = p.get_text(" ", strip=True)
        m = _EU_PRICE.search(price_text) or _INT_EURO.search(price_text)
        price = parse_price(m.group(1)) if m else None
        title = t.get_text(" ", strip=True)
        if title and price:
            seen.add(clean)
            products.append(Product(site, title, price, url, price_text[:30]))
    return products


def extract_aliexpress(html: str, base_url: str, site: str) -> list[Product]:
    soup = BeautifulSoup(html, "html.parser")
    products: list[Product] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        if "/item/" not in a["href"]:
            continue
        url = urljoin(base_url, a["href"])
        clean = url.split("?")[0]
        if clean in seen:
            continue
        text = a.get_text(" ", strip=True)
        m = _ALI_PRICE.search(text)
        if not m:
            continue
        price = float(f"{m.group(1)}.{m.group(2)}")
        title = text[: m.start()].strip() or text[:70]
        seen.add(clean)
        products.append(Product(site, title, price, url, m.group(0)))
    return products


def extract_chollometro(html: str, base_url: str, site: str) -> list[Product]:
    soup = BeautifulSoup(html, "html.parser")
    products: list[Product] = []
    seen: set[str] = set()
    for art in soup.find_all("article"):
        a = art.select_one("a.thread-title") or art.select_one("a.cept-tt")
        if not a or not a.get("href"):
            continue
        url = urljoin(base_url, a["href"])
        clean = url.split("?")[0]
        if clean in seen:
            continue
        title = a.get_text(" ", strip=True)
        pe = art.select_one(".thread-price")
        price = parse_price(_text(pe)) if pe else None
        seen.add(clean)
        products.append(Product(site, title, price or 0.0, url, _text(pe)))
    return products


# --------------------------------------------------------------------------- #
# Definicion de cada web
# --------------------------------------------------------------------------- #

SITES = {
    "amazon": {
        "search_url": "https://www.amazon.es/s?k={q}",
        "wait": "domcontentloaded",
        "extractor": lambda h, b, s: extract_generic(h, b, s, "/dp/"),
    },
    "mediamarkt": {
        "search_url": "https://www.mediamarkt.es/es/search.html?query={q}",
        "wait": "domcontentloaded",
        "extractor": extract_mediamarkt,
    },
    "aliexpress": {
        "search_url": "https://es.aliexpress.com/w/wholesale-{q}.html",
        "wait": "networkidle",
        "extractor": extract_aliexpress,
    },
    "chollometro": {
        "search_url": "https://www.chollometro.com/search?q={q}",
        "wait": "domcontentloaded",
        "extractor": extract_chollometro,
        "deal_site": True,   # agregador: cada chollo nuevo que pase el filtro avisa
    },
}


def fetch_html(page, url: str, wait: str) -> str:
    page.goto(url, wait_until=wait, timeout=45000)
    try:
        page.wait_for_timeout(2500)
        for _ in range(3):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(1500)
    except Exception:  # noqa: BLE001
        pass
    return page.content()


def scrape_site(page, site: str, query: str, save_debug: bool = False) -> list[Product]:
    cfg = SITES[site]
    url = cfg["search_url"].format(q=query.replace(" ", "+"))
    log.info("[%s] %s", site, url)
    try:
        html = fetch_html(page, url, cfg["wait"])
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] error al cargar: %s", site, e)
        return []
    if save_debug:
        with open(f"debug_{site}.html", "w", encoding="utf-8") as f:
            f.write(html)
    products = cfg["extractor"](html, url, site)
    log.info("[%s] %d productos extraidos", site, len(products))
    return products
