/**
 * SwitchBot · comandos de Telegram (Cloudflare Worker)
 *
 * Recibe los comandos de Telegram al instante y lee/edita los archivos del
 * repo de GitHub (juegos.json y state.json). Las revisiones de precios las
 * sigue haciendo GitHub Actions.
 *
 * Variables del Worker (Settings → Variables and Secrets):
 *   TELEGRAM_TOKEN   (secreto)  token de @BotFather
 *   CHAT_ID          (texto)    tu chat ID; el bot solo te obedece a ti
 *   GITHUB_TOKEN     (secreto)  token de GitHub con permiso sobre el repo
 *   GITHUB_REPO      (texto)    usuario/repo, ej. Baloocito/SwitchBot
 *   WEBHOOK_SECRET   (secreto)  clave inventada por ti (letras, números, _ o -)
 */

const GH = "https://api.github.com";
const WORKFLOW = "alertas.yml";
const VERSION = "2026-10-25 memoria-audio";
const TG_MAX = 3900;

const SEP = "━━━━━━━━━━━━━━";

function marco(titulo, sub, cuerpo, pie = []) {
  const l = [titulo];
  if (sub) l.push(`<i>${sub}</i>`);
  l.push(SEP, ...cuerpo);
  if (pie.length) l.push(SEP, ...pie);
  return l.join("\n");
}

function guardado(cuerpo, pie = ["Se aplica desde la próxima revisión · /revisar para hacerla ahora"]) {
  return marco("✅ <b>CAMBIO GUARDADO</b>", null, Array.isArray(cuerpo) ? cuerpo : [cuerpo], pie);
}

function uso(comando, ejemplos) {
  return marco(`ℹ️ <b>CÓMO USAR ${comando.toUpperCase()}</b>`, null, ejemplos);
}

const AYUDA = `📖 <b>MENÚ DE SWITCHBOT</b>
<i>Tu cazador de ofertas de Nintendo Switch</i>
${"━━━━━━━━━━━━━━"}

<b>Panel</b>
/estado — cómo están el bot, las tiendas y el canal
/metricas — suscriptores, clics por juego/tienda/post y comisiones

<b>Otros productos</b>
/memoria — microSD más baratas (/memoria express · /memoria 256)
/audio — audífonos gamer (/audio hyperx · /audio zeus)
/consolas — Switch 2, OLED, Lite y packs
/seguimiento — tus seguimientos personales
/comision 5000 mercadolibre — anotar una comisión (sin número: ver el mes)
/canal — estado del canal y controles (pausa, seguir, revisión on/off)
/resumen — informe diario de negocio (hora, ahora, off)

<b>Precios</b>
/precios — todas las sagas, primero las que tienen oportunidades
/precios <i>saga o juego</i> — tarjetas completas para decidir
/ofertas — todo lo que hoy tiene descuento
/ofertas compra · /ofertas fisico · /ofertas digital · /ofertas todo
/ofertas <i>%</i> — solo descuentos de ese % o más
/historial <i>juego</i> — mínimo, típico y evolución del precio
/revisar — revisar precios ahora (tarda 2–4 min)

<b>Agenda</b>
/evento — modo evento: Black Friday y otros (on, off)

<b>Seguimiento</b>
/seguidos — tus objetivos ⭐ seguidos y 🔕 silenciados
⚙️ En cada tarjeta: objetivo, seguir o silenciar con un toque

<b>Juegos</b>
/agregar <i>nombre</i> — ej. /agregar Kirby
/agregar <i>nombre</i> | <i>búsqueda 1</i>, <i>búsqueda 2</i>
/quitar <i>nombre</i>
/objetivo <i>juego precio</i> — ej. /objetivo Zelda 30000
/objetivo <i>juego precio</i> usd — ej. /objetivo Zelda 29.99 usd
/objetivo <i>juego</i> quitar

<b>Tiendas</b>
/tienda <i>nombre url</i> — ej. /tienda Zmart https://www.zmart.cl
/quitartienda <i>nombre</i>

<b>Ajustes</b>
/config — ver la configuración
/descuento <i>%</i> — descuento mínimo para avisar
/baja <i>%</i> — baja mínima entre revisiones
/externos si|no — incluir vendedores externos de marketplaces
${"━━━━━━━━━━━━━━"}
🏆 mínimo histórico · 🎯 tu objetivo · 🏪 vendedor externo · S2 = Switch 2`;

const COMANDOS_MENU = [
  ["estado", "Estado del bot, tiendas y canal"],
  ["metricas", "Suscriptores, clics y comisiones"],
  ["memoria", "Precios de microSD (Switch 1 y 2)"],
  ["audio", "Precios de audífonos gamer"],
  ["consolas", "Precios de consolas Nintendo"],
  ["seguimiento", "Tus seguimientos (ej. Redragon Zeus)"],
  ["comision", "Anotar una comisión de afiliado"],
  ["canal", "Canal: estado, pausa, revisión"],
  ["precios", "Precios actuales (opcional: juego)"],
  ["ofertas", "Juegos con descuento hoy"],
  ["historial", "Historial de precios de un juego"],
  ["seguidos", "Tus objetivos, seguidos y silenciados"],
  ["resumen", "Informe diario de negocio"],
  ["evento", "Modo evento (Black Friday)"],
  ["revisar", "Revisar precios ahora"],
  ["config", "Ver la configuración"],
  ["agregar", "Agregar un juego"],
  ["quitar", "Quitar un juego"],
  ["objetivo", "Precio objetivo de un juego"],
  ["descuento", "Descuento mínimo para avisar"],
  ["baja", "Baja mínima entre revisiones"],
  ["tienda", "Agregar una tienda"],
  ["quitartienda", "Quitar una tienda"],
  ["ayuda", "Menú de comandos"],
];

// ------------------------------------------------------------------ entrada

export default {
  // Reloj de Cloudflare (Settings → Triggers → Cron Triggers: "5,35 * * * *").
  // El cron de GitHub se atrasa horas cuando hay mucha carga; este llega a la hora.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(reloj(env, new Date(event.scheduledTime)));
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/r") return redirigir(request, env, url, ctx);
    if (request.method === "GET" && url.pathname === "/metricas/datos") {
      if (!env.WEBHOOK_SECRET || request.headers.get("X-Proxy-Key") !== env.WEBHOOK_SECRET) {
        return new Response("No autorizado.", { status: 403 });
      }
      const datos = await metricas(env, Number(url.searchParams.get("dias") || 7));
      return new Response(JSON.stringify(datos), { headers: { "content-type": "application/json" } });
    }

    if (request.method === "GET" && url.pathname === "/proxy") return proxy(request, env, url);
    if (request.method === "GET" && url.pathname.startsWith("/ml/")) return rutaML(request, env, url);
    if (request.method === "GET" && url.pathname === "/setup") {
      if (!env.WEBHOOK_SECRET || url.searchParams.get("clave") !== env.WEBHOOK_SECRET) {
        return new Response("Clave incorrecta.", { status: 403 });
      }
      return new Response(await setup(env, url.origin), {
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }
    if (request.method !== "POST") return new Response(`SwitchBot activo · versión ${VERSION}`);

    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("No autorizado.", { status: 403 });
    }
    const update = await request.json().catch(() => null);
    if (update && update.callback_query) {
      const cq = update.callback_query;
      const chat = String(cq.message && cq.message.chat && cq.message.chat.id);
      if (chat !== String(env.CHAT_ID).trim()) return new Response("ok");
      try {
        await manejarBoton(env, cq);
      } catch (e) {
        await tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: "⚠️ " + (e.message || e).toString().slice(0, 180), show_alert: true });
      }
      return new Response("ok");
    }
    const msg = update && (update.message || update.edited_message);
    if (!msg || typeof msg.text !== "string") return new Response("ok");

    const chatId = String(msg.chat.id);
    if (chatId !== String(env.CHAT_ID).trim()) {
      await send(env, chatId, `Este bot es privado.\nTu chat ID es <code>${chatId}</code>.`);
      return new Response("ok");
    }
    try {
      const ref = msg.reply_to_message && /ref:([0-9a-f]{10})/.exec(msg.reply_to_message.text || "");
      if (ref) return (await objetivoEscrito(env, chatId, ref[1], msg.text.trim())), new Response("ok");
      await manejar(env, chatId, msg.text.trim());
    } catch (e) {
      await send(env, chatId, marco("⚠️ <b>NO SE PUDO</b>", null, [esc(e.message || String(e))]));
    }
    return new Response("ok");
  },
};

// ------------------------------------------------------------------ relay de tiendas
// bot.py pide aqui las paginas de tiendas que bloquean a los servidores de GitHub.
// Solo responde con la clave (PROXY_KEY = WEBHOOK_SECRET) y solo a tiendas .cl.

const PROXY_HEADERS = {
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36",
  "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
  "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
};

async function proxy(request, env, url) {
  if (!env.WEBHOOK_SECRET || request.headers.get("X-Proxy-Key") !== env.WEBHOOK_SECRET) {
    return new Response("No autorizado.", { status: 403 });
  }
  let destino;
  try { destino = new URL(url.searchParams.get("u") || ""); } catch { return new Response("URL inválida", { status: 400 }); }
  const extra = String(env.PROXY_DOMINIOS || "").split(",").map((x) => x.trim()).filter(Boolean);
  const host = destino.hostname.toLowerCase();
  if (destino.protocol !== "https:" || !(host.endsWith(".cl") || extra.some((d) => host === d || host.endsWith("." + d)))) {
    return new Response("Dominio no permitido", { status: 403 });
  }
  const r = await fetch(destino.toString(), { headers: PROXY_HEADERS, redirect: "follow", cf: { cacheTtl: 0 } });
  return new Response(r.body, {
    status: r.status,
    headers: { "content-type": r.headers.get("content-type") || "text/html; charset=utf-8" },
  });
}

// ------------------------------------------------------------------ MercadoLibre
// Secretos en Cloudflare: ML_CLIENT_ID y ML_CLIENT_SECRET. Opcional: un KV llamado ML
// (necesario solo si MercadoLibre no acepta client_credentials para buscar).
// Rutas:
//   /ml/buscar?q=...     (con X-Proxy-Key) resultados de busqueda en MercadoLibre Chile
//   /ml/estado           (con X-Proxy-Key) que acceso funciona
//   /ml/login?clave=...  autorizar una vez con tu cuenta (si client_credentials no alcanza)
//   /ml/callback         donde vuelve MercadoLibre tras autorizar

const ML_API = "https://api.mercadolibre.com";
let ML_CACHE = null;   // { token, vence, via } en memoria del Worker

async function mlTokenPost(env, cuerpo) {
  const r = await fetch(`${ML_API}/oauth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
    body: new URLSearchParams({ client_id: env.ML_CLIENT_ID, client_secret: env.ML_CLIENT_SECRET, ...cuerpo }),
  });
  const j = await r.json().catch(() => ({}));
  return { ok: r.ok && !!j.access_token, j, status: r.status };
}

async function mlToken(env, forzarUsuario = false) {
  if (!env.ML_CLIENT_ID || !env.ML_CLIENT_SECRET) throw new Error("Faltan ML_CLIENT_ID y ML_CLIENT_SECRET en Cloudflare");
  const ahora = Date.now();
  if (ML_CACHE && ML_CACHE.vence > ahora + 60000 && (!forzarUsuario || ML_CACHE.via === "usuario")) return ML_CACHE;
  if (env.ML && !forzarUsuario) {
    const guardado = await env.ML.get("access", "json");
    if (guardado && guardado.vence > ahora + 60000) return (ML_CACHE = guardado);
  }
  const guardar = async (j, via) => {
    ML_CACHE = { token: j.access_token, vence: ahora + (j.expires_in || 21600) * 1000, via };
    if (env.ML) await env.ML.put("access", JSON.stringify(ML_CACHE));
    return ML_CACHE;
  };
  if (!forzarUsuario) {
    const cc = await mlTokenPost(env, { grant_type: "client_credentials" });
    if (cc.ok) return guardar(cc.j, "app");
  }
  const refresh = env.ML && await env.ML.get("refresh");
  if (!refresh) throw new Error("MercadoLibre no dio acceso de app y no hay autorización de usuario: abre /ml/login");
  const rt = await mlTokenPost(env, { grant_type: "refresh_token", refresh_token: refresh });
  if (!rt.ok) throw new Error(`No pude renovar el acceso de MercadoLibre (${rt.status}): vuelve a abrir /ml/login`);
  if (rt.j.refresh_token) await env.ML.put("refresh", rt.j.refresh_token);   // es de un solo uso
  return guardar(rt.j, "usuario");
}

async function mlGet(env, path) {
  const tok = await mlToken(env, !!(env.ML && await env.ML.get("refresh")));
  const r = await fetch(ML_API + path, { headers: { Authorization: `Bearer ${tok.token}`, Accept: "application/json" } });
  return { status: r.status, j: await r.json().catch(() => ({})) };
}

// Plan B: buscador de catalogo (productos) + publicaciones de cada producto.
// MercadoLibre cerro /sites/{site}/search a muchas apps; el catalogo suele seguir abierto.
async function mlBuscarCatalogo(env, q) {
  const ps = await mlGet(env, `/products/search?status=active&site_id=MLC&q=${encodeURIComponent(q)}&limit=12`);
  if (ps.status !== 200) return { ok: false, status: ps.status, via: "catalogo", error: ps.j.message || ps.j.error || "error" };
  const items = [];
  for (const p of (ps.j.results || []).slice(0, 8)) {
    const it = await mlGet(env, `/products/${p.id}/items?limit=30`);
    if (it.status === 200 && Array.isArray(it.j.results) && it.j.results.length) {
      for (const x of it.j.results) {
        items.push({ titulo: p.name, precio: x.price, moneda: x.currency_id || "CLP", condicion: x.condition || "new",
          url: x.item_id ? `https://articulo.mercadolibre.cl/${String(x.item_id).replace(/^MLC-?/, "MLC-")}`
            : `https://www.mercadolibre.cl/p/${p.id}`,
          vendidos: x.sold_quantity ?? null, seller_id: x.seller_id || null,
          oficial: !!x.official_store_id, vendedor: null, envio_gratis: !!(x.shipping && x.shipping.free_shipping) });
      }
    } else {
      const det = await mlGet(env, `/products/${p.id}`);
      const bb = det.j && det.j.buy_box_winner;
      if (bb && bb.price) {
        items.push({ titulo: p.name, precio: bb.price, moneda: bb.currency_id || "CLP", condicion: bb.condition || "new",
          url: det.j.permalink || `https://www.mercadolibre.cl/p/${p.id}`, vendidos: null, oficial: !!bb.official_store_id,
          vendedor: null, envio_gratis: !!(bb.shipping && bb.shipping.free_shipping) });
      }
    }
  }
  await marcarSeguros(env, items);
  return { ok: true, via: "catalogo", total: items.length, items };
}

