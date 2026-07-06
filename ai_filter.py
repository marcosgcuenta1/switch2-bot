"""Capa de enriquecimiento con IA (opcional).

Toma los candidatos que ya pasaron el filtro por keywords y los manda a Gemini
Flash (gratis) en LOTES para que decida, por cada uno:

  - is_console : ¿es una consola Nintendo Switch 2 de verdad (o un pack consola
                 + juego), o es un accesorio / funda / mando / juego suelto /
                 reserva sin precio / chollo no relacionado?
  - deal_score : 0-100, lo buena que es la oferta para una Switch 2
                 (referencia de mercado ~470-510€; por debajo de 400 = excelente).
  - reason     : una frase corta en español explicando el porqué.

Todo esto es OPCIONAL: si no hay GEMINI_API_KEY, `analyse_batch` devuelve la
lista tal cual y el bot se comporta como antes. La idea (sacada de la skill
data-scraper-agent de ECC) es:

  - NUNCA una llamada por producto: se agrupan en lotes (batch_size) para no
    reventar el tier gratuito.
  - Cadena de fallback de modelos: si uno da 429 (sin cuota), salta al siguiente.
"""

from __future__ import annotations

import os
import re
import json
import time
import logging

import requests

log = logging.getLogger("ai")

# Detecta un CODIGO de cupon (se teclea en el pago): >=5 caracteres con al menos
# una letra y un numero. Ej.: BDES40, ESMYS40, BIENVENIDA60. NO casa con frases
# como "Nuevo comprador" ni "Ahorra 54€", que son descuentos YA aplicados por la web.
_CODE_RE = re.compile(r"\b(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{5,}\b", re.I)

# Descuento maximo creible de un cupon sobre una consola. Cualquier "precio
# efectivo" que implique restar mas de esto se considera un descuento FALSO
# (los "Ahorra 310€" de AliExpress son sobre un precio original inflado) y se
# descarta. Un cupon real (BDES40, nuevo comprador, etc.) ronda el 5-15%.
MAX_DISCOUNT_FRAC = 0.25

