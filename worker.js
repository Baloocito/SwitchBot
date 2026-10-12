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
const TG_MAX = 3900;

const AYUDA = `👑 <b>Comandos de SwitchBot</b>

<b>Precios</b>
/precios — lo más barato de cada juego
/precios <i>juego</i> — todos los precios de un juego
/ofertas — todo lo que hoy tiene descuento
/ofertas <i>%</i> — solo descuentos de ese % o más
/revisar — revisar precios ahora (tarda 2–4 min)

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
/baja <i>%</i> — baja mínima entre revisiones`;

const COMANDOS_MENU = [
  ["precios", "Precios actuales (opcional: juego)"],
  ["ofertas", "Juegos con descuento hoy"],
  ["revisar", "Revisar precios ahora"],
  ["config", "Ver la configuración"],
  ["agregar", "Agregar un juego"],
  ["quitar", "Quitar un juego"],
  ["objetivo", "Precio objetivo de un juego"],
  ["descuento", "Descuento mínimo para avisar"],
  ["baja", "Baja mínima entre revisiones"],
  ["tienda", "Agregar una tienda"],
  ["quitartienda", "Quitar una tienda"],
  ["ayuda", "Lista de comandos"],
];

// ------------------------------------------------------------------ entrada

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/setup") {
      if (!env.WEBHOOK_SECRET || url.searchParams.get("clave") !== env.WEBHOOK_SECRET) {
        return new Response("Clave incorrecta.", { status: 403 });
      }
      return new Response(await setup(env, url.origin), {
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }
    if (request.method !== "POST") return new Response("SwitchBot activo.");

    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("No autorizado.", { status: 403 });
    }
    const update = await request.json().catch(() => null);
    const msg = update && (update.message || update.edited_message);
    if (!msg || typeof msg.text !== "string") return new Response("ok");

    const chatId = String(msg.chat.id);
    if (chatId !== String(env.CHAT_ID).trim()) {
      await send(env, chatId, `Este bot es privado.\nTu chat ID es <code>${chatId}</code>.`);
      return new Response("ok");
    }
    try {
      await manejar(env, chatId, msg.text.trim());
    } catch (e) {
      await send(env, chatId, "⚠️ " + esc(e.message || String(e)));
    }
    return new Response("ok");
  },
};

