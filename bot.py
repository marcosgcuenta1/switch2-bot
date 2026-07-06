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
from datetime import date

import yaml
import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from scrapers import SITES, scrape_site, USER_AGENT, Product
import ai_filter

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
    by_site = cfg.get("ignore_below_price_by_site", {}) or {}
    if p.site in by_site:                      # suelo propio (p.ej. segunda mano)
        floor = by_site[p.site]
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


def best_price(p: Product) -> float:
    """Precio que cuenta para decidir oferta: el efectivo (tras cupon) si la IA lo
    ha calculado, si no el de lista."""
    return p.effective_price if p.effective_price else p.price


def is_offer(p: Product, state: dict, cfg: dict, deal_site: bool = False) -> tuple[bool, str]:
    """Devuelve (es_oferta, motivo). Actualiza el estado en `state`."""
    key = p.key()
    entry = state.get(key, {})
    eff = best_price(p)
    con_cupon = " (con cupón)" if p.effective_price else ""

    # Webs tipo agregador (Chollometro): avisamos de un chollo NUEVO de consola
    # solo si ademas esta por debajo del techo (target_price). Marcamos "notified"
    # para no repetir, pero si lo vimos caro y luego baja de 400, si avisamos.
    if deal_site:
        target = cfg.get("target_price")
        within = target is None or eff <= target
        already = entry.get("notified", False)
        notify = within and not already
        state[key] = {
            "title": p.title,
            "last_price": eff,
            "notified": already or notify,
        }
        return notify, f"chollo de consola por debajo de {target}€{con_cupon}"

    historic_min = entry.get("min_price")
    last_notified = entry.get("last_notified")

    target = cfg.get("target_price")
    drop_pct = cfg.get("drop_percent")

    reason = ""
    offer = False

    if target is not None and eff <= target:
        offer = True
        reason = f"precio {eff:.2f}€{con_cupon} <= objetivo {target}€"
    elif historic_min and drop_pct and eff <= historic_min * (1 - drop_pct / 100):
        offer = True
        reason = f"baja {drop_pct}%+ desde el minimo de {historic_min:.2f}€"

    # Evitar repetir el mismo aviso si el precio no ha bajado mas
    if offer and last_notified is not None and eff >= last_notified:
        offer = False

    # Actualizar minimo historico
    new_min = eff if not historic_min else min(historic_min, eff)
    state[key] = {
        "title": p.title,
        "min_price": new_min,
        "last_price": eff,
        "last_notified": eff if offer else last_notified,
    }

    return offer, reason


EMOJIS = {"amazon": "🟠", "mediamarkt": "🔴", "aliexpress": "🟡", "chollometro": "🔥"}


# --------------------------------------------------------------------------- #
# Scraping + enriquecimiento (compartido por todos los modos)
# --------------------------------------------------------------------------- #

def scrape_all(cfg: dict, debug: bool = False) -> list[Product]:
    """Scrapea todas las webs activas y devuelve la lista de productos."""
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
            all_products.extend(scrape_site(page, site, query, save_debug=debug))
        browser.close()
    return all_products


def apply_platform_codes(cfg: dict, products: list[Product]) -> None:
    """Aplica los codigos de cupon de plataforma vigentes (config) al precio efectivo.
    Estos codigos se teclean en el pago y NO estan en el precio mostrado. Se aplica el
    mejor (mayor descuento) cuyo minimo de compra cumpla el precio del producto."""
    codes_by_site = cfg.get("codigos_plataforma", {}) or {}
    today = date.today().isoformat()
    for p in products:
        aplicables = [
            c for c in codes_by_site.get(p.site, [])
            if str(c.get("caduca", "9999")) >= today
            and p.price >= float(c.get("mineur", 0))
            and float(c.get("descuento", 0)) > 0
        ]
        if not aplicables:
            continue
        best = max(aplicables, key=lambda c: float(c["descuento"]))
        cand = round(p.price - float(best["descuento"]), 2)
        # Solo si mejora el precio efectivo actual (no pisamos un descuento ya mayor).
        if cand < best_price(p):
            p.effective_price = cand
            nota = best.get("nota", "")
            p.coupon = f"código {best['codigo']} (−{float(best['descuento']):.0f}€){' · ' + nota if nota else ''}"


def enrich(cfg: dict, products: list[Product]) -> list[Product]:
    """Pre-filtra por keywords, pasa la capa de IA (si hay clave) y aplica los
    codigos de cupon de plataforma de la config."""
    candidates = [p for p in products if matches_filter(p, cfg)]
    ai_cfg = cfg.get("ai", {})
    if ai_cfg.get("enabled", True):
        candidates = ai_filter.analyse_batch(
            candidates,
            batch_size=ai_cfg.get("batch_size", 5),
            model=ai_cfg.get("model", ""),
            rate_limit=ai_cfg.get("rate_limit_seconds", 7.0),
        )
    apply_platform_codes(cfg, candidates)
    return candidates


# --------------------------------------------------------------------------- #
# Pasada principal
# --------------------------------------------------------------------------- #