// Vendedor seguro para recomendar en el canal: tienda oficial, o MercadoLíder Gold/Platinum
// con reputación verde. Se revisan solo los nuevos más baratos (una consulta por vendedor).
async function marcarSeguros(env, items, maxVendedores = 12) {
  const cache = {};
  const nuevos = items.filter((x) => x.condicion !== "used").sort((a, b) => a.precio - b.precio);
  for (const x of nuevos) {
    if (x.oficial) { x.seguro = true; x.nivel = "tienda oficial"; }
    if (!x.seller_id) continue;
    if (!(x.seller_id in cache)) {
      if (Object.keys(cache).length >= maxVendedores) continue;
      const u = await mlGet(env, `/users/${x.seller_id}`).catch(() => ({ status: 0, j: {} }));
      cache[x.seller_id] = u.status === 200 ? u.j : null;
    }
    const u = cache[x.seller_id];
    if (!u) continue;
    const rep = u.seller_reputation || {};
    const lider = ["platinum", "gold"].includes(rep.power_seller_status);
    const negativas = ((rep.transactions || {}).ratings || {}).negative ?? 1;
    x.vendedor = u.nickname || null;
    if (x.oficial) continue;
    if (lider && rep.level_id === "5_green" && negativas <= 0.05) {
      x.seguro = true;
      x.nivel = `MercadoLíder ${rep.power_seller_status === "platinum" ? "Platinum" : "Gold"}`;
    }
  }
}

async function mlBuscar(env, q, offset = 0) {
  const url = `${ML_API}/sites/MLC/search?q=${encodeURIComponent(q)}&limit=50&offset=${offset}`;
  const pedir = (tok) => fetch(url, { headers: { Authorization: `Bearer ${tok.token}`, Accept: "application/json" } });
  let tok = await mlToken(env);
  let r = await pedir(tok);
  if ((r.status === 401 || r.status === 403) && tok.via === "app" && env.ML && await env.ML.get("refresh")) {
    tok = await mlToken(env, true);         // el acceso de app no alcanza para buscar: se usa el de usuario
    r = await pedir(tok);
  }
  const j = await r.json().catch(() => ({}));
  if (r.status === 403) return mlBuscarCatalogo(env, q);
  if (!r.ok) return { ok: false, status: r.status, via: tok.via, error: j.message || j.error || "error" };
  const items = (j.results || []).map((x) => ({
    titulo: x.title, precio: x.price, moneda: x.currency_id, condicion: x.condition, url: x.permalink,
    vendidos: x.sold_quantity ?? null, oficial: !!x.official_store_id, vendedor: x.seller && x.seller.nickname || null,
    seller_id: x.seller && x.seller.id || null, envio_gratis: !!(x.shipping && x.shipping.free_shipping),
  }));
  await marcarSeguros(env, items);
  return { ok: true, via: tok.via, total: (j.paging && j.paging.total) || items.length, items };
}

async function rutaML(request, env, url) {
  const json = (o, status = 200) => new Response(JSON.stringify(o), { status, headers: { "content-type": "application/json" } });
  const autorizado = env.WEBHOOK_SECRET && request.headers.get("X-Proxy-Key") === env.WEBHOOK_SECRET;
  const callback = `${url.origin}/ml/callback`;
  try {
    if (url.pathname === "/ml/buscar") {
      if (!autorizado) return json({ ok: false, error: "No autorizado" }, 403);
      return json(await mlBuscar(env, url.searchParams.get("q") || "", Number(url.searchParams.get("offset") || 0)));
    }
    if (url.pathname === "/ml/estado") {
      if (!autorizado) return json({ ok: false, error: "No autorizado" }, 403);
      const cc = env.ML_CLIENT_ID ? await mlTokenPost(env, { grant_type: "client_credentials" }) : { ok: false, status: 0 };
      const prueba = await mlBuscar(env, "zelda tears of the kingdom nintendo switch").catch((e) => ({ ok: false, error: e.message }));
      const rutas = {};
      for (const [k, path] of [["busqueda_publica", "/sites/MLC/search?q=zelda&limit=1"],
        ["catalogo", "/products/search?status=active&site_id=MLC&q=zelda&limit=1"]]) {
        rutas[k] = await mlGet(env, path).then((x) => x.status).catch((e) => e.message);
      }
      return json({ client_credentials: cc.ok ? "ok" : `no (${cc.status})`, kv: !!env.ML,
        autorizacion_usuario: !!(env.ML && await env.ML.get("refresh")), rutas,
        busqueda: prueba.ok ? `ok vía ${prueba.via}, ${prueba.items.length} resultados` : prueba });
    }
    if (url.pathname === "/ml/login") {
      if (!env.WEBHOOK_SECRET || url.searchParams.get("clave") !== env.WEBHOOK_SECRET) return new Response("Clave incorrecta.", { status: 403 });
      if (!env.ML) return new Response("Primero crea un KV en Cloudflare y conéctalo a este Worker con el nombre ML.", { status: 400 });
      const estado = crypto.randomUUID();
      await env.ML.put("estado_login", estado, { expirationTtl: 900 });
      const auth = `https://auth.mercadolibre.cl/authorization?response_type=code&client_id=${env.ML_CLIENT_ID}` +
        `&redirect_uri=${encodeURIComponent(callback)}&state=${estado}`;
      return Response.redirect(auth, 302);
    }
    if (url.pathname === "/ml/callback") {
      const code = url.searchParams.get("code");
      if (!code || !env.ML) return new Response("Falta el código o el KV ML.", { status: 400 });
      const esperado = await env.ML.get("estado_login");
      if (!esperado || esperado !== url.searchParams.get("state")) return new Response("Autorización vencida: vuelve a abrir /ml/login.", { status: 400 });
      const t = await mlTokenPost(env, { grant_type: "authorization_code", code, redirect_uri: callback });
      if (!t.ok) return new Response(`MercadoLibre rechazó el código (${t.status}).`, { status: 400 });
      await env.ML.put("refresh", t.j.refresh_token);
      await env.ML.delete("estado_login");
      ML_CACHE = { token: t.j.access_token, vence: Date.now() + (t.j.expires_in || 21600) * 1000, via: "usuario" };
      await env.ML.put("access", JSON.stringify(ML_CACHE));
      return new Response("✅ SwitchBot quedó conectado a MercadoLibre. Ya puedes cerrar esta página.",
        { headers: { "content-type": "text/plain; charset=utf-8" } });
    }
    return json({ ok: false, error: "Ruta desconocida" }, 404);
  } catch (e) {
    return json({ ok: false, error: e.message || String(e) }, 500);
  }
}

async function setup(env, origin) {
  const faltan = ["TELEGRAM_TOKEN", "CHAT_ID", "GITHUB_TOKEN", "GITHUB_REPO", "WEBHOOK_SECRET"]
    .filter((k) => !env[k]);
  if (faltan.length) return "Faltan estas variables en el Worker: " + faltan.join(", ");
  if (!/^[A-Za-z0-9_-]{8,256}$/.test(env.WEBHOOK_SECRET)) {
    return "WEBHOOK_SECRET debe tener al menos 8 caracteres y solo letras, números, _ o -.";
  }
  const lineas = [];
  // diagnostico: como estaba el webhook antes de reconfigurarlo
  const info = await tg(env, "getWebhookInfo", {});
  if (info.ok) {
    const w = info.result || {};
    lineas.push("Antes: webhook " + (w.url ? (w.url === origin + "/" ? "apuntaba a este Worker" : "apuntaba a OTRA dirección: " + w.url) : "NO estaba configurado"));
    lineas.push("Antes: mensajes en espera " + (w.pending_update_count || 0));
    if (w.last_error_message) {
      lineas.push("Antes: último error de Telegram al llamar al Worker: " + w.last_error_message
        + (w.last_error_date ? " (" + new Date(w.last_error_date * 1000).toISOString() + ")" : ""));
    }
  } else {
    lineas.push("Telegram no respondió getWebhookInfo: " + info.description + " (¿token correcto o bot bloqueado?)");
  }
  const wh = await tg(env, "setWebhook", {
    url: origin + "/",
    secret_token: env.WEBHOOK_SECRET,
    allowed_updates: ["message", "edited_message", "callback_query"],
    drop_pending_updates: true,
  });
  lineas.push("Webhook de Telegram: " + (wh.ok ? "OK" : "ERROR " + wh.description));
  const cm = await tg(env, "setMyCommands", {
    commands: COMANDOS_MENU.map(([command, description]) => ({ command, description })),
  });
  lineas.push("Menú de comandos: " + (cm.ok ? "OK" : "ERROR " + cm.description));
  try {
    const r = await gh(env, `/repos/${env.GITHUB_REPO}/contents/juegos.json`);
    lineas.push("Acceso al repo de GitHub: " + (r.ok ? "OK" : "ERROR " + r.status));
  } catch (e) {
    lineas.push("Acceso al repo de GitHub: ERROR " + e.message);
  }
  const hola = await tg(env, "sendMessage", {
    chat_id: env.CHAT_ID,
    text: "👑 Comandos activados. Escribe /ayuda para verlos.",
  });
  lineas.push("Mensaje de prueba a tu chat: " + (hola.ok ? "OK" : "ERROR " + hola.description
    + (hola.parameters && hola.parameters.retry_after ? ` (Telegram bloqueó al bot por ${Math.ceil(hola.parameters.retry_after / 60)} min)` : "")));
  try {
    const a = await gh(env, `/repos/${env.GITHUB_REPO}/actions/workflows`);
    lineas.push("Permiso de Actions (reloj y botones): " + (a.ok ? "OK" : "ERROR " + a.status + " — dale Actions: Read and write al token"));
  } catch (e) {
    lineas.push("Permiso de Actions: ERROR " + e.message);
  }
  return lineas.join("\n");
}

// --------------------------------------------------------------- comandos

