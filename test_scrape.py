"""Prueba de scraping aislada: ejecuta cada web e imprime lo que extrae.
No envia nada a Telegram. Guarda el HTML en debug_<web>.html.

    python test_scrape.py
"""
import sys
import logging
import yaml

# La consola de Windows es cp1252 y peta con caracteres chinos de AliExpress.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass
from playwright.sync_api import sync_playwright

from scrapers import SITES, scrape_site, USER_AGENT
from bot import matches_filter

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

with open("config.yaml", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

query = cfg["search_query"]
active = [s for s, on in cfg.get("sites", {}).items() if on and s in SITES]

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=USER_AGENT, locale="es-ES",
        viewport={"width": 1366, "height": 900},
    )
    page = context.new_page()
    for site in active:
        print("\n" + "=" * 70)
        print(f"  {site.upper()}")
        print("=" * 70)
        products = scrape_site(page, site, query, save_debug=True)
        kept = 0
        for p in products[:30]:
            ok = matches_filter(p, cfg)
            if ok:
                kept += 1
            mark = "[OK consola]" if ok else "[  descart ]"
            print(f"  {mark} {p.price:8.2f} EUR | {p.title[:60]}")
        if not products:
            print("  (0 productos -> revisa debug_%s.html)" % site)
        else:
            print(f"  --> {kept} consola(s) pasan el filtro")
    browser.close()
