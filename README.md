# SwitchBot · alertas de ofertas Switch por Telegram

Cada 6 horas busca tus juegos **por nombre** en la eShop de EE. UU. y en tiendas chilenas,
y te manda por Telegram un resumen agrupado por juego con las ofertas nuevas.

## Archivos en el repo

| Archivo | Ruta |
|---|---|
| `bot.py` | raíz |
| `juegos.json` | raíz |
| `alertas.yml` | `.github/workflows/alertas.yml` |
| `state.json` | raíz (lo crea y actualiza el bot solo) |

## Cuándo avisa

Te avisa de un juego cuando se cumple cualquiera de estas condiciones:

- **Descuento:** la tienda o la eShop lo muestra con 25 % o más de rebaja sobre su precio normal (`descuento_minimo_pct`).
- **Baja:** el precio bajó 10 % o más desde la revisión anterior (`baja_minima_pct`).
- **Objetivo:** llegó a tu precio objetivo, si le pusiste uno (`objetivo_clp` / `objetivo_usd`).

No repite alertas: vuelve a avisar solo si el precio baja aún más, o si la oferta terminó y después vuelve.
La primera revisión te manda todas las ofertas que ya estaban activas.

## Agregar juegos

```json
{ "nombre": "Metroid", "buscar": "metroid" }
```

- `buscar`: palabras que deben aparecer en el título. Puede ser una lista para nombres que se escriben
  de varias formas: `["final fantasy vii rebirth", "final fantasy 7 rebirth"]`.
- `excluir` (opcional): palabras para descartar resultados, ej. `["dlc", "pack"]`.
- `objetivo_clp` / `objetivo_usd` (opcional): tu precio objetivo.
- `eshop: false` o `fisico: false` (opcional): para revisar solo uno de los dos.
- `links` (opcional): links de producto puestos a mano, para tiendas que no se dejan buscar.

En las tiendas, el bot solo considera productos que dicen "Switch" en el título y descarta
amiibos, fundas, consolas, controles, cartas, figuras, etc.

## Agregar tiendas

```json
{ "nombre": "Mi Tienda", "url": "https://www.mitienda.cl" }
```

El bot detecta solo cómo leer la tienda (Shopify, WooCommerce, VTEX, Magento y otras).
Si una tienda no funciona, se le puede indicar su página de búsqueda con
`"busqueda": "https://www.mitienda.cl/buscar?q={q}"`.

## Diagnóstico

Para ver qué encuentra el bot en cada tienda, entra a **Actions → Alertas de ofertas Switch → Run workflow**,
elige `modo: diagnostico`, escribe una búsqueda (ej. `zelda`) y córrelo.
Abre el paso "Diagnóstico de tiendas" para ver el resultado. Esta corrida no manda nada a Telegram.

## Tiempo por revisión

Los digitales casi no suman tiempo. En las tiendas, cada búsqueda es una consulta (alrededor de 1 segundo),
así que el tiempo crece con juegos × tiendas. Con 12 juegos y 7 tiendas son unos 2 a 4 minutos.

## Limitaciones

- Algunas tiendas grandes bloquean visitas automáticas desde los servidores de GitHub. Si una tienda
  falla 5 revisiones seguidas, el bot te avisa por Telegram.
- El catálogo y los precios de la eShop vienen de servicios internos de nintendo.com, no de una API
  oficial; podrían cambiar.
- GitHub pausa los workflows programados si el repo pasa 60 días sin actividad. Como el bot guarda
  `state.json` en cada corrida, eso no debería pasar.