async function manejar(env, chatId, texto) {
  const m = texto.match(/^\/([a-zA-Z_]+)(?:@\S+)?\s*([\s\S]*)$/);
  if (!m) {
    return send(env, chatId, marco("👋 <b>HOLA</b>", null, ["Escríbeme un comando, por ejemplo /ofertas.", "Todos los comandos: /ayuda"]));
  }
  const cmd = m[1].toLowerCase();
  const args = m[2].trim();

  switch (cmd) {
    case "start":
    case "ayuda":
    case "help":
      return send(env, chatId, AYUDA);
    case "config":
      return send(env, chatId, await cmdConfig(env));
    case "estado":
      return send(env, chatId, await cmdEstado(env));
    case "memoria":
    case "memorias":
    case "microsd":
      return send(env, chatId, await cmdMemoria(env, args));
    case "audio":
    case "audifonos":
      return send(env, chatId, await cmdAudio(env, args));
    case "consolas":
    case "consola":
      return send(env, chatId, await cmdConsolas(env, args));
    case "seguimiento":
    case "seguimientos":
      return send(env, chatId, await cmdSeguimiento(env));
    case "metricas":
    case "métricas":
      return send(env, chatId, await cmdMetricas(env, args));
    case "comision":
    case "comisiones":
      return send(env, chatId, await cmdComision(env, args));
    case "canal":
      return send(env, chatId, await cmdCanal(env, args));
    case "precios":
      return send(env, chatId, await cmdPrecios(env, args));
    case "ofertas":
      return send(env, chatId, await cmdOfertas(env, args));
    case "historial":
      if (!args) return send(env, chatId, uso("/historial", ["/historial metroid prime 4", "/historial zelda"]));
      {
        const r = await cmdPrecios(env, args);
        if (typeof r === "object") {
          const t2 = r.titulo.replace("💰 <b>PRECIOS ·", "📈 <b>HISTORIAL ·");
          r.bloques[0] = r.bloques[0].replace(r.titulo, t2);
          r.titulo = t2;
        }
        return send(env, chatId, typeof r === "string" ? r.replace("💰 <b>PRECIOS ·", "📈 <b>HISTORIAL ·") : r);
      }
    case "resumen":
      return send(env, chatId, await cmdResumen(env, args));
    case "evento":
    case "eventos":
      return send(env, chatId, await cmdEvento(env, args));
    case "seguidos":
    case "seguimiento":
      return send(env, chatId, await cmdSeguidos(env));
    case "revisar":
      return send(env, chatId, await cmdRevisar(env));
    case "agregar":
      return send(env, chatId, await cmdAgregar(env, args));
    case "quitar":
      return send(env, chatId, await cmdQuitar(env, args));
    case "objetivo":
      return send(env, chatId, await cmdObjetivo(env, args));
    case "descuento":
      return send(env, chatId, await cmdAjuste(env, args, "descuento_minimo_pct", "Descuento mínimo"));
    case "baja":
      return send(env, chatId, await cmdAjuste(env, args, "baja_minima_pct", "Baja mínima"));
    case "externos":
      return send(env, chatId, await cmdExternos(env, args));
    case "tienda":
      return send(env, chatId, await cmdTienda(env, args));
    case "quitartienda":
      return send(env, chatId, await cmdQuitarTienda(env, args));
    default:
      return send(env, chatId, marco("❓ <b>COMANDO DESCONOCIDO</b>", null, [`No conozco /${esc(cmd)}.`, "Todos los comandos: /ayuda"]));
  }
}

// ------------------------------------------------------------- otros productos
// Lee vistas/extras.json (lo escribe el bot en cada revision).

async function extras(env) {
  const { data } = await leer(env, "vistas/extras.json");
  return data;
}
const linkP = (x, txt) => `<a href="${escAttr(x.url || "")}">${txt}</a>`;
const sigue = (x) => (x.avisos && x.avisos[1]) ? ` · sigue ${esc(x.avisos[1].tienda)} ${clp(x.avisos[1].precio)}` : "";
const rebaja = (x) => (x.pct && x.pct >= 5 && x.ref_tipo) ? ` · −${Math.round(x.pct)}%` : "";
const sinExtras = (t) => marco(t, null, ["Todavía no hay datos: aparecen después de la próxima revisión."]);

async function cmdMemoria(env, args) {
  const titulo = "💾 <b>MEMORIAS microSD</b>";
  const d = await extras(env);
  if (!d || !(d.memorias || []).length) return sinExtras(titulo);
  const a = norm(args);
  let mem = d.memorias;
  if (a.includes("express") || a.includes("switch 2") || a === "2") mem = mem.filter((m) => m.tipo === "express");
  else if (a.includes("normal") || a.includes("switch 1") || a === "1") mem = mem.filter((m) => m.tipo === "normal");
  const gb = parseInt(a, 10);
  if (gb >= 32) mem = mem.filter((m) => m.gb === gb);
  const l = [];
  for (const [tipo, nombre] of [["express", "🎮 <b>SWITCH 2 · Express</b> (para juegos de Switch 2)"], ["normal", "🕹️ <b>SWITCH 1 · normal</b>"]]) {
    const xs = mem.filter((m) => m.tipo === tipo);
    if (!xs.length) continue;
    l.push("", nombre);
    const porGb = {};
    for (const m of xs) if (!porGb[m.gb] || m.precio < porGb[m.gb].precio) porGb[m.gb] = m;
    for (const m of Object.values(porGb).sort((x, y) => x.gb - y.gb)) {
      l.push(`• ${m.gb >= 1024 ? m.gb / 1024 + " TB" : m.gb + " GB"} ${esc(m.marca)}: ${linkP(m, "<b>" + clp(m.precio) + "</b>")} en ${esc(m.tienda)}`
        + ` (${clp(m.por_gb)}/GB)${rebaja(m)}${sigue(m)}`);
    }
  }
  if (!l.length) return marco(titulo, null, ["No encontré memorias con ese filtro."], ["/memoria — todas", "/memoria express · /memoria 256"]);
  return marco(titulo, "La más barata de cada tamaño", l.slice(1), ["/memoria express · /memoria normal · /memoria 256"]);
}

async function cmdAudio(env, args) {
  const titulo = "🎧 <b>AUDÍFONOS GAMER</b>";
  const d = await extras(env);
  if (!d || !(d.audifonos || []).length) return sinExtras(titulo);
  const a = norm(args);
  let xs = d.audifonos;
  if (a) xs = xs.filter((x) => norm(`${x.marca} ${x.nombre}`).includes(a));
  if (!xs.length) return marco(titulo, null, [`No encontré audífonos con "${esc(args)}".`], ["/audio — todos", "/audio hyperx · /audio zeus"]);
  const l = [];
  const rebajas = xs.filter((x) => x.pct >= 15 && x.ref_tipo).sort((x, y) => y.pct - x.pct).slice(0, 5);
  if (rebajas.length && !a) {
    l.push("🔥 <b>REBAJAS</b>");
    rebajas.forEach((x) => l.push(`• ${linkP(x, esc(x.nombre.slice(0, 45)))} — <b>${clp(x.precio)}</b> en ${esc(x.tienda)}${rebaja(x)}`));
    l.push("");
  }
  if (a) {
    l.push(...xs.slice(0, 12).map((x) => `• ${linkP(x, esc(x.nombre.slice(0, 45)))} — <b>${clp(x.precio)}</b> en ${esc(x.tienda)}${rebaja(x)}${sigue(x)}`));
  } else {
    l.push("💰 <b>EL MÁS BARATO DE CADA MARCA</b>");
    const porMarca = {};
    for (const x of xs) if (!porMarca[x.marca] || x.precio < porMarca[x.marca].precio) porMarca[x.marca] = x;
    Object.values(porMarca).sort((x, y) => x.precio - y.precio)
      .forEach((x) => l.push(`• ${esc(x.marca)}: ${linkP(x, esc(x.nombre.slice(0, 38)))} — <b>${clp(x.precio)}</b> en ${esc(x.tienda)}`));
  }
  return marco(titulo, `${xs.length} modelos de marca`, l, ["/audio hyperx · /audio logitech · /audio zeus"]);
}

async function cmdConsolas(env, args) {
  const titulo = "🕹️ <b>CONSOLAS NINTENDO</b>";
  const d = await extras(env);
  if (!d || !(d.consolas || []).length) return sinExtras(titulo);
  const l = [];
  for (const modelo of ["Switch 2", "Switch OLED", "Switch", "Switch Lite"]) {
    for (const bundle of [false, true]) {
      const xs = d.consolas.filter((c) => c.modelo === modelo && !!c.bundle === bundle);
      if (!xs.length) continue;
      const c = xs[0];
      l.push(`• <b>${modelo}${bundle ? " con juego" : ""}</b>: ${linkP(c, "<b>" + clp(c.precio) + "</b>")} en ${esc(c.tienda)}${rebaja(c)}${sigue(c)}`);
    }
  }
  return marco(titulo, "La más barata de cada versión", l);
}

async function cmdSeguimiento(env) {
  const titulo = "👀 <b>TUS SEGUIMIENTOS</b>";
  const d = await extras(env);
  if (!d || !(d.seguimientos || []).length) {
    return marco(titulo, null, ["Todavía no hay datos.", "Se configuran en juegos.json → seguimientos (ahora: Redragon Zeus)."]);
  }
  const l = [];
  const items = [...new Set(d.seguimientos.map((s) => s.item))];
  for (const it of items) {
    l.push(`<b>${esc(it)}</b>`);
    for (const s of d.seguimientos.filter((x) => x.item === it)) {
      const v = s.variante === "base" ? "modelo base" : s.variante.split(" ").map((w) => w.length === 1 ? w.toUpperCase()
        : w === "wireless" ? "inalámbrico" : w[0].toUpperCase() + w.slice(1)).join(" ");
      l.push(`• ${esc(v)}: ${linkP(s, "<b>" + clp(s.precio) + "</b>")} en ${esc(s.tienda)}${sigue(s)}`
        + (s.min_hist && s.min_hist < s.precio ? ` · mín ${clp(s.min_hist)}` : ""));
    }
    l.push("");
  }
  return marco(titulo, "Se revisan en cada revisión de precios", l);
}

// ------------------------------------------------------------- metricas
// Los links del canal pasan por /r: se cuenta el clic (D1 "METRICAS") y se redirige a la tienda.
// Las visitas de robots (vistas previas de Telegram, WhatsApp, buscadores) se guardan marcadas
// y no cuentan como clics.

const DESTINOS_OK = ["nintendo.com", "mercadolibre.cl", "mercadolibre.com", "wa.me", "t.me", "whatsapp.com"];
const ROBOT_RE = /bot|crawl|spider|preview|facebookexternalhit|whatsapp|telegram|slack|discord|curl|python|wget|headless/i;

function destinoPermitido(env, u) {
  let h;
  try { const x = new URL(u); if (x.protocol !== "https:") return false; h = x.hostname; } catch { return false; }
  const extra = String(env.PROXY_DOMINIOS || "").split(",").map((d) => d.trim()).filter(Boolean);
  return h.endsWith(".cl") || [...DESTINOS_OK, ...extra].some((d) => h === d || h.endsWith("." + d));
}

async function redirigir(request, env, url, ctx) {
  const destino = url.searchParams.get("u") || "";
  if (!destinoPermitido(env, destino)) return new Response("Link no válido.", { status: 400 });
  if (env.METRICAS) {
    const ahora = new Date();
    const chile = new Date(ahora.toLocaleString("en-US", { timeZone: "America/Santiago" }));
    const dia = `${chile.getFullYear()}-${String(chile.getMonth() + 1).padStart(2, "0")}-${String(chile.getDate()).padStart(2, "0")}`;
    const p = (k) => (url.searchParams.get(k) || "").slice(0, 120);
    const robot = ROBOT_RE.test(request.headers.get("user-agent") || "") ? 1 : 0;
    const guardar = env.METRICAS.prepare(
      "INSERT INTO clics (ts, dia, hora, post, slot, item, tienda, tipo, destino, robot) VALUES (?,?,?,?,?,?,?,?,?,?)")
      .bind(ahora.toISOString(), dia, chile.getHours(), p("p"), p("s"), p("i"), p("t"), p("k"),
        new URL(destino).hostname, robot).run().catch(() => null);
    if (ctx && ctx.waitUntil) ctx.waitUntil(guardar); else await guardar;
  }
  return Response.redirect(destino, 302);
}

async function metricas(env, dias = 7) {
  if (!env.METRICAS) return { ok: false, error: "Falta conectar la base METRICAS al Worker" };
  const desde = new Date(Date.now() - dias * 86400000).toISOString().slice(0, 10);
  const q = (sql, ...b) => env.METRICAS.prepare(sql).bind(...b).all().then((r) => r.results || []).catch(() => []);
  const base = "FROM clics WHERE robot = 0 AND dia >= ?";
  const [porDia, items, tiendas, slots, tipos, horas, comMes, comTot] = await Promise.all([
    q(`SELECT dia, COUNT(*) n ${base} GROUP BY dia ORDER BY dia`, desde),
    q(`SELECT item, COUNT(*) n ${base} AND item != '' AND tipo != 'whatsapp' GROUP BY item ORDER BY n DESC LIMIT 8`, desde),
    q(`SELECT tienda, COUNT(*) n ${base} AND tienda != '' GROUP BY tienda ORDER BY n DESC`, desde),
    q(`SELECT slot, COUNT(*) n, COUNT(DISTINCT post) posts ${base} AND slot != '' AND tipo != 'whatsapp' GROUP BY slot ORDER BY n DESC`, desde),
    q(`SELECT tipo, COUNT(*) n ${base} GROUP BY tipo ORDER BY n DESC`, desde),
    q(`SELECT hora, COUNT(*) n ${base} GROUP BY hora ORDER BY n DESC LIMIT 3`, desde),
    q("SELECT COALESCE(SUM(monto),0) total, COUNT(*) n FROM comisiones WHERE dia >= ?", new Date().toISOString().slice(0, 7) + "-01"),
    q("SELECT COALESCE(SUM(monto),0) total, COUNT(*) n FROM comisiones"),
  ]);
  const total = porDia.reduce((a, x) => a + x.n, 0);
  return { ok: true, dias, total, por_dia: porDia, items, tiendas, slots, tipos, horas,
    comisiones_mes: comMes[0] || { total: 0, n: 0 }, comisiones_total: comTot[0] || { total: 0, n: 0 } };
}

