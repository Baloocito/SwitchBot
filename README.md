# Alertas de ofertas Switch por Telegram

Revisa cada 6 horas los precios de tus juegos y te avisa por Telegram.

- **Digitales:** eShop de Nintendo (EE. UU. por defecto).
- **Físicos:** páginas de producto de tiendas chilenas.

## Archivos

| Archivo | Dónde va en el repo |
|---|---|
| `bot.py` | raíz |
| `juegos.json` | raíz |
| `alertas.yml` | `.github/workflows/alertas.yml` |

`state.json` lo crea el bot solo (guarda los últimos precios).

## Instalación

1. **Crea el bot:** en Telegram habla con `@BotFather`, manda `/newbot` y guarda el token.
2. **Consigue tu chat ID:** mándale cualquier mensaje a tu bot y abre
   `https://api.telegram.org/bot<TOKEN>/getUpdates`. Tu ID es el número en `"chat":{"id": ...}`.
3. **Crea un repo privado en GitHub** y sube los tres archivos en las rutas de la tabla.
4. **Agrega los secretos:** Settings → Secrets and variables → Actions:
   - `TELEGRAM_TOKEN`
   - `TELEGRAM_CHAT_ID`
5. **Prueba:** pestaña Actions → "Alertas de ofertas Switch" → Run workflow.

Para probar en tu PC:

```
pip install requests
set TELEGRAM_TOKEN=...
set TELEGRAM_CHAT_ID=...
python bot.py --test        # manda un mensaje de prueba
set DRY_RUN=1
python bot.py               # revisa precios y solo imprime las alertas
```

## Cómo agregar juegos (`juegos.json`)

### Digitales

```json
{
  "nombre": "Nombre del juego",
  "url": "https://www.nintendo.com/us/store/products/...",
  "precio_objetivo_usd": 40
}
```

- `url`: el link del juego en la tienda de Nintendo de EE. UU.
- `precio_objetivo_usd` (opcional): con objetivo, avisa solo cuando el precio llega a ese
  valor o menos. Sin objetivo, avisa con cualquier oferta.
- `nsuid` (opcional): el bot lo saca solo desde el link. Si no lo logra (lo dice en el log),
  pégalo a mano: es el número de 14 dígitos que empieza con `7001` y aparece en el código
  fuente de la página del juego.

### Físicos

```json
{
  "nombre": "Nombre del juego",
  "precio_objetivo_clp": 45000,
  "urls": ["https://tienda1.cl/...", "https://tienda2.cl/..."]
}
```

- `urls`: el link del producto en cada tienda que quieras vigilar. El bot toma el más barato con stock.
- `precio_objetivo_clp` (opcional): con objetivo, avisa al llegar a ese precio o menos.
  Sin objetivo, avisa cuando baja al menos `baja_minima_pct` (10 %) respecto de la revisión anterior.

Las entradas que todavía dicen `PEGA_AQUI` se ignoran.

## Comportamiento

- No repite la misma alerta: vuelve a avisar solo si el precio baja más, o si la oferta
  terminó y después vuelve.
- Si una tienda falla 5 revisiones seguidas, te manda un aviso para que revises el link.
- Para cambiar la frecuencia, edita la línea `cron` de `alertas.yml`.

## Limitaciones

- El precio de las tiendas se lee desde los datos estructurados (JSON-LD) de la página.
  Si una tienda no los publica o bloquea las visitas automáticas (pasa con algunos retail
  grandes desde servidores de GitHub), esa tienda no va a funcionar y el bot te avisará.
- El servicio de precios de Nintendo no es una API oficial documentada; podría cambiar.
- GitHub pausa los workflows programados si el repo pasa 60 días sin actividad.
