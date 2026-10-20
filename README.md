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
(gratis): ver `COMANDOS.md`. El Worker está en la carpeta `worker/` y Cloudflare lo despliega solo
(ver "Despliegue automático del Worker").

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

## Tu bot personal: panel de negocio y control

El canal se encarga de los digitales para jugar. Tu chat personal queda para el negocio físico
y para manejar todo:

- **Alertas al momento:** físicos (con margen frente a las otras tiendas, si se está agotando y
  si el precio viene subiendo), memorias, tus objetivos y lo que sigues. Los digitales solo si
  los sigues o tienen objetivo (`ajustes.alertas_digitales`: `seguidos` o `todas`).
- **Informe diario** (20:00, `/resumen`): mercado físico (más baratos que el resto, se están
  agotando, subiendo de precio), memorias, objetivos, estado del canal (miembros, posts,
  revisiones) y salud del bot (tiendas caídas, última revisión, evento próximo).
- **Moderador:** revisión de cada post del canal, alarmas de errores y precios sospechosos.
- **Comandos:** `/estado` (todo de un vistazo) y `/canal` (`pausa`, `seguir`, `revision on|off`,
  `revision 15`). Para ver los miembros del canal en `/estado`, agrega en Cloudflare la variable
  `CHANNEL_ID` con el `-100…` del canal.

## Memorias microSD

Además de los juegos, el bot sigue el precio de las microSD en las mismas tiendas y te avisa
igual que con un juego: mínimo histórico, descuento real contra las otras tiendas, o baja de precio.

- Las agrupa por tipo, capacidad y marca ("microSD Express 256 GB · SanDisk").
- **Express** = Switch 2 (es la única que sirve para sus juegos). **Normal** = Switch 1.
- Descarta lectores, packs, consolas con memoria incluida, vendedores externos y precios
  imposibles por GB (típico de tarjetas falsas). Las de marca desconocida se registran sin avisar.
- Se configura en `juegos.json` → `memorias` (búsquedas, descuento mínimo, `"activa": false` para apagarlo).

La primera revisión las registra en silencio; desde la segunda avisa de cambios.

En el canal, los miércoles a las 20:00 sale **💾 Memorias para tu Switch**: la microSD más barata de
cada tamaño (Express 256/512 GB para Switch 2; 128/256/512 GB para Switch 1), solo de marcas
conocidas, con la segunda mejor opción de otra marca. El diagnóstico muestra, tienda por tienda,
cuántas microSD encontró, cuáles cuentan y por qué descartó las demás.

## Accesorios (en seguimiento)

Una vez al día el bot busca accesorios de Switch y Switch 2 en las mismas tiendas: controles,
Joy-Con, estuches, bases y cargadores, micas, volantes y audífonos. Solo cuentan las marcas
conocidas (Nintendo, PowerA, HORI, 8BitDo, PDP, Turtle Beach…); los genéricos se ignoran.

- **Informe diario:** sección ACCESORIOS con las rebajas confirmadas y cuántos se siguen por tipo.
- **Alerta al momento:** si uno baja 35 % o más (`juegos.json` → `accesorios.remate_pct`), confirmado
  con su historial o con las otras tiendas. Máximo 3 por revisión.
- Todavía no sale en el canal: primero se junta historial para decidir si vale la pena.

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
| 20:00 | Memorias microSD (comparador) | mié |
| Tras cada revisión | Bombazo (máx. 2 al día, nunca entre 23:00 y 08:30) | todos |

**Nunca mudo:** si al Top, al Pack o a Físicos no les toca ninguna oferta nueva, sale un relleno
útil en su lugar (el que lleve más días sin salir): 🎯 *Los imprescindibles* (cuánto cuestan hoy
los juegos de sagas grandes y los más populares, sin repetir juegos en la semana), 💾 *Memorias*
o 📅 *Lo que viene*. Se cambia en `canal.json` → `ajustes.relleno`.

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

**Protecciones antes de publicar** (en `canal.json`):

- **Revisión previa** (`revision_previa`): cada post llega primero a tu chat con botones
  *Publicar ya*, *Saltar* y *Quitar juegos*. Si no haces nada, sale solo pasados
  `revision_minutos` (en la siguiente pasada, cada 30 min). Al publicar se vuelve a revisar
  que cada oferta siga vigente. Los botones los maneja el Worker (`worker/worker.js`).
- **Alarma de errores:** si la última revisión de precios tiene más de `datos_viejos_horas`,
  el canal se pausa y te avisa. Las tiendas que no responden y los precios sospechosos
  (descuento de 80 %+ en físico o 95 %+ en digital, bloque `sospechoso`) te llegan a tu chat
  y no se publican.
- **Bienvenida:** Actions → Canal de ofertas → `bienvenida` publica el post fijado con
  horarios, íconos y el aviso de precios y links de afiliado.