const NOMBRE_TIPO = { juego: "nombre del juego", tienda: "tienda", comprar: "botón Comprar", whatsapp: "compartir por WhatsApp" };

async function cmdMetricas(env, args) {
  const dias = Math.min(90, Math.max(1, parseInt(args, 10) || 7));
  const [m, miembros] = await Promise.all([metricas(env, dias), miembrosCanal(env)]);
  const titulo = `📈 <b>MÉTRICAS · últimos ${dias} días</b>`;
  if (!m.ok) return marco(titulo, null, [esc(m.error)]);
  const hoy = m.por_dia.length ? m.por_dia[m.por_dia.length - 1] : null;
  const l = ["👥 <b>CANAL</b>", `Suscriptores: <b>${miembros ?? "?"}</b>`];
  const sinShare = m.tipos.filter((t) => t.tipo !== "whatsapp").reduce((a, t) => a + t.n, 0);
  const share = (m.tipos.find((t) => t.tipo === "whatsapp") || {}).n || 0;
  l.push("", "🖱️ <b>CLICS</b>", `Total: <b>${sinShare}</b> a tiendas · ${share} compartidos por WhatsApp`);
  if (miembros) l.push(`Por suscriptor: ${(sinShare / miembros).toFixed(1)} en ${dias} días`);
  if (hoy) l.push(`Último día con clics: ${hoy.dia.slice(5)} · ${hoy.n}`);
  if (m.items.length) {
    l.push("", "🎮 <b>LO MÁS CLICKEADO</b>");
    m.items.forEach((x, i) => l.push(`${i + 1}. ${esc(x.item)} — ${x.n}`));
  }
  if (m.tiendas.length) l.push("", "🏬 <b>POR TIENDA</b>", m.tiendas.map((x) => `${esc(x.tienda)} ${x.n}`).join(" · "));
  if (m.slots.length) {
    l.push("", "📰 <b>POR TIPO DE POST</b> (clics por post)");
    m.slots.forEach((x) => l.push(`${esc(x.slot)}: ${x.n} en ${x.posts} post(s) · ${(x.n / Math.max(1, x.posts)).toFixed(1)}/post`));
  }
  if (m.tipos.length) l.push("", "👆 <b>DÓNDE TOCAN</b>", m.tipos.filter((x) => x.tipo !== "whatsapp")
    .map((x) => `${NOMBRE_TIPO[x.tipo] || esc(x.tipo || "otro")} ${x.n}`).join(" · "));
  if (m.horas.length) l.push("", "🕐 <b>HORAS CON MÁS CLICS</b>", m.horas.map((x) => `${x.hora}:00 (${x.n})`).join(" · "));
  l.push("", "💵 <b>COMISIONES</b>", `Este mes: <b>${clp(m.comisiones_mes.total)}</b> (${m.comisiones_mes.n}) · Total: ${clp(m.comisiones_total.total)}`);
  if (!sinShare) l.push("", "<i>Aún no hay clics medidos: se cuentan desde los posts nuevos.</i>");
  return marco(titulo, null, l, ["/metricas 30 — último mes", "/comision 5000 mercadolibre — anotar una comisión"]);
}

async function cmdComision(env, args) {
  const titulo = "💵 <b>COMISIONES</b>";
  if (!env.METRICAS) return marco(titulo, null, ["Falta conectar la base METRICAS al Worker."]);
  const m = args.match(/^\$?\s*([\d.]+)\s*(\S+)?\s*([\s\S]*)$/);
  if (!args || !m) {
    const mes = new Date().toISOString().slice(0, 7);
    const r = await env.METRICAS.prepare("SELECT dia, monto, fuente, nota FROM comisiones WHERE dia >= ? ORDER BY dia DESC LIMIT 15")
      .bind(mes + "-01").all().then((x) => x.results || []).catch(() => []);
    const tot = r.reduce((a, x) => a + x.monto, 0);
    return marco(titulo, `Este mes: ${clp(tot)}`, r.length ? r.map((x) => `• ${x.dia.slice(5)} · ${clp(x.monto)} · ${esc(x.fuente || "")} ${esc(x.nota || "")}`)
      : ["Todavía no anotas comisiones este mes."], ["/comision 5000 mercadolibre — anotar", "/comision 12000 falabella memoria 256 — con nota"]);
  }
  const monto = parseInt(m[1].replace(/\./g, ""), 10);
  if (!(monto > 0)) return uso("/comision", ["/comision 5000 mercadolibre"]);
  const hoy = new Date(new Date().toLocaleString("en-US", { timeZone: "America/Santiago" }));
  const dia = `${hoy.getFullYear()}-${String(hoy.getMonth() + 1).padStart(2, "0")}-${String(hoy.getDate()).padStart(2, "0")}`;
  await env.METRICAS.prepare("INSERT INTO comisiones (dia, monto, fuente, nota) VALUES (?,?,?,?)")
    .bind(dia, monto, (m[2] || "").toLowerCase(), (m[3] || "").trim()).run();
  return marco(titulo, null, [`✅ Anotada: ${clp(monto)}${m[2] ? " de " + esc(m[2]) : ""}.`, "Sale en /metricas y en el informe diario."]);
}

// ------------------------------------------------------------- panel de control

async function miembrosCanal(env) {
  const canal = env.CHANNEL_ID || env.TELEGRAM_CHANNEL_ID;
  if (!canal) return null;
  const r = await tg(env, "getChatMemberCount", { chat_id: canal });
  return r && r.ok ? r.result : null;
}

async function lineasCanal(env) {
  const [{ data: est }, { data: ccfg }, miembros] = await Promise.all([
    leer(env, "canal_estado.json"), leer(env, "canal.json"), miembrosCanal(env)]);
  const aj = (ccfg && ccfg.ajustes) || {};
  const e = est || {};
  const dia = (e.dias || {})[hoyChile()] || {};
  const cola = Object.values(e.cola || {});
  const l = [];
  if (miembros !== null) l.push(`👥 ${miembros} miembros`);
  l.push(aj.pausado ? "⏸️ <b>PAUSADO</b>: no publica nada" : "▶️ Publicando");
  l.push(aj.revision_previa ? `👀 Revisión previa: sí (${aj.revision_minutos ?? 10} min para revisar)` : "👀 Revisión previa: no (publica directo)");
  l.push(`📝 Hoy: ${dia.posts || 0} post(s)${(dia.slots || []).length ? " · " + dia.slots.join(", ") : ""}${dia.bombazos ? ` · 💣 ${dia.bombazos}` : ""}`);
  if (cola.length) l.push(`⏳ Esperando tu revisión: ${cola.map((c) => c.slot).join(", ")}`);
  if (e.ultimo_post) l.push(`🕐 Último post: ${esc(String(e.ultimo_post).slice(0, 16).replace("T", " "))}`);
  return l;
}

async function cmdEstado(env) {
  const { data: ev } = await leer(env, "vistas/estado.json");
  const l = ["🩺 <b>BOT</b>"];
  if (ev) {
    // "hace X h" se calcula ahora (el texto guardado por el bot queda congelado)
    const horasRev = ev.ultima_revision ? (Date.now() - Date.parse(ev.ultima_revision)) / 3600000 : null;
    l.push(...(ev.salud || []).map((x) => esc(String(x).startsWith("🔄 Última revisión") && horasRev !== null
      ? `🔄 Última revisión: hace ${Math.round(horasRev)} h${horasRev > 9 ? " ⚠️ (atrasada)" : ""}` : x)));
    l.push(`📊 Hoy: ${ev.revisiones_hoy || 0} revisión(es) · ${ev.alertas_hoy || 0} alerta(s) al momento`);
    if (ev.evento) l.push(`🚨 Modo evento: ${esc(ev.evento)}`);
    else if (ev.proximo) l.push(`📅 Próximo evento: ${esc(ev.proximo)}`);
  } else {
    l.push("Todavía no hay datos: aparecen después de la próxima revisión (/revisar).");
  }
  l.push("", "📣 <b>CANAL</b>", ...(await lineasCanal(env)));
  return marco("📟 <b>ESTADO GENERAL</b>", ev ? `Datos de la revisión del ${hora(ev.ultima_revision)}` : null, l,
    ["/canal · /resumen ahora · /revisar"]);
}

async function cmdCanal(env, args) {
  const a = norm(args);
  const cambiar = (msg, fn) => editarArchivo(env, "canal.json", `Canal: ${msg}`, (c) => { c.ajustes ||= {}; fn(c.ajustes); });
  if (/^(pausa|pausar|stop|off)$/.test(a)) {
    await cambiar("pausa", (aj) => { aj.pausado = true; });
    return guardado(["⏸️ Canal en pausa: no publica nada hasta que escribas /canal seguir.",
      "Los posts que estaban en revisión quedan esperando."], []);
  }
  if (/^(seguir|reanudar|play|on|activar)$/.test(a)) {
    await cambiar("seguir", (aj) => { delete aj.pausado; });
    return guardado("▶️ El canal vuelve a publicar desde la próxima pasada (cada 30 min).", []);
  }
  const rev = a.match(/^revision (on|off|si|no|\d+)$/);
  if (rev) {
    const v = rev[1];
    if (/^\d+$/.test(v)) {
      await cambiar("minutos de revision", (aj) => { aj.revision_previa = true; aj.revision_minutos = Number(v); });
      return guardado(`👀 Tienes <b>${v} minutos</b> para revisar cada post antes de que salga.`, []);
    }
    const on = v === "on" || v === "si";
    await cambiar(`revision ${on ? "on" : "off"}`, (aj) => { aj.revision_previa = on; });
    return guardado(on ? "👀 Cada post te llega primero a ti con Publicar ya / Saltar / Quitar juegos."
      : "🚀 Los posts salen directo al canal, sin pasar por tu chat.", []);
  }
  if (a) return uso("/canal", ["/canal — estado del canal", "/canal pausa — dejar de publicar",
    "/canal seguir — volver a publicar", "/canal revision on|off — revisar cada post antes",
    "/canal revision 15 — minutos para revisar"]);
  return marco("📣 <b>CANAL</b>", null, await lineasCanal(env),
    ["/canal pausa · /canal seguir · /canal revision on|off"]);
}

async function cmdConfig(env) {
  const { data: cfg } = await leer(env, "juegos.json");
  const { data: idx } = await leer(env, "vistas/indice.json");
  const st = idx ? { fallos: idx.fallos, ultima_revision: idx.revision } : null;
  const aj = cfg.ajustes || {};
  const juegos = cfg.juegos || [];
  const tiendas = cfg.tiendas || [];
  const fallos = (st && st.fallos) || {};
  const objetivos = juegos.filter((j) => j.objetivo_clp != null || j.objetivo_usd != null);
  const l = [
    "🔔 <b>Cuándo te aviso</b>",
    `• Descuento real de <b>${aj.descuento_minimo_pct ?? 25}%</b> o más (/descuento)`,
    `• Baja de <b>${aj.baja_minima_pct ?? 10}%</b> entre revisiones (/baja)`,
    "• Mínimo histórico 🏆 y precio objetivo 🎯",
    `• Vendedores externos: <b>${aj.excluir_externos ? "excluidos" : "incluidos 🏪"}</b> (/externos)`,
    "",
    `🗞️ Resumen diario: <b>${(aj.modo_alertas || "resumen") === "resumen" ? `a las ${aj.hora_resumen ?? 20}:00` : "apagado"}</b> (/resumen)`,
    `🚨 Modo evento: <b>${idx && idx.evento ? "activo · " + esc(idx.evento.nombre) : idx && idx.proximo ? "próximo " + esc(idx.proximo.nombre) + " el " + fecha(idx.proximo.desde) : "sin eventos"}</b> (/evento)`,
    "",
    `🌎 <b>eShop comparadas</b>: ${esc((aj.regiones_eshop || ["US", "CA", "MX", "BR", "CL", "CO", "PE"]).join(" · "))}`,
    "",
    `🏬 <b>Tiendas (${tiendas.filter((t) => t.activa !== false).length} activas)</b>`,
  ];
  for (const t of tiendas) {
    const est = t.activa === false ? "⏸️ pausada" : fallos[t.nombre] ? `⚠️ ${fallos[t.nombre]} ${fallos[t.nombre] === 1 ? "fallo" : "fallos seguidos"}` : "✅";
    l.push(`• ${esc(t.nombre)} ${est}`);
  }
  l.push("", `🎮 <b>Juegos vigilados (${juegos.length})</b>`);
  l.push(juegos.map((j) => esc(j.nombre)).sort((a, b) => a.localeCompare(b, "es")).join(" · "));
  if (objetivos.length) {
    l.push("", "🎯 <b>Precios objetivo</b>");
    for (const j of objetivos) {
      const o = [j.objetivo_clp != null ? clp(j.objetivo_clp) : null, j.objetivo_usd != null ? usd(j.objetivo_usd) : null].filter(Boolean);
      l.push(`• ${esc(j.nombre)}: ${o.join(" / ")}`);
    }
  }
  const sub = st && st.ultima_revision ? `Última revisión: ${hora(st.ultima_revision)}` : null;
  return marco("⚙️ <b>CONFIGURACIÓN</b>", sub, l, ["Cambiar: /agregar · /quitar · /objetivo · /tienda"]);
}

