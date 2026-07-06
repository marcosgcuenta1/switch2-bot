# 🎮 Bot cazaofertas Nintendo Switch 2

Revisa **Amazon**, **MediaMarkt**, **AliExpress** y **Chollometro** cada X minutos
buscando la Nintendo Switch 2 y te avisa por **Telegram** cuando detecta una oferta.

## Qué considera "oferta"
Regla única: avisa solo si una Switch 2 está **por debajo de `target_price`**
(ahora 400 €). Aplica a todas las webs, incluida Chollometro. El aviso por caída
porcentual (`drop_percent`) está desactivado (`null`).

Para no avisarte de juegos ni accesorios (que también llevan "Switch 2" en el
título), descarta todo lo que cueste menos de `ignore_below_price` (la consola
ronda los 430–600 €). Con deduplicación: no repite el mismo aviso salvo bajada mayor.

## Filtro de IA y cupones (opcional, gratis)
Si defines `GEMINI_API_KEY` (gratis en https://aistudio.google.com/apikey), antes
de avisar el bot pasa los candidatos por **Gemini Flash**, que:
- **Descarta lo que no es consola** (fundas, mandos, juegos sueltos, packs raros)
  mejor que el filtro de keywords.
- **Tiene en cuenta los cupones** (sobre todo de AliExpress): calcula un *precio
  efectivo* tras el descuento y decide la oferta con él, así no se pierden chollos
  cuyo precio de lista supera el umbral pero con cupón baja. Ignora los "Ahorra X€"
  inflados de AliExpress y marca los cupones de "nuevo comprador" como tales.
- Añade una puntuación `IA 0-100` de lo buena que es la oferta.

Sin la clave, el bot funciona igual que siempre (solo keywords y precio de lista).
En GitHub Actions, añade el secret `GEMINI_API_KEY` para activarlo también ahí.

> **Nota de realismo:** Amazon es la fuente más fiable. MediaMarkt va con
> selectores a medida. AliExpress funciona pero está lleno de clones e
> importaciones grises. Si una web deja de dar resultados, lanza
> `python bot.py --once --debug` y revisa el `debug_<web>.html` para reajustar
> los selectores en `scrapers.py`.

---

## 1. Instalación (una sola vez)

```powershell
cd C:\Users\marco\switch2-bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

## 2. Crear el bot de Telegram

1. En Telegram, habla con **@BotFather** → `/newbot` → te da un **token**.
2. Escríbele algo a tu bot recién creado.
3. Abre en el navegador `https://api.telegram.org/bot<TU_TOKEN>/getUpdates`
   y copia el `chat.id`.
4. Copia `.env.example` a `.env` y rellena `TELEGRAM_TOKEN` y `TELEGRAM_CHAT_ID`.

```powershell
copy .env.example .env
notepad .env
```

## 3. Configurar umbrales

Edita `config.yaml`: `target_price`, `drop_percent`, intervalo, qué webs usar, filtros de título.

## 4. Ejecutar

```powershell
# Una pasada de prueba (recomendado la primera vez)
python bot.py --once

# Una pasada guardando el HTML de cada web (para depurar selectores)
python bot.py --once --debug

# Bucle continuo (revisa cada poll_interval_minutes)
python bot.py

# PRUEBA: envia por Telegram lo que encuentra ahora (max 2/web), sin tocar
# el estado y marcado como [PRUEBA]. Para comprobar que el envio funciona.
python bot.py --test
```

---

## ⚠️ Aviso de realismo
Estas tres webs **cambian el HTML y tienen anti-bot** (AliExpress es la más dura).
El scraper combina enlaces de producto + precio cercano, lo que aguanta cambios
pequeños, pero **tarde o temprano alguna dejará de devolver resultados** y habrá
que retocar `scrapers.py`. Si una web da 0 productos, lanza `--debug` y mira el
`debug_<web>.html` para ajustar el patrón de URL o el parseo de precio.

Alternativa más estable (de pago) para Amazon: la API de **Keepa**.

## Dejarlo corriendo siempre

### GitHub Actions (24/7, ya configurado)
El workflow `.github/workflows/cazaofertas.yml` corre cada 30 min en la nube.
**Solo vigila Chollometro** (`ONLY_SITES=chollometro`): las tiendas (Amazon,
MediaMarkt, AliExpress) bloquean la IP de datacenter de los runners, así que
desde Actions devuelven 0. Chollometro es un agregador, así que una bajada real
de la consola suele aparecer ahí igualmente. El estado se versiona en `state.json`
(el workflow lo commitea de vuelta) para no repetir avisos.

Secrets necesarios en el repo: `TELEGRAM_TOKEN` y `TELEGRAM_CHAT_ID`.

### Local (las 4 webs)
Tu IP de casa NO está bloqueada, así que en local funcionan las 4. Opciones:
- Deja `python bot.py` abierto en una terminal, o
- **Tarea Programada de Windows:** lanza `bot.py --once` cada 30 min al encender.