async function setup(env, origin) {
  const faltan = ["TELEGRAM_TOKEN", "CHAT_ID", "GITHUB_TOKEN", "GITHUB_REPO", "WEBHOOK_SECRET"]
    .filter((k) => !env[k]);
  if (faltan.length) return "Faltan estas variables en el Worker: " + faltan.join(", ");
  if (!/^[A-Za-z0-9_-]{8,256}$/.test(env.WEBHOOK_SECRET)) {
    return "WEBHOOK_SECRET debe tener al menos 8 caracteres y solo letras, números, _ o -.";
  }
  const lineas = [];
  const wh = await tg(env, "setWebhook", {
    url: origin + "/",
    secret_token: env.WEBHOOK_SECRET,
    allowed_updates: ["message", "edited_message"],
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
  lineas.push("Mensaje de prueba a tu chat: " + (hola.ok ? "OK" : "ERROR " + hola.description));
  return lineas.join("\n");
}

// --------------------------------------------------------------- comandos

async function manejar(env, chatId, texto) {
  const m = texto.match(/^\/([a-zA-Z_]+)(?:@\S+)?\s*([\s\S]*)$/);
  if (!m) {
    return send(env, chatId, "Escribe /ayuda para ver los comandos.");
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
    case "precios":
      return send(env, chatId, await cmdPrecios(env, args));
    case "ofertas":
      return send(env, chatId, await cmdOfertas(env, args));
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
    case "tienda":
      return send(env, chatId, await cmdTienda(env, args));
    case "quitartienda":
      return send(env, chatId, await cmdQuitarTienda(env, args));
    default:
      return send(env, chatId, "No conozco ese comando. Escribe /ayuda.");
  }
}

async function cmdConfig(env) {
  const { data: cfg } = await leer(env, "juegos.json");
  const { data: st } = await leer(env, "state.json");
  const aj = cfg.ajustes || {};
  const l = [
    "⚙️ <b>Configuración</b>",
    `Descuento mínimo: <b>${aj.descuento_minimo_pct ?? 25}%</b>`,
    `Baja mínima entre revisiones: <b>${aj.baja_minima_pct ?? 10}%</b>`,
    `eShop: <b>${esc(aj.pais_eshop || "US")}</b>`,
    "",
    `🎮 <b>Juegos (${(cfg.juegos || []).length})</b>`,
  ];
  for (const j of cfg.juegos || []) {
    const t = terminos(j);
    let linea = `• ${esc(j.nombre)}`;
    if (t.length !== 1 || norm(t[0]) !== norm(j.nombre)) linea += ` <i>(busca: ${esc(t.join(", "))})</i>`;
    const obj = [];
    if (j.objetivo_clp != null) obj.push(clp(j.objetivo_clp));
    if (j.objetivo_usd != null) obj.push(usd(j.objetivo_usd));
    if (obj.length) linea += ` 🎯 ${obj.join(" / ")}`;
    l.push(linea);
  }
  l.push("", `🏬 <b>Tiendas (${(cfg.tiendas || []).length})</b>`);
  const fallos = (st && st.fallos) || {};
  for (const t of cfg.tiendas || []) {
    const estado = fallos[t.nombre] ? ` ⚠️ ${fallos[t.nombre]} revisiones seguidas sin poder leerla` : "";
    l.push(`• ${esc(t.nombre)} — ${esc(t.url.replace(/^https?:\/\/(www\.)?/, ""))}${estado}`);
  }
  if (st && st.ultima_revision) l.push("", `🕒 Última revisión: ${hora(st.ultima_revision)}`);
  return l.join("\n");
}

function itemsVigentes(st) {
  const out = [];
  if (!st) return out;
  const corte = st.ultima_revision
    ? new Date(new Date(st.ultima_revision).getTime() - 12 * 3600e3).toISOString()
    : "";
  for (const grupo of ["eshop", "tiendas"]) {
    for (const it of Object.values(st[grupo] || {})) {
      if (!it.titulo || it.ultimo == null) continue;
      if (corte && (it.visto || "") < corte.slice(0, 19)) continue;
      out.push(it);
    }
  }
  return out;
}

function lineaItem(it, conJuego = false) {
  const pct = it.normal && it.normal > it.ultimo ? Math.round(100 * (1 - it.ultimo / it.normal)) : 0;
  let s = `<a href="${escAttr(it.url)}">${esc(it.titulo)}</a> · ${esc(it.origen)}\n   ${dinero(it.ultimo, it.moneda)}`;
  if (pct > 0) s += `  <s>${dinero(it.normal, it.moneda)}</s>  −${pct}%`;
  if (it.fin && pct > 0) s += `  ⏳ ${it.fin.slice(0, 10)}`;
  if (conJuego) s = `<i>${esc(it.juego)}</i> · ` + s;
  return s;
}

async function cmdPrecios(env, args) {
  const { data: st } = await leer(env, "state.json");
  const items = itemsVigentes(st);
  if (!items.length) return "Todavía no hay precios guardados. Usa /revisar y vuelve en unos minutos.";
  const pie = `\n\n🕒 Precios de la revisión del ${hora(st.ultima_revision)}`;

  if (args) {
    const q = norm(args);
    const sel = items.filter((it) => norm(it.juego) === q)
      .concat(items.filter((it) => norm(it.juego) !== q && norm(it.titulo).includes(q)));
    if (!sel.length) return `No encontré precios para "${esc(args)}". Revisa el nombre con /config.`;
    const fis = sel.filter((it) => it.moneda === "CLP").sort((a, b) => a.ultimo - b.ultimo);
    const dig = sel.filter((it) => it.moneda !== "CLP").sort((a, b) => a.ultimo - b.ultimo);
    const l = [`💰 <b>${esc(args)}</b> — ${sel.length} precios`];
    if (fis.length) l.push("", "📦 <b>Físico</b>", ...fis.map((it) => lineaItem(it)));
    if (dig.length) l.push("", "🎮 <b>Digital</b>", ...dig.map((it) => lineaItem(it)));
    return l.join("\n") + pie;
  }

  const porJuego = {};
  for (const it of items) (porJuego[it.juego] ||= []).push(it);
  const l = ["💰 <b>Lo más barato de cada juego</b>", "<i>Para ver todo: /precios nombre</i>"];
  for (const juego of Object.keys(porJuego).sort((a, b) => a.localeCompare(b, "es"))) {
    const g = porJuego[juego];
    l.push("", `<b>${esc(juego)}</b> (${g.length})`);
    const fis = g.filter((it) => it.moneda === "CLP").sort((a, b) => a.ultimo - b.ultimo)[0];
    const dig = g.filter((it) => it.moneda !== "CLP").sort((a, b) => a.ultimo - b.ultimo)[0];
    if (fis) l.push("📦 " + lineaItem(fis));
    if (dig) l.push("🎮 " + lineaItem(dig));
  }
  return l.join("\n") + pie;
}

async function cmdOfertas(env, args) {
  const { data: st } = await leer(env, "state.json");
  const min = args ? parseFloat(args.replace(/[^\d.]/g, "")) || 1 : 1;
  const items = itemsVigentes(st)
    .filter((it) => it.normal && it.normal > it.ultimo)
    .map((it) => ({ ...it, pct: 100 * (1 - it.ultimo / it.normal) }))
    .filter((it) => it.pct >= min)
    .sort((a, b) => b.pct - a.pct);
  if (!items.length) {
    return `No hay juegos con ${min > 1 ? min + "% o más de " : ""}descuento en la última revisión.`;
  }
  const l = [`🔥 <b>Ofertas${min > 1 ? ` de ${min}% o más` : ""}</b> — ${items.length}`];
  for (const it of items.slice(0, 60)) l.push(lineaItem(it, true));
  if (items.length > 60) l.push(`… y ${items.length - 60} más. Usa /ofertas 30 para filtrar.`);
  l.push("", `🕒 Revisión del ${hora(st.ultima_revision)}`);
  return l.join("\n");
}

async function cmdRevisar(env) {
  const r = await gh(env, `/repos/${env.GITHUB_REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    body: JSON.stringify({ ref: "main", inputs: { modo: "normal" } }),
  });
  if (r.status !== 204) throw new Error(`GitHub no inició la revisión (${r.status}): ${(await r.text()).slice(0, 150)}`);
  return "🔎 Revisión iniciada. En 2–4 minutos te llegan las ofertas nuevas, si hay.\nDespués puedes ver todo con /precios u /ofertas.";
}

async function cmdAgregar(env, args) {
  if (!args) return "Uso: /agregar Kirby\no: /agregar Final Fantasy | final fantasy, ff";
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
  return `✅ Agregado <b>${esc(nombre)}</b> (busca: ${esc(buscar.join(", "))}).\nSe incluye desde la próxima revisión, o usa /revisar.`;
}

async function cmdQuitar(env, args) {
  if (!args) return "Uso: /quitar Kirby";
  let quitado;
  await editar(env, `Quita juego ${args}`, (cfg) => {
    const i = (cfg.juegos || []).findIndex((j) => norm(j.nombre) === norm(args));
    if (i < 0) throw new Error(`No encontré "${args}". Revisa el nombre con /config.`);
    quitado = cfg.juegos.splice(i, 1)[0].nombre;
  });
  return `🗑️ Quitado <b>${esc(quitado)}</b>.`;
}

async function cmdObjetivo(env, args) {
  const uso = "Uso:\n/objetivo Zelda 30000 (pesos, físico)\n/objetivo Zelda 29.99 usd (eShop)\n/objetivo Zelda quitar";
  if (!args) return uso;
  let resp;
  const quitar = args.match(/^(.+?)\s+(quitar|borrar|no)$/i);
  if (quitar) {
    await editar(env, `Quita objetivo ${quitar[1]}`, (cfg) => {
      const j = buscarJuego(cfg, quitar[1]);
      delete j.objetivo_clp;
      delete j.objetivo_usd;
      resp = `🎯 Sin precio objetivo para <b>${esc(j.nombre)}</b>.`;
    });
    return resp;
  }
  const m = args.match(/^(.+?)\s+(us\$|\$)?\s*([\d.,]+)\s*(usd|us\$|dolares|dólares|clp|pesos)?$/i);
  if (!m) return uso;
  const nombre = m[1].trim();
  const numero = m[3];
  const marca = ((m[2] || "") + (m[4] || "")).toLowerCase();
  const esUsd = /us|dol|dól/.test(marca) || (!/clp|peso/.test(marca) && /^\d+[.,]\d{1,2}$/.test(numero));
  const valor = esUsd
    ? parseFloat(numero.replace(",", "."))
    : parseInt(numero.replace(/[.,]/g, ""), 10);
  if (!(valor > 0)) return uso;
  await editar(env, `Objetivo ${nombre}`, (cfg) => {
    const j = buscarJuego(cfg, nombre);
    j[esUsd ? "objetivo_usd" : "objetivo_clp"] = valor;
    resp = `🎯 <b>${esc(j.nombre)}</b>: te aviso cuando ${esUsd ? "en la eShop" : "en tiendas"} llegue a <b>${esUsd ? usd(valor) : clp(valor)}</b> o menos.`;
  });
  return resp;
}

async function cmdAjuste(env, args, clave, etiqueta) {
  const n = parseFloat((args || "").replace(/[^\d.]/g, ""));
  if (!(n >= 1 && n <= 95)) return `Uso: /${clave.startsWith("desc") ? "descuento" : "baja"} 30  (entre 1 y 95)`;
  await editar(env, `${etiqueta} ${n}%`, (cfg) => {
    cfg.ajustes ||= {};
    cfg.ajustes[clave] = n;
  });
  return `✅ ${etiqueta}: <b>${n}%</b>. Se aplica desde la próxima revisión.`;
}

async function cmdTienda(env, args) {
  const m = (args || "").match(/^(.+?)\s+(https?:\/\/\S+)$/i);
  if (!m) return "Uso: /tienda Zmart https://www.zmart.cl";
  const nombre = m[1].trim();
  const url = m[2].replace(/\/+$/, "");
  await editar(env, `Agrega tienda ${nombre}`, (cfg) => {
    cfg.tiendas ||= [];
    if (cfg.tiendas.some((t) => norm(t.nombre) === norm(nombre))) {
      throw new Error(`La tienda "${nombre}" ya está.`);
    }
    cfg.tiendas.push({ nombre, url });
  });
  return `✅ Tienda <b>${esc(nombre)}</b> agregada. En la próxima revisión el bot prueba cómo leerla; si no puede, te avisa.`;
}

async function cmdQuitarTienda(env, args) {
  if (!args) return "Uso: /quitartienda Zmart";
  let quitada;
  await editar(env, `Quita tienda ${args}`, (cfg) => {
    const i = (cfg.tiendas || []).findIndex((t) => norm(t.nombre) === norm(args));
    if (i < 0) throw new Error(`No encontré la tienda "${args}". Revisa con /config.`);
    quitada = cfg.tiendas.splice(i, 1)[0].nombre;
  });
  return `🗑️ Tienda <b>${esc(quitada)}</b> quitada.`;
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
    if (archivo === "state.json") return { data: null, sha: null };
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
  for (let intento = 0; intento < 3; intento++) {
    const { data, sha } = await leer(env, "juegos.json");
    cambio(data);
    const r = await gh(env, `/repos/${env.GITHUB_REPO}/contents/juegos.json`, {
      method: "PUT",
      body: JSON.stringify({
        message: `${mensaje} (desde Telegram)`,
        content: b64encode(JSON.stringify(data, null, 2) + "\n"),
        sha,
      }),
    });
    if (r.ok) return;
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
  for (const parte of trocear(texto)) {
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

function trocear(texto) {
  const partes = [];
  let actual = "";
  for (const linea of texto.split("\n")) {
    if (actual && actual.length + linea.length + 1 > TG_MAX) {
      partes.push(actual);
      actual = linea;
    } else {
      actual = actual ? actual + "\n" + linea : linea;
    }
  }
  if (actual) partes.push(actual);
  return partes;
}

// ---------------------------------------------------------------- formato

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
