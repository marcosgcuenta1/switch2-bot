"""Bot cazaofertas Nintendo Switch 2.

Revisa cada X minutos Amazon, MediaMarkt y AliExpress buscando la Switch 2,
y avisa por Telegram cuando detecta una oferta (precio bajo el umbral o caida
respecto al minimo historico).

Uso:
    python bot.py            # bucle continuo segun poll_interval_minutes
    python bot.py --once     # una sola pasada (util para probar / cron)
    python bot.py --once --debug   # guarda el HTML de cada web en debug_*.html
"""

from __future__ import annotations

import os
import sys
import json
import time
import logging
import argparse

import yaml
import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from scrapers import SITES, scrape_site, USER_AGENT, Product

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.yaml")

# La consola de Windows es cp1252 y peta al loguear titulos con caracteres
# chinos (AliExpress). Forzamos UTF-8 con reemplazo para que nunca crashee.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")


# --------------------------------------------------------------------------- #
# Estado (minimo historico y ultimo precio avisado por producto)
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=20,
        )
        if not r.ok:
            log.error("Telegram respondio %s: %s", r.status_code, r.text)
    except Exception as e:  # noqa: BLE001
        log.error("No se pudo enviar a Telegram: %s", e)


# --------------------------------------------------------------------------- #
# Logica de filtrado y de "oferta"
# --------------------------------------------------------------------------- #

def matches_filter(p: Product, cfg: dict) -> bool:
    floor = cfg.get("ignore_below_price")
    if floor is not None and p.price < floor:
        return False
    title = " ".join(p.title.lower().split())  # normaliza espacios
    for word in cfg.get("title_must_contain", []):
        if word.lower() not in title:
            return False
    for word in cfg.get("title_must_not_contain", []):
        if word.lower() in title:
            return False
    return True


def is_offer(p: Product, state: dict, cfg: dict, deal_site: bool = False) -> tuple[bool, str]:
    """Devuelve (es_oferta, motivo). Actualiza el estado en `state`."""
    key = p.key()
    entry = state.get(key, {})

    # Webs tipo agregador (Chollometro): cada chollo NUEVO que pase el filtro
    # es de por si una oferta. Solo avisamos la primera vez que lo vemos.
    if deal_site:
        first_time = key not in state
        state[key] = {"title": p.title, "last_price": p.price, "seen": True}
        return first_time, "chollo nuevo en Chollometro"

    historic_min = entry.get("min_price")
    last_notified = entry.get("last_notified")

    target = cfg.get("target_price")
    drop_pct = cfg.get("drop_percent")

    reason = ""
    offer = False

    if target is not None and p.price <= target:
        offer = True
        reason = f"precio {p.price:.2f}€ <= objetivo {target}€"
    elif historic_min and drop_pct and p.price <= historic_min * (1 - drop_pct / 100):
        offer = True
        reason = f"baja {drop_pct}%+ desde el minimo de {historic_min:.2f}€"

    # Evitar repetir el mismo aviso si el precio no ha bajado mas
    if offer and last_notified is not None and p.price >= last_notified:
        offer = False

    # Actualizar minimo historico
    new_min = p.price if not historic_min else min(historic_min, p.price)
    state[key] = {
        "title": p.title,
        "min_price": new_min,
        "last_price": p.price,
        "last_notified": p.price if offer else last_notified,
    }

    return offer, reason


# --------------------------------------------------------------------------- #
# Pasada principal
# --------------------------------------------------------------------------- #

def run_once(cfg: dict, token: str, chat_id: str, debug: bool = False) -> None:
    state = load_state()
    query = cfg["search_query"]
    active = [s for s, on in cfg.get("sites", {}).items() if on and s in SITES]

    # En GitHub Actions las tiendas (Amazon/MediaMarkt/AliExpress) bloquean la IP
    # del runner, asi que alli limitamos a Chollometro con ONLY_SITES=chollometro.
    # En local no se define y corren todas las de config.yaml.
    only = os.getenv("ONLY_SITES")
    if only:
        wanted = {x.strip() for x in only.split(",") if x.strip()}
        active = [s for s in active if s in wanted]

    all_products: list[Product] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="es-ES",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()
        for site in active:
            products = scrape_site(page, site, query, save_debug=debug)
            all_products.extend(products)
        browser.close()

    offers_found = 0
    for p in all_products:
        if not matches_filter(p, cfg):
            continue
        deal_site = SITES.get(p.site, {}).get("deal_site", False)
        offer, reason = is_offer(p, state, cfg, deal_site=deal_site)
        if offer:
            offers_found += 1
            emoji = {"amazon": "🟠", "mediamarkt": "🔴",
                     "aliexpress": "🟡", "chollometro": "🔥"}.get(p.site, "🛒")
            msg = (
                f"{emoji} <b>¡OFERTA Switch 2!</b> ({p.site})\n\n"
                f"<b>{p.title}</b>\n"
                f"💶 <b>{p.price:.2f}€</b>\n"
                f"📉 {reason}\n\n"
                f'<a href="{p.url}">Ver oferta</a>'
            )
            log.info("OFERTA: %s | %.2f€ | %s", p.site, p.price, p.title[:60])
            send_telegram(token, chat_id, msg)

    save_state(state)
    log.info("Pasada terminada: %d productos, %d ofertas nuevas.",
             len(all_products), offers_found)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot cazaofertas Nintendo Switch 2")
    parser.add_argument("--once", action="store_true", help="una sola pasada y salir")
    parser.add_argument("--debug", action="store_true", help="guarda el HTML de cada web")
    args = parser.parse_args()

    load_dotenv(os.path.join(BASE_DIR, ".env"))
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.error("Falta TELEGRAM_TOKEN o TELEGRAM_CHAT_ID en .env "
                  "(copia .env.example a .env y rellenalo).")
        sys.exit(1)

    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    interval = cfg.get("poll_interval_minutes", 30) * 60

    if args.once:
        run_once(cfg, token, chat_id, debug=args.debug)
        return

    log.info("Arrancando bucle: revision cada %d min. Ctrl+C para parar.",
             cfg.get("poll_interval_minutes", 30))
    while True:
        try:
            run_once(cfg, token, chat_id, debug=args.debug)
        except KeyboardInterrupt:
            log.info("Parado por el usuario.")
            break
        except Exception as e:  # noqa: BLE001
            log.exception("Error en la pasada: %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