function vigente(it, st) {
  if (!st || !st.ultima_revision) return true;
  const corte = new Date(new Date(st.ultima_revision).getTime() - 12 * 3600e3).toISOString().slice(0, 19);
  return (it.visto || "") >= corte;
}

function productosVigentes(st) {
  if (!st) return [];
  return Object.entries(st.productos || {})
    .filter(([, p]) => p.nombre && p.precio != null && vigente(p, st))
    .map(([clave, p]) => ({ clave, ...p }));
}

function eshopVigentes(st) {
  if (!st) return [];
  return Object.entries(st.eshop || {})
    .filter(([, e]) => e.titulo && e.ultimo != null && vigente(e, st))
    .map(([nsuid, e]) => ({ nsuid, ...e }));
}

const REF_TXT = { tipico: "vs su precio típico", mercado: "vs otras tiendas", tienda: "según la tienda" };
const REF_CORTO = { tipico: "vs típico", mercado: "vs otras", tienda: "según tienda" };

function nombreProducto(p) {
  return esc(p.nombre) + (p.variante && p.variante.length ? ` (${esc(p.variante.join(" "))})` : "");
}

function lineaProducto(p, st, { conJuego = false, detalle = false } = {}) {
  let s = `<a href="${escAttr(p.url)}">${nombreProducto(p)}</a> · ${esc(p.plataforma)}`;
  if (conJuego) s = `<i>${esc(p.juego)}</i> · ` + s;
  s += `\n   📦 ${clp(p.precio)} en ${esc(p.tienda)}` + (p.externo ? ` 🏪 ${esc(p.vendedor || "vendedor externo")}` : "");
  if (p.min_hist && p.dias >= 7 && p.precio <= p.min_hist) s += " 🏆";
  if (p.ref && p.pct >= 5) s += `  −${Math.round(p.pct)}% ${REF_TXT[p.ref_tipo] || "vs referencia"} (${clp(p.ref)})`;
  const e = p.nsuid && st && st.eshop ? st.eshop[p.nsuid] : null;
  if (e && e.ultimo != null) {
    s += `\n   🎮 ${usd(e.ultimo)}` + (e.clp ? ` ≈${clp(e.clp)}` : "") + (e.pais && e.pais !== "US" ? ` en ${esc(e.pais)}` : "") +
      (pctEshop(e) ? ` (−${Math.round(pctEshop(e))}%)` : "") +
      (e.clp ? (e.clp < p.precio ? " → conviene digital" : " → conviene físico") : "");
  }
  if (detalle && p.avisos && p.avisos.length > 1) {
    for (const a of p.avisos) s += `\n      · <a href="${escAttr(a.url)}">${clp(a.precio)} ${esc(a.tienda)}</a>${a.externo ? " 🏪" : ""}`;
  }
  if (detalle && p.min_hist && p.dias >= 1) {
    s += `\n   📉 mínimo visto ${clp(p.min_hist)}` + (p.min_tienda ? ` en ${esc(p.min_tienda)}` : "") +
      (p.min_fecha ? ` (${fecha(p.min_fecha)})` : "");
  }
  return s;
}

function lineaEshop(e, conJuego = false) {
  const pct = Math.round(pctEshop(e));
  let s = `<a href="${escAttr(e.url)}">${esc(e.titulo)}</a>${e.plataforma === "Switch 2" && !/switch 2/i.test(e.titulo) ? " <b>S2</b>" : ""}\n   🎮 ${usd(e.ultimo)}` +
    (e.clp ? ` ≈${clp(e.clp)}` : "") + (e.pais && e.pais !== "US" ? ` en ${esc(e.pais)}` : "");
  if (pct > 0) s += `  <s>${usd(e.normal)}</s>  −${pct}%`;
  if (e.fin && pct > 0) s += `  ⏳ ${fecha(e.fin.slice(0, 10))}`;
  if (e.min_hist && e.dias >= 7 && e.ultimo <= e.min_hist) s += " 🏆";
  if (conJuego) s = `<i>${esc(e.juego)}</i> · ` + s;
  return s;
}

// % de oferta real en la eShop (ignora diferencias chicas entre paises)
function pctEshop(e) {
  if (!e || !e.normal || !(e.normal > e.ultimo)) return 0;
  const pct = 100 * (1 - e.ultimo / e.normal);
  return pct >= 5 ? pct : 0;
}

function coincideBusqueda(q, juego, nombre) {
  return norm(juego) === q || norm(nombre).includes(q) || norm(juego).includes(q);
}

// ------------------------------------------------- vistas (tarjetas ya armadas)

const NIVEL = {
  objetivo: [0, "🎯", "LLEGÓ A TU OBJETIVO"],
  compra: [1, "🟢", "COMPRA YA"],
  bueno: [2, "🟡", "BUEN PRECIO"],
  regular: [3, "🟠", "PRECIO REGULAR"],
  normal: [4, "⚪", "PRECIO NORMAL"],
};
const LEYENDA = "🟢 compra ya · 🟡 buen precio · 🟠 regular · ⚪ normal · 🎯 tu objetivo\n🏪 vendedor externo del marketplace · 🥇🥈🥉 ranking de tiendas";
const SEP_TARJETA = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈";

function resumenNiveles(items) {
  const c = {};
  for (const it of items) c[it.nivel] = (c[it.nivel] || 0) + 1;
  return Object.keys(NIVEL).filter((k) => c[k]).map((k) => `${NIVEL[k][1]} ${c[k]} · ${NIVEL[k][2].toLowerCase()}`);
}

function seccionesPorNivel(items) {
  const out = [];
  for (const k of Object.keys(NIVEL)) {
    const g = items.filter((it) => it.nivel === k);
    if (!g.length) continue;
    out.push(`━━━ ${NIVEL[k][1]} <b>${NIVEL[k][2]}</b> (${g.length}) ━━━`);
    for (const it of g) out.push({ t: `${it.texto}\n${SEP_TARJETA}`, boton: it.boton });
  }
  return out;
}

function sinDatos(titulo) {
  return marco(titulo, null, ["Todavía no hay datos armados.", "Usa /revisar y vuelve en unos minutos."]);
}

async function cmdOfertas(env, args) {
  const { data } = await leer(env, "vistas/ofertas.json");
  const titulo = "🔥 <b>OFERTAS VIGENTES</b>";
  if (!data) return sinDatos(titulo);
  const a = norm(args);
  const min = parseFloat(a.replace(/[^\d.]/g, "")) || 0;
  const tipo = /fisic/.test(a) ? "fisico" : /digit|eshop/.test(a) ? "digital" : null;
  const todo = /todo|todas|regular/.test(a);
  const solo = /compra|ya/.test(a) ? ["objetivo", "compra"] : todo ? null : ["objetivo", "compra", "bueno"];
  let items = data.items.filter((it) => it.pct >= min && (!tipo || it.tipo === tipo) && (!solo || solo.includes(it.nivel)));
  const regulares = todo ? 0 : data.items.filter((it) => it.nivel === "regular" && it.pct >= min && (!tipo || it.tipo === tipo)).length;
  const filtros = [min ? `${min}% o más` : null, tipo ? (tipo === "fisico" ? "solo físico" : "solo digital") : null,
    /compra|ya/.test(a) ? "solo para comprar ya" : null, todo ? "incluye regulares" : null].filter(Boolean).join(" · ");
  const sub = `Revisión del ${hora(data.revision)}${filtros ? " · " + filtros : ""}`;
  if (!items.length) return marco(titulo, sub, ["No hay ofertas con ese filtro ahora.", "Prueba: /ofertas · /ofertas fisico · /ofertas 20"]);
  const fis = items.filter((i) => i.tipo === "fisico").length;
  const cab = marco(titulo, sub, ["📋 <b>RESUMEN</b>", ...resumenNiveles(items),
    `📦 ${fis} físico${fis === 1 ? "" : "s"} · 🎮 ${items.length - fis} digital${items.length - fis === 1 ? "" : "es"}`]);
  return { titulo, bloques: [cab, ...seccionesPorNivel(items),
    `${SEP}\n${LEYENDA}` + (regulares ? `\n\n🟠 Hay ${regulares} más a precio regular (−5% a −15%): /ofertas todo` : "") +
    "\n\nFiltrar: /ofertas compra · /ofertas fisico · /ofertas digital · /ofertas 30 · /ofertas todo"] };
}

async function cmdPrecios(env, args) {
  const { data: indice } = await leer(env, "vistas/indice.json");
  if (!indice) return sinDatos("💰 <b>PRECIOS ACTUALES</b>");
  const sub = `Revisión del ${hora(indice.revision)}`;
  if (!args) {
    const con = indice.sagas.filter((s) => s.orden <= 2).sort((a, b) => a.orden - b.orden || a.juego.localeCompare(b.juego, "es"));
    const sin = indice.sagas.filter((s) => s.orden > 2).sort((a, b) => a.juego.localeCompare(b.juego, "es"));
    const l = [];
    if (con.length) l.push(`🔥 <b>CON OPORTUNIDADES</b> (${con.length})`, ...con.map((s) => s.linea));
    if (sin.length) l.push(...(l.length ? [""] : []), `😴 <b>SIN NOVEDADES</b> (${sin.length})`, ...sin.map((s) => s.linea));
    return marco("💰 <b>PRECIOS ACTUALES</b>", `${sub} · ${indice.sagas.length} sagas · ${indice.total} productos`, l,
      ["📦 físico · 🎮 eShop (país más barato)", "Ver una saga completa: /precios zelda", "Buscar un juego: /precios tears of the kingdom"]);
  }
  const q = norm(args);
  let saga = indice.sagas.find((s) => norm(s.juego) === q) ||
    indice.sagas.find((s) => norm(s.juego).includes(q) || q.includes(norm(s.juego)));
  let filtro = null;
  if (!saga) {
    saga = indice.sagas.find((s) => (s.nombres || []).some((n) => n.includes(q)));
    filtro = q;
  } else if (norm(saga.juego) !== q && !norm(saga.juego).includes(q)) {
    filtro = q;
  }
  const titulo = `💰 <b>PRECIOS · ${esc(args.toUpperCase())}</b>`;
  if (!saga) return marco(titulo, sub, [`No encontré "${esc(args)}".`, "Mira las sagas con /precios o agrégala con /agregar."]);
  const { data } = await leer(env, `vistas/saga/${saga.slug}.json`);
  if (!data) return sinDatos(titulo);
  let items = data.items;
  if (filtro) items = items.filter((it) => norm(it.nombre || it.texto).includes(filtro));
  if (!items.length) items = data.items;
  const fis = items.filter((i) => i.tipo === "fisico").length;
  const cab = marco(titulo, `${sub} · saga ${esc(saga.juego)}`, ["📋 <b>RESUMEN</b>", ...resumenNiveles(items),
    `📦 ${fis} físico${fis === 1 ? "" : "s"} · 🎮 ${items.length - fis} digital${items.length - fis === 1 ? "" : "es"}`]);
  return { titulo, bloques: [cab, ...seccionesPorNivel(items), `${SEP}\n${LEYENDA}\n\nTodas las sagas: /precios · Ofertas: /ofertas`] };
}

function lineaCorta(p, st) {
  const plat = p.plataforma === "Switch 2" ? " <b>S2</b>" : "";
  const varTxt = p.variante && p.variante.length ? ` (${esc(p.variante.join(" "))})` : "";
  let s = `• <a href="${escAttr(p.url)}">${esc(p.nombre)}</a>${varTxt}${plat} — ${clp(p.precio)} ${esc(p.tienda)}${p.externo ? " 🏪" : ""}`;
  if (p.min_hist && p.dias >= 7 && p.precio <= p.min_hist) s += " 🏆";
  if (p.ref && p.pct >= 5) s += ` · −${Math.round(p.pct)}% ${REF_CORTO[p.ref_tipo] || ""}`;
  const e = p.nsuid && st && st.eshop ? st.eshop[p.nsuid] : null;
  if (e && e.ultimo != null) {
    s += ` · 🎮 ${usd(e.ultimo)}${e.pais && e.pais !== "US" ? " " + e.pais : ""}`;
    if (e.clp) s += e.clp < p.precio ? " (digital más barato)" : "";
  }
  return s;
}

const BARRAS = "▁▂▃▄▅▆▇█";

function sparkline(valores) {
  if (valores.length < 2) return "";
  const lo = Math.min(...valores), hi = Math.max(...valores);
  return valores.map((v) => BARRAS[hi === lo ? 3 : Math.round(((v - lo) / (hi - lo)) * 7)]).join("");
}

function valor(v) {
  return Array.isArray(v) ? v[0] : v;
}

