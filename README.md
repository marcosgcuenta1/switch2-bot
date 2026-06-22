# 🎮 Bot cazaofertas Nintendo Switch 2

Revisa **Amazon**, **MediaMarkt**, **AliExpress** y **Chollometro** cada X minutos
buscando la Nintendo Switch 2 y te avisa por **Telegram** cuando detecta una oferta.

## Qué considera "oferta"
En las tiendas (Amazon, MediaMarkt, AliExpress) avisa cuando el precio:
- es **≤ `target_price`** (umbral que tú pones), **o**
- ha **bajado ≥ `drop_percent` %** respecto al mínimo histórico que el bot va guardando.

En **Chollometro** (agregador de chollos) avisa de **cada chollo nuevo** de la
consola que aparezca, sin esperar a un umbral.

Para no avisarte de juegos ni accesorios (que también llevan "Switch 2" en el
título), descarta todo lo que cueste menos de `ignore_below_price` (la consola
ronda los 430–600 €). Con deduplicación: no repite el mismo aviso salvo bajada mayor.

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
- **Sencillo:** deja `python bot.py` abierto en una terminal.
- **Tarea programada de Windows:** programa `python bot.py --once` cada 30 min.
- **GitHub Actions:** como el bot del Mundial, un workflow `cron` que ejecute
  `--once` (requiere `playwright install` en el runner).