`python canal.py --sugerir` lista editoras que solo publican relleno, para agregarlas a
`editoras.basura` en `canal.json` (necesita que el bot ya haya guardado las editoras).

## Catálogo completo y lanzamientos

- **Catálogo completo:** además de buscar tus juegos, el bot recorre las búsquedas generales
  de juegos de Switch en Falabella, Lider y Paris, página por página (`CATALOGO_CONOCIDO` en
  `bot.py`; `"catalogo": false` en una tienda lo apaga). Las tiendas Shopify ya se leían completas.
- **Datos de Nintendo:** se guardan popularidad, tendencia, géneros y franquicias de cada juego.
  Un juego entre los 500 más populares suma puntos en el canal y nunca se descarta como basura.
- **Preventas:** los físicos marcados "preventa" en las tiendas te avisan una vez (de tu lista o
  ediciones especiales). El canal publica los jueves "📅 Lo que viene" con lanzamientos de la
  eShop y preventas de las próximas semanas.

## MercadoLibre (precio de reventa)

El bot consulta en MercadoLibre Chile a cuánto se vende cada juego físico, nuevo y usado,
a través del Worker (que guarda las credenciales; el repo es público y no las tiene).

1. App en developers.mercadolibre.cl con Redirect URI `https://TU-WORKER/ml/callback`.
2. En Cloudflare, secretos `ML_CLIENT_ID` y `ML_CLIENT_SECRET`.
3. Diagnóstico: la línea "MercadoLibre:" dice si funciona con acceso de app (client_credentials).
4. Solo si dice que falta autorización: crea un KV en Cloudflare, conéctalo al Worker con el
   nombre `ML` y abre una vez `https://TU-WORKER/ml/login?clave=TU_WEBHOOK_SECRET`.

Se ve en las tarjetas físicas ("🛒 MercadoLibre: nuevo ~$X · usado ~$Y", "💰 Reventa") y en el
informe diario (sección REVENTA). Hasta 40 juegos por revisión, cada uno cada 3 días.

### MercadoLibre en el canal y afiliados

En los posts de físicos, MercadoLibre aparece como una tienda más, pero solo con un vendedor
seguro (tienda oficial, o MercadoLíder Gold/Platinum con reputación verde y menos de 5 % de
calificaciones negativas) y solo publicaciones nuevas.

Los links de afiliado se configuran en `canal.json` → `afiliados`: por cada tienda, los
parámetros que se agregan al link (MercadoLibre: `matt_word` y `matt_tool`, que salen de un link
del Generador de links). Mientras falte un valor, el link sale normal. Los links con afiliado
llevan 🔗 en el canal.

## Tiendas que bloquean a GitHub (relay por el Worker)

Algunas tiendas (Paris, Weplay…) rechazan las visitas desde los servidores de GitHub. Si una
tienda chilena responde bloqueada, el bot repite la consulta a través de tu Worker de Cloudflare,
que sale por otra red. Para activarlo, en GitHub → Settings → Secrets → Actions agrega:

- `WORKER_URL`: la dirección de tu Worker (ej. `https://switchbot.tu-usuario.workers.dev`)
- `PROXY_KEY`: el mismo valor que `WEBHOOK_SECRET` en Cloudflare

Una tienda con `"via": "worker"` en `juegos.json` se lee siempre por el Worker. El Worker solo
acepta pedidos con la clave y solo a dominios `.cl` (más dominios: variable `PROXY_DOMINIOS`).

## Reloj del Worker (publicaciones a la hora)

El cron de GitHub se atrasa hasta 2–3 horas cuando GitHub está cargado, y los posts del canal
perdían su hora. El Worker tiene su propio reloj, que sí es puntual: cada 30 minutos inicia el
canal y cada hora la revisión de precios (modo `programado`). El cron de GitHub queda de respaldo.

1. Cloudflare → tu Worker → Settings → Triggers → **Cron Triggers** → Add: `5,35 * * * *`.
2. El `GITHUB_TOKEN` del Worker necesita permiso **Actions: Read and write** (además de Contents).
   Si GitHub rechaza la orden, el bot te avisa por Telegram.

## Despliegue automático del Worker

El código del Worker vive en `worker/` (`worker.js` + `wrangler.toml`). Cloudflare lo despliega
solo cada vez que cambia algo en esa carpeta; los commits del bot (precios, estado) no lo tocan.

Configuración (una vez): Cloudflare → Workers → switchbot → Settings → Builds → Connect →
repo `Baloocito/SwitchBot`, rama `main`, **Root directory** `worker`, **Build watch paths**
incluir `worker/*`. Deploy command: `npx wrangler deploy` (el que viene por defecto).

`wrangler.toml` define el KV `ML` y el reloj (`5,35 * * * *`). Las variables y secretos siguen
en el panel (`keep_vars = true`), y un despliegue nunca borra secretos.

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