async function cmdHistorial(env, args) {
  if (!args) return uso("/historial", ["/historial metroid prime 4", "/historial zelda"]);
  const { data: hist } = await leer(env, "history.json");
  const { data: st } = await leer(env, "state.json");
  if (!hist || !hist.productos) return marco("📈 <b>HISTORIAL DE PRECIOS</b>", null, ["Todavía no hay historial. Se va armando con cada revisión."]);
  const q = norm(args);
  const encontrados = Object.entries(hist.productos)
    .filter(([, h]) => coincideBusqueda(q, h.juego || "", h.nombre || ""))
    .sort((a, b) => Object.keys(b[1].f || {}).length - Object.keys(a[1].f || {}).length);
  if (!encontrados.length) return marco(`📈 <b>HISTORIAL · ${esc(args.toUpperCase())}</b>`, null, [`No hay historial para "${esc(args)}".`]);
  const l = [];
  for (const [clave, h] of encontrados.slice(0, 8)) {
    const fechas = Object.keys(h.f || {}).sort();
    if (!fechas.length) continue;
    const vals = fechas.map((d) => valor(h.f[d]));
    const iMin = vals.indexOf(Math.min(...vals));
    const tiendaMin = Array.isArray(h.f[fechas[iMin]]) ? h.f[fechas[iMin]][1] : null;
    const actual = st && st.productos && st.productos[clave];
    if (l.length) l.push("");
    l.push(`<b>${esc(h.nombre)}</b> · ${esc(h.plataforma || "")}`);
    if (actual && vigente(actual, st)) l.push(`   Hoy: ${clp(actual.precio)} en ${esc(actual.tienda)}`);
    l.push(`   🏆 Mínimo: ${clp(vals[iMin])}${tiendaMin ? " en " + esc(tiendaMin) : ""} (${fecha(fechas[iMin])})`);
    const ordenados = [...vals].sort((a, b) => a - b);
    const tipico = ordenados[Math.floor(ordenados.length / 2)];
    l.push(`   Típico: ${clp(tipico)} · Máximo: ${clp(Math.max(...vals))}`);
    const ult = fechas.slice(-30).map((d) => valor(h.f[d]));
    l.push(`   ${sparkline(ult)}  ${fechas.length} día${fechas.length === 1 ? "" : "s"} de datos`);
    const nsuid = actual && actual.nsuid;
    const he = nsuid && hist.eshop && hist.eshop[nsuid];
    if (he) {
      const ev = Object.values(he).map(valor);
      l.push(`   🎮 eShop: hoy ${usd(ev[ev.length - 1])} · mínimo ${usd(Math.min(...ev))}`);
    }
  }
  const pie = ["Mínimo y típico usan el precio más bajo de cada día, entre todas las tiendas"];
  if (encontrados.length > 8) pie.unshift(`… y ${encontrados.length - 8} más. Escribe un nombre más preciso.`);
  return marco(`📈 <b>HISTORIAL · ${esc(args.toUpperCase())}</b>`, `${Math.min(8, encontrados.length)} de ${encontrados.length} productos`, l, pie);
}

async function cmdRevisar(env) {
  const r = await gh(env, `/repos/${env.GITHUB_REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    body: JSON.stringify({ ref: "main", inputs: { modo: "normal" } }),
  });
  if (r.status !== 204) throw new Error(`GitHub no inició la revisión (${r.status}): ${(await r.text()).slice(0, 150)}`);
  return marco("🔎 <b>REVISIÓN EN CURSO</b>", null, [
    "Estoy revisando todas las tiendas y la eShop.",
    "En unos 8 minutos te llega la 🔔 alerta si hay algo urgente; lo demás va al 🗞️ resumen.",
  ], ["Después: /ofertas · /precios"]);
}

async function cmdAgregar(env, args) {
  if (!args) return uso("/agregar", ["/agregar Kirby", "/agregar Final Fantasy | final fantasy, ff"]);
  const [nombreRaw, busqRaw] = args.split("|");
  const nombre = nombreRaw.trim();
  const buscar = busqRaw
    ? busqRaw.split(",").map((s) => s.trim().toLowerCase()).filter(Boolean)
    : [nombre.toLowerCase()];
  if (!nombre || !buscar.length) return "Falta el nombre del juego.";
  await editar(env, `Agrega juego ${nombre}`, (cfg) => {
    cfg.juegos ||= [];
    if (cfg.juegos.some((j) => norm(j.nombre) === norm(nombre))) {
      throw new Error(`"${nombre}" ya está en la lista.`);
    }
    cfg.juegos.push({ nombre, buscar: buscar.length === 1 ? buscar[0] : buscar });
  });
  return guardado([`➕ Juego agregado: <b>${esc(nombre)}</b>`, `Busca: ${esc(buscar.join(", "))}`]);
}

async function cmdQuitar(env, args) {
  if (!args) return uso("/quitar", ["/quitar Kirby"]);
  let quitado;
  await editar(env, `Quita juego ${args}`, (cfg) => {
    const i = (cfg.juegos || []).findIndex((j) => norm(j.nombre) === norm(args));
    if (i < 0) throw new Error(`No encontré "${args}". Revisa el nombre con /config.`);
    quitado = cfg.juegos.splice(i, 1)[0].nombre;
  });
  return guardado(`🗑️ Juego quitado: <b>${esc(quitado)}</b>`);
}

async function cmdObjetivo(env, args) {
  const ayuda = uso("/objetivo", ["/objetivo Zelda 30000 — pesos, físico", "/objetivo Zelda 29.99 usd — eShop", "/objetivo Zelda quitar"]);
  if (!args) return ayuda;
  let resp;
  const quitar = args.match(/^(.+?)\s+(quitar|borrar|no)$/i);
  if (quitar) {
    await editar(env, `Quita objetivo ${quitar[1]}`, (cfg) => {
      const j = buscarJuego(cfg, quitar[1]);
      delete j.objetivo_clp;
      delete j.objetivo_usd;
      resp = guardado(`🎯 Sin precio objetivo para <b>${esc(j.nombre)}</b>`);
    });
    return resp;
  }
  const m = args.match(/^(.+?)\s+(us\$|\$)?\s*([\d.,]+)\s*(usd|us\$|dolares|dólares|clp|pesos)?$/i);
  if (!m) return ayuda;
  const nombre = m[1].trim();
  const numero = m[3];
  const marca = ((m[2] || "") + (m[4] || "")).toLowerCase();
  const esUsd = /us|dol|dól/.test(marca) || (!/clp|peso/.test(marca) && /^\d+[.,]\d{1,2}$/.test(numero));
  const valor = esUsd
    ? parseFloat(numero.replace(",", "."))
    : parseInt(numero.replace(/[.,]/g, ""), 10);
  if (!(valor > 0)) return ayuda;
  await editar(env, `Objetivo ${nombre}`, (cfg) => {
    const j = buscarJuego(cfg, nombre);
    j[esUsd ? "objetivo_usd" : "objetivo_clp"] = valor;
    resp = guardado(`🎯 <b>${esc(j.nombre)}</b>: te aviso cuando ${esUsd ? "en la eShop" : "en tiendas"} llegue a <b>${esUsd ? usd(valor) : clp(valor)}</b> o menos.`);
  });
  return resp;
}

async function cmdAjuste(env, args, clave, etiqueta) {
  const n = parseFloat((args || "").replace(/[^\d.]/g, ""));
  if (!(n >= 1 && n <= 95)) return uso(`/${clave.startsWith("desc") ? "descuento" : "baja"}`, [`/${clave.startsWith("desc") ? "descuento" : "baja"} 30 — un número entre 1 y 95`]);
  await editar(env, `${etiqueta} ${n}%`, (cfg) => {
    cfg.ajustes ||= {};
    cfg.ajustes[clave] = n;
  });
  return guardado(`${etiqueta}: <b>${n}%</b>`);
}

async function cmdExternos(env, args) {
  const a = norm(args);
  if (!["si", "no"].includes(a)) return uso("/externos", ["/externos si — incluir vendedores externos 🏪", "/externos no — solo lo que vende la tienda"]);
  await editar(env, `Vendedores externos: ${a}`, (cfg) => {
    cfg.ajustes ||= {};
    cfg.ajustes.excluir_externos = a === "no";
  });
  return guardado(a === "no"
    ? "Solo muestro lo que vende la tienda misma, sin vendedores externos."
    : "Incluyo vendedores externos de los marketplaces, marcados con 🏪.");
}

async function cmdTienda(env, args) {
  const m = (args || "").match(/^(.+?)\s+(https?:\/\/\S+)$/i);
  if (!m) return uso("/tienda", ["/tienda Zmart https://www.zmart.cl"]);
  const nombre = m[1].trim();
  const url = m[2].replace(/\/+$/, "");
  await editar(env, `Agrega tienda ${nombre}`, (cfg) => {
    cfg.tiendas ||= [];
    if (cfg.tiendas.some((t) => norm(t.nombre) === norm(nombre))) {
      throw new Error(`La tienda "${nombre}" ya está.`);
    }
    cfg.tiendas.push({ nombre, url });
  });
  return guardado([`🏬 Tienda agregada: <b>${esc(nombre)}</b>`, "En la próxima revisión pruebo cómo leerla; si no puedo, te aviso."]);
}

async function cmdQuitarTienda(env, args) {
  if (!args) return uso("/quitartienda", ["/quitartienda Zmart"]);
  let quitada;
  await editar(env, `Quita tienda ${args}`, (cfg) => {
    const i = (cfg.tiendas || []).findIndex((t) => norm(t.nombre) === norm(args));
    if (i < 0) throw new Error(`No encontré la tienda "${args}". Revisa con /config.`);
    quitada = cfg.tiendas.splice(i, 1)[0].nombre;
  });
  return guardado(`🗑️ Tienda quitada: <b>${esc(quitada)}</b>`);
}

function buscarJuego(cfg, nombre) {
  const q = norm(nombre);
  const js = cfg.juegos || [];
  const j = js.find((x) => norm(x.nombre) === q) ||
    js.find((x) => norm(x.nombre).startsWith(q)) ||
    js.find((x) => norm(x.nombre).includes(q));
  if (!j) throw new Error(`No encontré "${nombre}". Revisa el nombre con /config.`);
  return j;
}


// ------------------------------------------------------------- agenda

// Cada 30 min: el canal (publica si toca un post). Cada hora (minuto 5): la revisión de
// precios, que decide sola si toca revisar o mandar el informe. Primero la revisión, así el
// canal (que espera en la misma fila de GitHub) usa los precios recién leídos.
async function reloj(env, ahora) {
  const tareas = [];
  if (ahora.getUTCMinutes() < 30) tareas.push([WORKFLOW, { modo: "programado" }]);
  tareas.push([WORKFLOW_CANAL, { slot: "auto", prueba: "false" }]);
  for (const [wf, inputs] of tareas) {
    const r = await gh(env, `/repos/${env.GITHUB_REPO}/actions/workflows/${wf}/dispatches`, {
      method: "POST",
      body: JSON.stringify({ ref: "main", inputs }),
    });
    if (r.status !== 204) {
      const detalle = (await r.text()).slice(0, 200);
      console.log(`reloj: ${wf} -> ${r.status} ${detalle}`);
      // aviso como mucho cada 6 horas para no llenar el chat
      if (ahora.getUTCHours() % 6 === 0 && ahora.getUTCMinutes() < 30) {
        await send(env, env.CHAT_ID, marco("⚠️ <b>RELOJ DEL WORKER</b>", null, [
          `GitHub no aceptó iniciar <code>${wf}</code> (${r.status}).`,
          "Revisa que GITHUB_TOKEN tenga permiso <b>Actions: Read and write</b>.",
          esc(detalle),
        ]));
      }
    }
  }
}

async function dispatch(env, modo) {
  const r = await gh(env, `/repos/${env.GITHUB_REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    body: JSON.stringify({ ref: "main", inputs: { modo } }),
  });
  if (r.status !== 204) throw new Error(`GitHub no inició la tarea (${r.status}): ${(await r.text()).slice(0, 150)}`);
}

async function cmdResumen(env, args) {
  const a = norm(args);
  const titulo = "🗞️ <b>RESUMEN DIARIO</b>";
  if (a === "ahora" || a === "ya") {
    await dispatch(env, "resumen");
    return marco(titulo, null, ["Lo estoy armando: llega en 1–2 minutos."]);
  }
  if (a === "off" || a === "no" || a === "apagar") {
    await editar(env, "Resumen diario apagado", (cfg) => { cfg.ajustes ||= {}; cfg.ajustes.modo_alertas = "inmediato"; });
    return guardado(["🔔 Sin resumen: todas las ofertas te llegan apenas las encuentro."], []);
  }
  const h = parseInt(a, 10);
  if (a && !(h >= 0 && h <= 23)) return uso("/resumen", ["/resumen 21 — a qué hora llega (0 a 23)", "/resumen ahora — mandarlo ya", "/resumen off — todo al momento, sin resumen"]);
  if (a) {
    await editar(env, `Resumen a las ${h}:00`, (cfg) => { cfg.ajustes ||= {}; cfg.ajustes.hora_resumen = h; cfg.ajustes.modo_alertas = "resumen"; });
    return guardado([`🗞️ El resumen te llega todos los días a las <b>${h}:00</b>.`,
      "🔔 Lo urgente (🎯 objetivo, 🟢 compra ya, ⭐ seguidos, 🏆 mínimos) sigue llegando al momento."], []);
  }
  const { data: cfg } = await leer(env, "juegos.json");
  const { data: idx } = await leer(env, "vistas/indice.json");
  const aj = (cfg && cfg.ajustes) || {};
  const on = (aj.modo_alertas || "resumen") === "resumen";
  return marco(titulo, null, [
    on ? `✅ Activo: todos los días a las <b>${aj.hora_resumen ?? 20}:00</b>` : "⏸️ Apagado: todo llega al momento",
    on ? `🗂️ ${idx && idx.pendientes || 0} oferta(s) esperando el próximo resumen` : "",
    "",
    "🔔 Al momento: 🎯 objetivo · 🟢 compra ya · ⭐ seguidos · 🏆 mínimos",
    "🗞️ En el resumen: 🟡 buen precio · 🟠 regular · ⏳ ofertas que terminan pronto · 🎯 cómo van tus objetivos · 📅 próximo evento",
  ].filter((x) => x !== ""), ["/resumen 21 · /resumen ahora · /resumen off"]);
}