def run_once(cfg: dict, token: str, chat_id: str, debug: bool = False,
             test_mode: bool = False) -> None:
    state = load_state()
    all_products = scrape_all(cfg, debug=debug)

    emojis = EMOJIS
    offers_found = 0
    per_site: dict[str, int] = {}   # solo para el modo prueba

    candidates = enrich(cfg, all_products)

    if test_mode:
        send_telegram(token, chat_id,
                      "🧪 <b>Prueba del bot Switch 2</b>\nEsto es lo que encuentro "
                      "ahora mismo (NO son ofertas reales, solo te enseño que funciona "
                      "y qué ve el bot). Máximo 2 por web.")

    for p in candidates:
        emoji = emojis.get(p.site, "🛒")
        ai_line = f"\n🤖 {p.ai_reason}" if p.ai_reason else ""
        ai_score = f" · IA {p.ai_score:.0f}/100" if p.ai_score is not None else ""
        coupon_line = f"\n🎟️ {p.coupon}" if p.coupon else ""
        if p.effective_price:
            price_line = f"💶 <s>{p.price:.2f}€</s> → <b>{p.effective_price:.2f}€</b>"
        else:
            price_line = f"💶 <b>{p.price:.2f}€</b>"

        if test_mode:
            # Manda hasta 2 por web sin tocar el estado, solo para enseñar
            if per_site.get(p.site, 0) >= 2:
                continue
            per_site[p.site] = per_site.get(p.site, 0) + 1
            offers_found += 1
            msg = (
                f"{emoji} <b>[PRUEBA] {p.site}</b>{ai_score}\n\n"
                f"<b>{p.title}</b>\n"
                f"{price_line}{coupon_line}{ai_line}\n\n"
                f'<a href="{p.url}">Ver en la web</a>'
            )
            send_telegram(token, chat_id, msg)
            continue

        deal_site = SITES.get(p.site, {}).get("deal_site", False)
        offer, reason = is_offer(p, state, cfg, deal_site=deal_site)
        if offer:
            offers_found += 1
            msg = (
                f"{emoji} <b>¡OFERTA Switch 2!</b> ({p.site}){ai_score}\n\n"
                f"<b>{p.title}</b>\n"
                f"{price_line}\n"
                f"📉 {reason}{coupon_line}{ai_line}\n\n"
                f'<a href="{p.url}">Ver oferta</a>'
            )
            log.info("OFERTA: %s | %.2f€ | %s", p.site, best_price(p), p.title[:60])
            send_telegram(token, chat_id, msg)

    if not test_mode:
        save_state(state)
    label = "enviados (prueba)" if test_mode else "ofertas nuevas"
    log.info("Pasada terminada: %d productos, %d %s.",
             len(all_products), offers_found, label)


def run_digest(cfg: dict, token: str, chat_id: str, debug: bool = False,
               top_n: int = 3) -> None:
    """Resumen semanal: mira las 4 webs y manda UN mensaje con las consolas mas
    baratas de cada una (hasta top_n por web), con precio efectivo, IA y cupon,
    marcando las ofertas. No toca el estado. Pensado para la tarea de los miercoles."""
    all_products = scrape_all(cfg, debug=debug)
    candidates = enrich(cfg, all_products)
    target = cfg.get("target_price")

    # Agrupamos por web y ordenamos cada grupo por precio efectivo (mas barato primero).
    by_site: dict[str, list[Product]] = {}
    for p in candidates:
        by_site.setdefault(p.site, []).append(p)
    for lst in by_site.values():
        lst.sort(key=best_price)

    header = "🗓️ <b>Revisión semanal Switch 2</b> (miércoles)\n"
    if not by_site:
        send_telegram(token, chat_id,
                      header + "\nNo he encontrado ninguna consola esta semana. "
                      "Puede que las webs hayan cambiado el HTML o no haya stock.")
        log.info("Digest: 0 consolas.")
        return

    lines = [header]
    shown = 0
    for site in ["amazon", "mediamarkt", "aliexpress", "chollometro"]:
        grupo = by_site.get(site)
        if not grupo:
            continue
        emoji = EMOJIS.get(site, "🛒")
        lines.append(f"\n{emoji} <b>{site}</b>")
        for p in grupo[:top_n]:
            shown += 1
            eff = best_price(p)
            flag = " ⭐ <b>OFERTA</b>" if target is not None and eff <= target else ""
            if p.effective_price:
                precio = f"<s>{p.price:.0f}€</s> → <b>{p.effective_price:.2f}€</b>"
            else:
                precio = f"<b>{p.price:.2f}€</b>"
            ai_score = f" · IA {p.ai_score:.0f}" if p.ai_score is not None else ""
            coupon = f" 🎟️ {p.coupon}" if p.coupon else ""
            lines.append(
                f'\n   <a href="{p.url}">{p.title[:48]}</a>\n'
                f"   💶 {precio}{ai_score}{flag}{coupon}"
            )

    lines.append(f"\n\n<i>Hasta {top_n} por web, más baratas primero. "
                 f"⭐ = precio (con código si lo hay) por debajo de {target}€.</i>")
    send_telegram(token, chat_id, "".join(lines))
    log.info("Digest enviado: %d consolas en %d webs.", shown, len(by_site))


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot cazaofertas Nintendo Switch 2")
    parser.add_argument("--once", action="store_true", help="una sola pasada y salir")
    parser.add_argument("--debug", action="store_true", help="guarda el HTML de cada web")
    parser.add_argument("--test", action="store_true",
                        help="envia por Telegram lo que encuentra ahora (max 2/web), "
                             "sin tocar el estado; para comprobar que funciona")
    parser.add_argument("--digest", action="store_true",
                        help="resumen semanal: 1 mensaje con la consola mas barata de "
                             "cada web; para la tarea programada de los miercoles")
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

    if args.test:
        run_once(cfg, token, chat_id, debug=args.debug, test_mode=True)
        return

    if args.digest:
        run_digest(cfg, token, chat_id, debug=args.debug)
        return

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