# Orden de preferencia: del mas barato/rapido al de mas calidad. Si uno se queda
# sin cuota (429) o no existe (404), se prueba el siguiente.
# NOTA: los gemini-2.0-* devuelven 429 en el tier gratis actual, asi que van al
# final. Empezamos por los "lite" 2.5, que responden y gastan menos.
MODEL_FALLBACK = [
    "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]

_last_call = 0.0


def ai_enabled() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def _generate(prompt: str, model: str, rate_limit: float) -> dict:
    """Llama a Gemini con auto-fallback de modelo. Devuelve JSON parseado o {}."""
    global _last_call

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {}

    elapsed = time.time() - _last_call
    if elapsed < rate_limit:
        time.sleep(rate_limit - elapsed)
    _last_call = time.time()

    models = [model] + [m for m in MODEL_FALLBACK if m != model] if model else MODEL_FALLBACK
    for m in models:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{m}:generateContent?key={api_key}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.2,
                "maxOutputTokens": 2048,
                # Desactiva el "pensamiento" de los modelos 2.5: para esta tarea
                # de clasificacion no aporta y se comia ~1000 tokens por llamada.
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        try:
            r = requests.post(url, json=payload, timeout=30)
            if r.status_code == 200:
                return _parse(r)
            if r.status_code in (429, 404, 503):
                log.info("[ai] %s devolvio %s, probando siguiente modelo", m, r.status_code)
                time.sleep(1)
                continue
            log.warning("[ai] %s devolvio %s: %s", m, r.status_code, r.text[:200])
            return {}
        except requests.RequestException as e:  # noqa: BLE001
            log.warning("[ai] error de red con %s: %s", m, e)
            return {}
    return {}


def _parse(resp) -> dict:
    try:
        text = (
            resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        )
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        return json.loads(text)
    except (json.JSONDecodeError, KeyError, IndexError):
        return {}


def _build_prompt(batch: list) -> str:
    items_text = "\n".join(
        f'{i + 1}. titulo="{p.title[:140]}" | precio={p.price:.2f}€ | web={p.site}'
        f' | cupon_visto="{p.context[:120] if p.context else "ninguno"}"'
        for i, p in enumerate(batch)
    )
    return f"""Eres un experto en precios de videoconsolas. La Nintendo Switch 2 es \
una consola REAL, lanzada en 2025 y a la venta en tiendas; da por hecho que existe \
y que hay stock. Clasifica cada anuncio MIRANDO SOLO SU TITULO y su cupon.

# Anuncios
{items_text}

# Criterios (clasifica por el texto, NO por si crees que existe o hay stock)
- is_console = true si el titulo describe la CONSOLA Nintendo Switch 2 (sola, modelo
  OLED/estandar, o pack consola + juego/accesorio).
- is_console = false SOLO si el titulo es claramente otra cosa: funda, carcasa,
  protector, mando, base/dock, cargador, tarjeta microSD, o un JUEGO suelto para
  Switch 2 (sin consola). Ante la duda, marca true.
- coupon: SOLO si en cupon_visto hay un CODIGO de cupon (texto alfanumerico que se
  teclea en el pago, tipo BDES40, ESMYS40, BIENVENIDA60), ponlo aqui con su importe.
  Si solo hay "Ahorra X€" o "Nuevo comprador" SIN codigo, deja coupon = "".
- effective_price: OJO con el precio de AliExpress:
    * El precio mostrado YA INCLUYE los descuentos automaticos de la web. Los badges
      "Ahorra X€" y "-X€ Nuevo comprador" YA estan reflejados en ese precio (o son
      falsos sobre un precio inflado). NUNCA los restes.
    * Solo pon un effective_price MENOR que el precio si hay un CODIGO de cupon
      adicional que aun no esta aplicado; entonces resta su importe.
    * Si no hay codigo, effective_price = precio.
- deal_score (0-100): calidad de la oferta usando el effective_price. Referencia: el
  precio normal ronda 470-510€. <400€ excelente (90+), 400-450 bueno (70-89),
  450-510 normal (40-69), por encima caro (<40). Si is_console=false, pon 0.
- reason: UNA frase corta en español (max 12 palabras).

# Salida
Devuelve SOLO este JSON, en el mismo orden que los anuncios:
{{"items": [{{"is_console": <bool>, "coupon": "<texto>", "effective_price": <numero>, \
"deal_score": <0-100>, "reason": "<texto>"}}]}}"""


def analyse_batch(products: list, batch_size: int = 5, model: str = "",
                  rate_limit: float = 7.0) -> list:
    """Enriquece los productos con ai_score y ai_reason, y descarta los que la IA
    considera que NO son consola. Sin GEMINI_API_KEY devuelve la lista intacta."""
    if not ai_enabled() or not products:
        return products

    batches = [products[i:i + batch_size] for i in range(0, len(products), batch_size)]
    log.info("[ai] %d productos -> %d llamadas (lotes de %d)",
             len(products), len(batches), batch_size)

    kept: list = []
    for n, batch in enumerate(batches, 1):
        result = _generate(_build_prompt(batch), model=model, rate_limit=rate_limit)
        analyses = result.get("items", []) if isinstance(result, dict) else []
        for j, p in enumerate(batch):
            ai = analyses[j] if j < len(analyses) else {}
            if not ai:
                # La IA fallo en este item: lo dejamos pasar sin anotar para no
                # perder ofertas por un fallo de la API.
                kept.append(p)
                continue
            if not ai.get("is_console", True):
                log.info("[ai] descartado (no es consola): %s", p.title[:60])
                continue
            try:
                p.ai_score = max(0, min(100, int(ai.get("deal_score", 0))))
            except (TypeError, ValueError):
                p.ai_score = None
            p.ai_reason = str(ai.get("reason", ""))[:120]
            # Solo restamos descuento si hay un CODIGO de cupon real (adicional, se
            # teclea en el pago). Los descuentos automaticos de AliExpress ("Ahorra",
            # "Nuevo comprador") YA estan en el precio mostrado, asi que sin codigo NO
            # tocamos el precio. Ademas el descuento debe ser creible (<= MAX_DISCOUNT_FRAC).
            code = str(ai.get("coupon", ""))
            has_code = bool(_CODE_RE.search(code))
            p.coupon = code[:120] if has_code else ""
            eff = ai.get("effective_price")
            try:
                eff = float(eff)
                discount = p.price - eff
                if has_code and 0 < discount <= p.price * MAX_DISCOUNT_FRAC:
                    p.effective_price = round(eff, 2)
            except (TypeError, ValueError):
                pass
            kept.append(p)
        log.info("[ai] lote %d/%d procesado", n, len(batches))

    log.info("[ai] %d productos tras el filtro de IA", len(kept))
    return kept