async function cmdEvento(env, args) {
  const a = norm(args);
  const titulo = "🚨 <b>MODO EVENTO</b>";
  const m = a.match(/^(on|si|activar)\s*(\d+)?/);
  if (m) {
    const horas = Math.min(parseInt(m[2] || "24", 10), 24 * 14);
    const hasta = new Date(Date.now() + horas * 3600e3).toISOString().slice(0, 19) + "Z";
    await editar(env, `Modo evento ${horas} h`, (cfg) => { cfg.ajustes ||= {}; cfg.ajustes.evento_manual = { nombre: "Modo evento manual", hasta }; });
    return guardado([`🚨 Modo evento activo por <b>${horas} horas</b>.`, "🔎 Reviso cada hora y te mando todas las ofertas al momento."], ["Apagar: /evento off"]);
  }
  if (a === "off" || a === "no" || a === "apagar") {
    await editar(env, "Modo evento apagado", (cfg) => { cfg.ajustes ||= {}; delete cfg.ajustes.evento_manual; });
    return guardado("Modo evento manual apagado. Los eventos del calendario se siguen activando solos.", []);
  }
  if (a) return uso("/evento", ["/evento on — activar 24 horas", "/evento on 72 — activar 72 horas", "/evento off — apagar el modo manual"]);
  const { data: idx } = await leer(env, "vistas/indice.json");
  const ev = idx && idx.evento, prox = idx && idx.proximo;
  const l = [];
  if (ev) l.push(`🚨 <b>ACTIVO</b>: ${esc(ev.nombre)} hasta el ${fecha(ev.hasta)}`, "Reviso cada hora y todo llega al momento.");
  else l.push("😴 Ningún evento activo ahora: reviso cada 6 horas.");
  if (prox) {
    const dias = Math.round((new Date(prox.desde) - new Date(hoyChile())) / 86400e3);
    l.push("", "📅 <b>Próximo</b>", `• ${esc(prox.nombre)}: ${fecha(prox.desde)} al ${fecha(prox.hasta)} (en ${dias} días)` + (prox.nota ? `\n  <i>${esc(prox.nota)}</i>` : ""));
  }
  l.push("", "🔎 Durante un evento cada tarjeta compara contra el precio de las semanas anteriores y te avisa si una tienda subió el precio justo antes para inflar el «descuento».");
  return marco(titulo, null, l, ["/evento on 24 · /evento off"]);
}

// ------------------------------------------------------------- botones

const SEG_VACIO = { objetivos: {}, seguidos: {}, silenciados: {} };

function hoyChile(dias = 0) {
  const d = new Date(Date.now() - 3 * 3600e3 + dias * 86400e3);
  return d.toISOString().slice(0, 10);
}

function dineroDe(info, v) {
  return info.moneda === "USD" ? usd(v) : clp(v);
}

function redondeo(info, v) {
  if (info.moneda === "USD") return Math.max(0.99, Math.floor(v) - 0.01);
  return Math.max(990, Math.round(v / 1000) * 1000 - 10);
}

function estadoSeg(seg, id) {
  const sil = (seg.silenciados || {})[id];
  const vigente = sil && (!sil.hasta || sil.hasta >= hoyChile());
  return {
    objetivo: ((seg.objetivos || {})[id] || {}).precio,
    seguido: !!(seg.seguidos || {})[id],
    silenciado: vigente ? (sil.hasta || "siempre") : null,
  };
}

function panel(info, id, seg) {
  const st = estadoSeg(seg, id);
  const plat = info.plataforma === "Switch 2" ? "Switch 2" : "Switch 1";
  const l = [
    `⚙️ <b>ACCIONES</b>`,
    `<b>${esc(info.nombre)}</b>`,
    `🕹️ ${plat} · ${info.tipo === "fisico" ? "📦 Físico" : "🎮 Digital"} · ${esc(info.juego)}`,
    SEP,
    `💰 Precio actual: <b>${dineroDe(info, info.precio)}</b> en ${esc(info.tienda || "")}`,
  ];
  if (info.min) l.push(`📉 Mínimo visto: ${dineroDe(info, info.min)}`);
  l.push(st.objetivo != null
    ? `🎯 Objetivo: <b>${dineroDe(info, st.objetivo)}</b>` + (info.precio <= st.objetivo ? " · ¡ya lo alcanzó!" : ` · faltan ${dineroDe(info, info.precio - st.objetivo)}`)
    : "🎯 Objetivo: sin definir");
  l.push(st.seguido ? "⭐ Lo sigues: te aviso de cualquier baja" : "⭐ No lo sigues");
  l.push(st.silenciado ? `🔕 Silenciado${st.silenciado === "siempre" ? "" : " hasta el " + fecha(st.silenciado)}` : "🔔 Alertas activas");
  l.push(SEP, "<i>Los cambios se aplican desde la próxima revisión</i>");

  const kb = [];
  if (info.url) kb.push([{ text: `🛒 Ir a ${info.tienda || "la tienda"}`, url: info.url }]);
  kb.push([{ text: "📈 Ver tarjeta completa", callback_data: `v:${id}` }]);
  kb.push([10, 20, 30].map((p) => {
    const v = redondeo(info, info.precio * (1 - p / 100));
    return { text: `🎯 ${dineroDe(info, v)} (−${p}%)`, callback_data: `o:${id}:${p}` };
  }));
  const fila = [{ text: "✏️ Otro precio", callback_data: `oc:${id}` }];
  if (info.min && info.min < info.precio) fila.unshift({ text: `🎯 Mínimo ${dineroDe(info, info.min)}`, callback_data: `om:${id}` });
  if (st.objetivo != null) fila.push({ text: "🗑️ Sin objetivo", callback_data: `ox:${id}` });
  kb.push(fila);
  kb.push([st.seguido ? { text: "⭐ Dejar de seguir", callback_data: `s:${id}:0` } : { text: "⭐ Seguir este juego", callback_data: `s:${id}:1` }]);
  kb.push(st.silenciado
    ? [{ text: "🔔 Reactivar alertas", callback_data: `m:${id}:x` }]
    : [{ text: "🔕 7 días", callback_data: `m:${id}:7` }, { text: "🔕 30 días", callback_data: `m:${id}:30` }, { text: "🔕 Siempre", callback_data: `m:${id}:0` }]);
  return { text: l.join("\n"), reply_markup: { inline_keyboard: kb } };
}

async function infoDe(env, id) {
  const { data } = await leer(env, "vistas/ids.json");
  const info = data && data[id];
  if (!info) throw new Error("No encontré ese juego en la última revisión. Prueba de nuevo después de /revisar.");
  return info;
}

async function guardarSeg(env, msg, cambio) {
  return editarArchivo(env, "seguimiento.json", msg, cambio, SEG_VACIO);
}

// ------------------------------------------------------- revision del canal
// canal.py manda cada post primero a tu chat con estos botones. Tu decision queda en
// canal_decisiones.json y canal.py la aplica (Publicar ya lanza el workflow del canal al tiro).

const WORKFLOW_CANAL = "canal.yml";

async function botonCanal(env, cq, acc, cid, arg) {
  const chatId = cq.message.chat.id;
  const msgId = cq.message.message_id;
  const toast = (text, alerta = false) => tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text, show_alert: alerta });
  if (acc === "cx") return toast("");
  const { data: est } = await leer(env, "canal_estado.json");
  const c = est && est.cola && est.cola[cid];
  if (!c) {
    await tg(env, "editMessageReplyMarkup", { chat_id: chatId, message_id: msgId, reply_markup: { inline_keyboard: [] } });
    return toast("Este post ya no está en revisión (ya salió, se saltó o venció).", true);
  }
  const guardar = (msg, cambio) => editarArchivo(env, "canal_decisiones.json", msg, (d) => {
    for (const k of Object.keys(d)) if (!(est.cola || {})[k]) delete d[k];   // limpia decisiones viejas
    d[cid] ||= {};
    cambio(d[cid]);
  }, {});
  const marcar = (texto) => tg(env, "editMessageReplyMarkup", {
    chat_id: chatId, message_id: msgId, reply_markup: { inline_keyboard: [[{ text: texto, callback_data: `cx:${cid}` }]] },
  });
  const teclado = (quitar) => {
    const filas = c.ids.map((id, i) => [{
      text: `${quitar.includes(id) ? "↩️ Volver a poner" : "❌ Quitar"} ${i + 1}. ${String(c.nombres[i] || "").slice(0, 28)}`,
      callback_data: `cq:${cid}:${i}`,
    }]);
    filas.push([{ text: "✅ Publicar ya", callback_data: `cp:${cid}` }, { text: "⏭️ Saltar", callback_data: `cs:${cid}` }]);
    return tg(env, "editMessageReplyMarkup", { chat_id: chatId, message_id: msgId, reply_markup: { inline_keyboard: filas } });
  };

  if (acc === "cs") {
    await guardar("Canal: saltar post", (x) => { x.accion = "saltar"; });
    await marcar("⏭️ Saltado");
    return toast("⏭️ Listo, ese post no sale.");
  }
  if (acc === "cp") {
    await guardar("Canal: publicar post", (x) => { x.accion = "publicar"; });
    const r = await gh(env, `/repos/${env.GITHUB_REPO}/actions/workflows/${WORKFLOW_CANAL}/dispatches`, {
      method: "POST",
      body: JSON.stringify({ ref: "main", inputs: { slot: "cola", prueba: "false" } }),
    });
    await marcar(r.status === 204 ? "⏳ Publicando…" : "✅ Aprobado (sale en la próxima pasada)");
    return toast(r.status === 204 ? "✅ Sale al canal en 1 a 2 minutos." : "✅ Aprobado. GitHub no respondió: sale en la próxima pasada.");
  }
  if (acc === "ce") {
    const { data: dec } = await leer(env, "canal_decisiones.json");
    await toast("Toca los juegos que quieres sacar");
    return teclado(((dec || {})[cid] || {}).quitar || []);
  }
  if (acc === "cq") {
    const id = c.ids[Number(arg)];
    if (!id) return toast("No encontré ese juego.", true);
    const d = await guardar("Canal: quitar juego", (x) => {
      x.quitar ||= [];
      x.quitar = x.quitar.includes(id) ? x.quitar.filter((y) => y !== id) : [...x.quitar, id];
    });
    const quitar = d[cid].quitar || [];
    await toast(quitar.includes(id) ? "❌ Quitado del post" : "↩️ Vuelve al post");
    return teclado(quitar);
  }
  return toast("Botón desconocido", true);
}

