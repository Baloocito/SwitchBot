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
| `history.json` | raíz (historial de precios; lo crea el bot solo) |

## Comandos de Telegram

`/precios`, `/ofertas`, `/agregar`, `/objetivo`, `/config` y más. Se instalan aparte en Cloudflare
(gratis): ver `COMANDOS.md`. El archivo `worker.js` no hace falta subirlo al repo, se pega en Cloudflare.

## Cómo decide si algo es oferta

El bot junta los avisos del mismo juego aunque cada tienda lo escriba distinto
("Metroid Prime 4 Beyond NSW", "Juego Metroid Prime 4: Beyond Nintendo Switch"…), separando
Switch de Switch 2 y las ediciones especiales. Usa el catálogo de la eShop como nombre oficial.

Para cada juego guarda el precio más bajo de cada día en `history.json`, y mide el descuento
contra un **precio de referencia real**, no contra el "antes" que publica la tienda:

1. **Su precio típico** (mediana de los últimos 90 días), cuando ya hay 7 días o más de datos.
2. Si no, **las otras tiendas** (mediana de lo que cobran las demás).
3. Solo si no hay con qué comparar, lo que dice la tienda (y la alerta lo indica).

Te avisa cuando:

- 🏆 **Mínimo histórico:** es el precio más bajo que ha visto (desde 7 días de datos).
- **Descuento real** de 25 % o más contra la referencia (`descuento_minimo_pct`, `/descuento`).
- 📉 **Baja** de 10 % o más desde la revisión anterior (`baja_minima_pct`, `/baja`).
- 🎯 **Objetivo:** llegó a tu precio objetivo.

En la eShop el precio normal es confiable, así que el descuento se mide contra ese precio.
No repite alertas: vuelve a avisar solo si el precio baja más, o si la oferta terminó y después vuelve.

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

## Memorias microSD

Además de los juegos, el bot sigue el precio de las microSD en las mismas tiendas y te avisa
igual que con un juego: mínimo histórico, descuento real contra las otras tiendas, o baja de precio.

- Las agrupa por tipo, capacidad y marca ("microSD Express 256 GB · SanDisk").
- **Express** = Switch 2 (es la única que sirve para sus juegos). **Normal** = Switch 1.
- Descarta lectores, packs, consolas con memoria incluida, vendedores externos y precios
  imposibles por GB (típico de tarjetas falsas). Las de marca desconocida se registran sin avisar.
- Se configura en `juegos.json` → `memorias` (búsquedas, descuento mínimo, `"activa": false` para apagarlo).

La primera revisión las registra en silencio; desde la segunda avisa de cambios.

## Canal público de ofertas

`canal.py` toma lo que recolecta `bot.py` y publica en un canal de Telegram solo lo que vale la pena,
a horas fijas y sin repetir. Tu chat personal sigue recibiendo todo, igual que antes.

| Hora (Chile) | Post | Días |
|---|---|---|
| 09:00 | Top 5 del día | lun–sáb |
| 13:30 | Pack temático (cambia según el día) | lun–sáb |
| 18:30 | Físicos en Chile | todos |
| 21:00 | Última llamada (terminan hoy o mañana) | todos |
| 12:00 | Top de la semana + encuesta | dom |
| Tras cada revisión | Bombazo (máx. 2 al día, nunca entre 23:00 y 08:30) | todos |

**Qué publica:** cada oferta recibe un puntaje (0–100) según si es de una saga que sigues, una "joya"
conocida o de una editora confiable, más el descuento, lo que ahorras en pesos y si es mínimo histórico.
Lo que es relleno (simuladores con año en el título, ajedrez, solitarios, juegos para niños pequeños,
descuentos inflados de 85 % o más) no entra. Todo se ajusta en `canal.json` sin tocar código.

**Configurarlo:**

1. Crea el canal en Telegram, agrega tu bot como administrador con permiso de publicar.
2. En GitHub → Settings → Secrets → Actions, agrega `TELEGRAM_CHANNEL_ID` (el `-100…` del canal).
3. Para probar sin publicar: Settings → Variables → Actions, crea `CANAL_PRUEBA` = `1`.
   Todo lo del canal te llegará a tu chat personal. Cuando te guste, borra la variable.
4. Para ver qué publicaría hoy sin mandar nada: `python canal.py --vista`.
5. Para forzar un post: Actions → Canal de ofertas → Run workflow → elige el formato.

`python canal.py --sugerir` lista editoras que solo publican relleno, para agregarlas a
`editoras.basura` en `canal.json` (necesita que el bot ya haya guardado las editoras).

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