async function manejarBoton(env, cq) {
  const [acc, id, arg] = String(cq.data || "").split(":");
  if (["cp", "cs", "ce", "cq", "cx"].includes(acc)) return botonCanal(env, cq, acc, id, arg);
  const chatId = cq.message.chat.id;
  const msgId = cq.message.message_id;
  const toast = (text) => tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text });
  const info = await infoDe(env, id);

  if (acc === "a") {                       // abrir panel en un mensaje nuevo
    await toast("");
    const { data: seg } = await leer(env, "seguimiento.json");
    return tg(env, "sendMessage", { chat_id: chatId, parse_mode: "HTML", disable_web_page_preview: true, ...panel(info, id, seg || SEG_VACIO) });
  }
  if (acc === "v") {                       // tarjeta completa
    await toast("");
    const { data } = await leer(env, `vistas/saga/${info.saga}.json`);
    const it = data && data.items.find((x) => x.id === id);
    return tg(env, "sendMessage", {
      chat_id: chatId, parse_mode: "HTML", disable_web_page_preview: true,
      text: it ? it.texto : "No encontré la tarjeta.",
      reply_markup: { inline_keyboard: [[{ text: "⚙️ Acciones", callback_data: `a:${id}` }]] },
    });
  }
  if (acc === "oc") {                      // objetivo escrito a mano
    await toast("");
    return tg(env, "sendMessage", {
      chat_id: chatId, parse_mode: "HTML",
      text: `✏️ <b>PRECIO OBJETIVO</b>\n${esc(info.nombre)}\nPrecio actual: ${dineroDe(info, info.precio)}\n\nResponde a este mensaje con el precio, por ejemplo ${info.moneda === "USD" ? "19.99" : "29990"}.\n<i>ref:${id}</i>`,
      reply_markup: { force_reply: true, input_field_placeholder: info.moneda === "USD" ? "19.99" : "29990" },
    });
  }

  let aviso = "";
  const seg = await guardarSeg(env, `Botón ${acc} · ${info.nombre}`, (s) => {
    s.objetivos ||= {}; s.seguidos ||= {}; s.silenciados ||= {};
    const base = { nombre: info.nombre, tipo: info.tipo, moneda: info.moneda, desde: hoyChile() };
    if (acc === "o" || acc === "om") {
      const v = acc === "om" ? info.min : redondeo(info, info.precio * (1 - Number(arg) / 100));
      s.objetivos[id] = { ...base, precio: v };
      aviso = `🎯 Te aviso cuando llegue a ${dineroDe(info, v)}`;
    } else if (acc === "ox") {
      delete s.objetivos[id];
      aviso = "🗑️ Objetivo quitado";
    } else if (acc === "s") {
      if (arg === "1") { s.seguidos[id] = base; aviso = "⭐ Ahora lo sigues"; } else { delete s.seguidos[id]; aviso = "Dejaste de seguirlo"; }
    } else if (acc === "m") {
      if (arg === "x") { delete s.silenciados[id]; aviso = "🔔 Alertas reactivadas"; } else {
        s.silenciados[id] = { ...base, hasta: arg === "0" ? null : hoyChile(Number(arg)) };
        aviso = arg === "0" ? "🔕 Silenciado para siempre" : `🔕 Silenciado ${arg} días`;
      }
    } else {
      throw new Error("Botón desconocido");
    }
  });
  await toast(aviso);
  return tg(env, "editMessageText", { chat_id: chatId, message_id: msgId, parse_mode: "HTML", disable_web_page_preview: true, ...panel(info, id, seg) });
}

async function objetivoEscrito(env, chatId, id, texto) {
  const info = await infoDe(env, id);
  const limpio = texto.replace(/[^\d.,]/g, "");
  const v = info.moneda === "USD" ? parseFloat(limpio.replace(",", ".")) : parseInt(limpio.replace(/[.,]/g, ""), 10);
  if (!(v > 0)) return send(env, chatId, marco("⚠️ <b>NO SE PUDO</b>", null, ["No entendí el precio. Escribe solo el número, por ejemplo 29990."]));
  const seg = await guardarSeg(env, `Objetivo ${info.nombre}`, (s) => {
    s.objetivos ||= {};
    s.objetivos[id] = { nombre: info.nombre, tipo: info.tipo, moneda: info.moneda, desde: hoyChile(), precio: v };
  });
  return tg(env, "sendMessage", { chat_id: chatId, parse_mode: "HTML", disable_web_page_preview: true, ...panel(info, id, seg) });
}

async function cmdSeguidos(env) {
  const { data: seg } = await leer(env, "seguimiento.json");
  const { data: ids } = await leer(env, "vistas/ids.json");
  const s = seg || SEG_VACIO;
  const titulo = "⭐ <b>TU SEGUIMIENTO</b>";
  const lista = (obj) => Object.entries(obj || {});
  const obj = lista(s.objetivos), seg_ = lista(s.seguidos);
  const sil = lista(s.silenciados).filter(([, v]) => !v.hasta || v.hasta >= hoyChile());
  if (!obj.length && !seg_.length && !sil.length) {
    return marco(titulo, null, ["Todavía no sigues ningún juego.",
      "En cualquier tarjeta toca ⚙️ para ponerle objetivo, seguirlo o silenciarlo."]);
  }
  const bloques = [marco(titulo, `${obj.length} con objetivo · ${seg_.length} seguido${seg_.length === 1 ? "" : "s"} · ${sil.length} silenciado${sil.length === 1 ? "" : "s"}`, [])];
  const fila = (id, v, extra) => {
    const info = (ids || {})[id];
    const precio = info ? dineroDe(info, info.precio) : "sin datos";
    return { junto: true, t: `• <b>${esc(v.nombre)}</b> — hoy ${precio}${extra(info)}`,
      boton: { text: `⚙️ ${v.nombre.length > 32 ? v.nombre.slice(0, 31) + "…" : v.nombre}`, callback_data: `a:${id}` } };
  };
  if (obj.length) {
    bloques.push("🎯 <b>CON OBJETIVO</b>");
    obj.sort((a, b) => ((ids || {})[a[0]]?.precio / a[1].precio || 9) - ((ids || {})[b[0]]?.precio / b[1].precio || 9));
    for (const [id, v] of obj) bloques.push(fila(id, v, (info) => {
      const meta = info ? dineroDe(info, v.precio) : v.precio;
      return info && info.precio <= v.precio ? ` · 🎯 ${meta} ✅ ¡lo alcanzó!` : ` · 🎯 ${meta}`;
    }));
  }
  if (seg_.length) {
    bloques.push("⭐ <b>SEGUIDOS</b>");
    for (const [id, v] of seg_) bloques.push(fila(id, v, (info) => info && info.min ? ` · mínimo ${dineroDe(info, info.min)}` : ""));
  }
  if (sil.length) {
    bloques.push("🔕 <b>SILENCIADOS</b>");
    for (const [id, v] of sil) bloques.push(fila(id, v, () => v.hasta ? ` · hasta el ${fecha(v.hasta)}` : " · para siempre"));
  }
  return { titulo, bloques };
}

// ----------------------------------------------------------------- GitHub

async function gh(env, path, opts = {}) {
  return fetch(GH + path, {
    ...opts,
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "switchbot-worker",
      ...(opts.body ? { "Content-Type": "application/json" } : {}),
      ...(opts.headers || {}),
    },
  });
}

async function leer(env, archivo) {
  const r = await gh(env, `/repos/${env.GITHUB_REPO}/contents/${archivo}`);
  if (r.status === 404) {
    if (archivo !== "juegos.json") return { data: null, sha: null };
    throw new Error(`No encontré ${archivo} en el repo.`);
  }
  if (!r.ok) throw new Error(`GitHub respondió ${r.status} al leer ${archivo}. Revisa GITHUB_TOKEN y GITHUB_REPO.`);
  const j = await r.json();
  let texto;
  if (j.content) {
    texto = b64decode(j.content.replace(/\n/g, ""));
  } else {
    const raw = await gh(env, `/repos/${env.GITHUB_REPO}/contents/${archivo}`, {
      headers: { Accept: "application/vnd.github.raw" },
    });
    texto = await raw.text();
  }
  return { data: JSON.parse(texto), sha: j.sha };
}

async function editar(env, mensaje, cambio) {
  return editarArchivo(env, "juegos.json", mensaje, cambio);
}

async function editarArchivo(env, archivo, mensaje, cambio, inicial = null) {
  for (let intento = 0; intento < 3; intento++) {
    let { data, sha } = await leer(env, archivo);
    if (!data) data = inicial ? JSON.parse(JSON.stringify(inicial)) : {};
    cambio(data);
    const r = await gh(env, `/repos/${env.GITHUB_REPO}/contents/${archivo}`, {
      method: "PUT",
      body: JSON.stringify({
        message: `${mensaje} (desde Telegram)`,
        content: b64encode(JSON.stringify(data, null, 2) + "\n"),
        ...(sha ? { sha } : {}),
      }),
    });
    if (r.ok) return data;
    if (r.status !== 409 && r.status !== 422) {
      throw new Error(`GitHub no guardó el cambio (${r.status}). Revisa los permisos del token.`);
    }
  }
  throw new Error("No pude guardar el cambio; inténtalo de nuevo en un minuto.");
}

// --------------------------------------------------------------- Telegram

async function tg(env, metodo, cuerpo) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/${metodo}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cuerpo),
  });
  return r.json().catch(() => ({ ok: false, description: `HTTP ${r.status}` }));
}

async function send(env, chatId, texto) {
  if (texto && typeof texto === "object") return enviarBloques(env, chatId, texto);
  let partes = trocear(texto);
  if (partes.length > 1) {
    const titulo = texto.split("\n")[0];
    const n = partes.length;
    partes = partes.map((p, i) => i === 0
      ? p.replace(titulo, `${titulo} <i>(1/${n})</i>`)
      : `${titulo} <i>(${i + 1}/${n})</i>\n\n${p}`);
  }
  for (const parte of partes) {
    const r = await tg(env, "sendMessage", {
      chat_id: chatId,
      text: parte,
      parse_mode: "HTML",
      disable_web_page_preview: true,
    });
    if (!r.ok) {
      // si el HTML falla, se manda como texto plano
      await tg(env, "sendMessage", { chat_id: chatId, text: parte.replace(/<[^>]+>/g, "") });
    }
  }
}

// mensajes con tarjetas: cada mensaje lleva los botones de sus tarjetas
async function enviarBloques(env, chatId, { titulo, bloques }) {
  const msgs = [];
  let actual = "", botones = [];
  const usados = new Set();
  for (const b of bloques) {
    const t = typeof b === "string" ? b : b.t;
    if (!t) continue;
    const sep = actual ? (typeof b === "object" && b.junto ? "\n" : "\n\n") : "";
    if (actual && actual.length + sep.length + t.length > TG_MAX) {
      msgs.push([actual, botones]);
      actual = t; botones = [];
    } else {
      actual += sep + t;
    }
    if (typeof b === "object" && b.boton && !usados.has(b.boton.callback_data)) {
      usados.add(b.boton.callback_data);
      botones.push(b.boton);
    }
  }
  if (actual) msgs.push([actual, botones]);
  // tope: decenas de mensajes seguidos hacen que Telegram bloquee al bot por horas
  const MAX_MSGS = 6;
  if (msgs.length > MAX_MSGS) {
    const resto = msgs.length - (MAX_MSGS - 1);
    msgs.splice(MAX_MSGS - 1, msgs.length, [`📦 <b>Y hay más</b>\n${SEP}\nNo te mando ${resto} mensajes más para que Telegram no bloquee al bot.\n` +
      "Afina la búsqueda: /ofertas compra · /ofertas fisico · /ofertas 50 · /precios <i>juego</i>", []]);
  }
  const n = msgs.length;
  for (let i = 0; i < n; i++) {
    let [m, bts] = msgs[i];
    if (n > 1) m = i === 0 ? m.replace(titulo, `${titulo} <i>(1/${n})</i>`) : `${titulo} <i>(${i + 1}/${n})</i>\n\n${m}`;
    if (bts.length) m += "\n\n<i>⚙️ Toca un juego para ponerle objetivo, seguirlo o silenciarlo</i>";
    const cuerpo = { chat_id: chatId, text: m, parse_mode: "HTML", disable_web_page_preview: true };
    if (bts.length) cuerpo.reply_markup = { inline_keyboard: bts.map((x) => [x]) };
    const r = await tg(env, "sendMessage", cuerpo);
    if (!r.ok) await tg(env, "sendMessage", { chat_id: chatId, text: m.replace(/<[^>]+>/g, "") });
  }
}

function trocear(texto) {
  // empaqueta por bloques (separados por linea en blanco) sin partir ninguno
  const bloques = texto.split("\n\n");
  const partes = [];
  let actual = "";
  for (let b of bloques) {
    while (b.length > TG_MAX) {           // bloque gigante: se corta por lineas
      const corte = b.lastIndexOf("\n", TG_MAX);
      const k = corte > 0 ? corte : TG_MAX;
      if (actual) { partes.push(actual); actual = ""; }
      partes.push(b.slice(0, k));
      b = b.slice(k).replace(/^\n/, "");
    }
    const sep = actual ? "\n\n" : "";
    if (actual && actual.length + sep.length + b.length > TG_MAX) {
      partes.push(actual);
      actual = b;
    } else {
      actual += sep + b;
    }
  }
  if (actual) partes.push(actual);
  return partes.filter((p) => p.trim());
}

function terminos(j) {
  const b = j.buscar || j.nombre || "";
  return Array.isArray(b) ? b : [b];
}

function norm(s) {
  return String(s || "")
    .replace(/[™®©]/g, " ")
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function escAttr(s) {
  return esc(s).replace(/"/g, "&quot;");
}

function clp(n) {
  return "$" + String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
}

function usd(n) {
  return "US$" + Number(n).toFixed(2);
}

function dinero(n, moneda) {
  return moneda === "CLP" ? clp(n) : usd(n);
}

function fecha(ymd) {
  const [y, m, d] = String(ymd).split("-").map(Number);
  const meses = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];
  return d && m ? `${d} ${meses[m - 1]}` + (y !== new Date().getFullYear() ? ` ${y}` : "") : String(ymd);
}

function hora(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("es-CL", {
      timeZone: "America/Santiago",
      day: "numeric",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function b64decode(b) {
  const bin = atob(b);
  const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

function b64encode(s) {
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(bin);
}
