#!/usr/bin/env python3
"""
Canal de ofertas: cura lo que recolecta bot.py y lo publica en el canal de Telegram
a horas fijas, sin repetir y sin llenar el chat.

bot.py no cambia: sigue mandandote TODO a ti. Este archivo solo lee state.json,
history.json y canal.json, y guarda lo publicado en canal_estado.json.

Uso:
    python canal.py --auto              publica lo que toque segun la hora de Chile (lo usa canal.yml)
    python canal.py --slot top          fuerza un formato: top, pack, fisicos, ultima, semanal
    python canal.py --bombazos          publica bombazos pendientes (lo usa alertas.yml)
    python canal.py --vista             muestra todo lo que publicaria hoy, sin mandar nada
    python canal.py --sugerir           editoras candidatas a la lista de basura

Variables de entorno:
    TELEGRAM_TOKEN, TELEGRAM_CHANNEL_ID
    CANAL_PRUEBA=1   manda a tu chat personal (TELEGRAM_CHAT_ID) en vez del canal
    DRY_RUN=1        no manda nada, solo imprime
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

from bot import (BASE, MIN_DIAS, STATE_FILE, TIMEOUT, clp, dias_para, esc, esc_attr,
                 fecha_corta, load_json, log, norm, save_json, url_pais)

CANAL_FILE = BASE / "canal.json"
ESTADO_FILE = BASE / "canal_estado.json"
OTROS = "otros juegos"
DIAS = ["lun", "mar", "mie", "jue", "vie", "sab", "dom"]
VENTANA_MIN = 90            # minutos despues de la hora del slot en que todavia se publica
SIN_ENVIO = object()

try:
    from zoneinfo import ZoneInfo
except ImportError:          # pragma: no cover
    ZoneInfo = None


# ------------------------------------------------------------------ utilidades

def ahora(cfg):
    if ZoneInfo:
        return datetime.now(ZoneInfo(cfg["ajustes"].get("zona_horaria", "America/Santiago")))
    return datetime.now(timezone.utc) - timedelta(hours=3)


def hhmm(txt):
    h, m = txt.split(":")
    return int(h), int(m)


def en_silencio(cfg, t):
    a = cfg["ajustes"]
    desde, hasta = hhmm(a["silencio_desde"]), hhmm(a["silencio_hasta"])
    x = (t.hour, t.minute)
    return x >= desde or x < hasta if desde > hasta else desde <= x < hasta


def contiene(titulo_norm, frase):
    """frase completa dentro del titulo, respetando palabras ('poker' no calza con 'pokemon')."""
    f = norm(frase)
    return bool(f) and re.search(r"(^| )" + re.escape(f) + r"( |$)", titulo_norm) is not None


def calza_lista(titulo_norm, lista):
    for x in lista:
        if x.startswith("="):
            if titulo_norm == norm(x[1:]):
                return x[1:]
        elif contiene(titulo_norm, x):
            return x
    return None


# lo que puede venir despues del nombre de una joya sin que sea otro juego
# ("Dead Cells: Return to Castlevania Edition" si; "Hades Uprising" no)
SUFIJOS_OK = {"2", "3", "4", "ii", "iii", "iv", "edition", "deluxe", "definitive", "complete",
              "collection", "switch", "nintendo", "goty", "game", "of", "the", "year", "anniversary",
              "remastered", "remaster", "hd", "plus", "ultimate", "special", "enhanced", "directors",
              "cut", "bundle", "remake", "royal", "dx", "s", "for", "x", "and", "return", "to",
              "castlevania", "trilogy", "standard", "digital", "full", "gold", "premium", "edicion"}


def es_joya(titulo_norm, joyas):
    for x in joyas:
        exacto = x.startswith("=")
        f = norm(x[1:] if exacto else x)
        if not f:
            continue
        if titulo_norm == f:
            return x
        if not exacto and titulo_norm.startswith(f + " "):
            if all(w in SUFIJOS_OK for w in titulo_norm[len(f) + 1:].split()):
                return x
    return None


def redondear_clp(n):
    return int(round(n / 10.0)) * 10


VACIAS = {"the", "a", "an", "of", "el", "la", "los", "las", "de", "super", "new"}


def saga_valida(saga, titulo_norm):
    """El titulo debe nombrar la saga: 'Pokemon Mystery Dungeon / Snap' exige 'pokemon' o 'snap'
    (asi no entra 'The Strongest Job... Dungeon and Mystery Girl')."""
    for parte in saga.split("/"):
        clave = next((w for w in norm(parte).split() if w not in VACIAS), None)
        if clave and contiene(titulo_norm, clave):
            return True
    return False


def saga_de(juego):
    j = norm(re.sub(r"^\W+", "", juego or ""))
    return None if not j or j == OTROS else juego


# ------------------------------------------------------------ candidatos

def candidatos(cfg, state, incluir_excluidos=False):
    """Todas las ofertas vigentes, ya puntuadas: digitales (eShop Chile) y fisicos."""
    a = cfg["ajustes"]
    rate = ((state.get("fx") or {}).get("rates") or {}).get("CLP")
    revision = state.get("ultima_revision") or ""
    corte = ""
    if revision:
        corte = (datetime.fromisoformat(revision.replace("Z", "+00:00")) - timedelta(hours=12)
                 ).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    # fisico mas barato de cada juego (tiendas reales), para comparar con el digital
    fisicos = {}
    for s in (state.get("productos") or {}).values():
        if not s.get("nsuid") or (s.get("visto") or "") < corte:
            continue
        for av in s.get("avisos") or []:
            if av.get("externo"):
                continue
            prev = fisicos.get(s["nsuid"])
            if not prev or av["precio"] < prev["precio"]:
                fisicos[s["nsuid"]] = {"precio": av["precio"], "tienda": av["tienda"], "url": av["url"]}

    for nsuid, e in (state.get("eshop") or {}).items():
        if (e.get("visto") or "") < corte or e.get("ultimo") is None or not rate:
            continue
        d = dias_para(e["fin"]) if e.get("fin") else None
        if d is not None and d <= 0.02:
            continue                      # la oferta ya termino (o termina en minutos)
        regs = e.get("regiones") or {}
        if "CL" not in regs:
            continue                      # no se vende en la eShop de Chile
        regs_n = e.get("regiones_normal") or {}
        if e.get("pais") == "CL" and e.get("moneda_local") == "CLP" and e.get("local"):
            precio = redondear_clp(e["local"])
        else:
            precio = redondear_clp(regs["CL"] * rate)
        if "CL" in regs_n:
            normal_usd = regs_n["CL"]
        elif e.get("pais") == "CL" and e.get("normal"):
            normal_usd = e["normal"]
        elif e.get("pct_oferta") and regs["CL"] <= e["ultimo"] * 1.10:
            # datos antiguos sin precio normal por pais: Chile esta cerca del mas barato,
            # asi que se asume la misma rebaja
            normal_usd = regs["CL"] / (1 - e["pct_oferta"] / 100)
        else:
            normal_usd = regs["CL"]
        normal = redondear_clp(normal_usd * rate)
        pct = max(0.0, 100 * (1 - precio / normal)) if normal else 0.0
        if pct < 5:
            continue
        ext = None
        for r in a.get("regiones_saldo") or []:
            if r in regs and regs[r] <= regs["CL"] * (1 - a["ventaja_saldo_pct"] / 100):
                ext = {"pais": r, "clp": redondear_clp(regs[r] * rate),
                       "ahorro": round(100 * (1 - regs[r] / regs["CL"]))}
        out.append({
            "id": e.get("id") or "d" + nsuid, "tipo": "digital", "nombre": e["titulo"],
            "saga": saga_de(e.get("juego")), "plataforma": e.get("plataforma"),
            "editora": e.get("editora"), "url": url_pais(e.get("url"), "CL"),
            "precio": precio, "normal": normal, "pct": pct, "normal_usd": normal_usd,
            "fin": e.get("fin"), "ext": ext, "ahorro": normal - precio, "fisico": fisicos.get(nsuid),
            "minimo": (e.get("dias") or 0) >= MIN_DIAS and e.get("min_hist") is not None
            and e["ultimo"] <= e["min_hist"],
        })

    f = cfg["fisicos"]
    for clave, s in (state.get("productos") or {}).items():
        if (s.get("visto") or "") < corte:
            continue
        avisos = [x for x in (s.get("avisos") or [])
                  if not (f["excluir_vendedores_externos"] and x.get("externo"))]
        pct = s.get("pct") or 0
        if (s.get("externo") and f["excluir_vendedores_externos"]) or pct < f["pct_min"]:
            continue
        if s.get("ref_tipo") not in ("tipico", "mercado"):
            continue                      # descuento sin confirmar con otra tienda ni historial
        if s.get("ref_tipo") == "mercado":
            # la comparacion tiene que ser contra tiendas reales, no contra revendedores
            if len(avisos) < f["min_tiendas"]:
                continue
            if len(avisos) == 2 and pct > f["pct_max_con_2_tiendas"]:
                continue                  # probablemente la otra tienda tiene otra edicion
        e = (state.get("eshop") or {}).get(s.get("nsuid") or "") or {}
        out.append({
            "id": s.get("id") or "f" + clave, "tipo": "fisico", "nombre": s["nombre"],
            "saga": saga_de(s.get("juego")), "plataforma": s.get("plataforma"),
            "editora": e.get("editora"), "url": s.get("url"), "tienda": s.get("tienda"),
            "precio": s["precio"], "normal": s.get("ref"), "pct": pct, "avisos": avisos,
            "ahorro": (s.get("ref") or s["precio"]) - s["precio"],
            "normal_usd": None, "fin": None, "ext": None,
            "minimo": (s.get("dias") or 0) >= MIN_DIAS and s.get("min_hist") is not None
            and s["precio"] <= s["min_hist"],
        })

    for o in out:
        puntuar(cfg, o)
    return out if incluir_excluidos else [o for o in out if not o.get("excluido")]


def puntuar(cfg, o):
    p, b, ed = cfg["puntaje"], cfg["basura"], cfg["editoras"]
    t = norm(o["nombre"])
    senales = []
    estrellas = {norm(x) for x in cfg["sagas_estrella"]}
    # una "saga" barata suele ser otro juego que solo comparte una palabra ("Doom & Destiny")
    saga_ok = (o["saga"] and saga_valida(o["saga"], t)
               and (o["tipo"] == "fisico" or o["normal_usd"] >= p["saga_precio_min_usd"]))
    if not saga_ok:
        o["saga"] = None
    if saga_ok:
        if norm(o["saga"]) in estrellas:
            senales.append(("saga_estrella", p["saga_estrella"]))
        else:
            senales.append(("saga", p["saga"]))
    if es_joya(t, cfg["joyas"]):
        senales.append(("joya", p["joya"]))
    edit = norm(o.get("editora") or "")
    if edit:
        if any(contiene(edit, x) for x in ed.get("basura") or []):
            o["excluido"] = "editora basura"
            return
        if any(contiene(edit, x) for x in ed["a"]):
            senales.append(("editora_a", p["editora_a"]))
        elif any(contiene(edit, x) for x in ed["b"]):
            senales.append(("editora_b", p["editora_b"]))
    o["senales"] = [s for s, _ in senales]
    rel = max((v for _, v in senales), default=0) + p["senal_extra"] * max(0, len(senales) - 1)

    # basura
    if calza_lista(t, b["titulo_excluir"]):
        o["excluido"] = "titulo"
        return
    if not rel and any(re.search(rx, t) for rx in b["titulo_regex_excluir"]):
        o["excluido"] = "titulo con año"
        return
    if o["tipo"] == "digital" and not rel:
        if o["normal_usd"] < b["precio_normal_min_usd"]:
            o["excluido"] = "precio normal muy bajo"
            return
        inf = b["descuento_inflado"]
        if o["pct"] >= inf["pct_min"] and o["normal_usd"] >= inf["normal_min_usd"]:
            o["excluido"] = "descuento inflado"
            return

    score = rel + min(p["descuento_max"], o["pct"] * p["descuento_por_punto"])
    score += min(p["ahorro_max"], o["ahorro"] / 1000 * p["ahorro_por_mil_clp"])
    if o["minimo"]:
        score += p["minimo_historico"]
    d = dias_para(o["fin"]) if o["fin"] else None
    o["dias"] = d
    if d is not None and d <= 1.5:
        score += p["termina_pronto"]
    if not rel and calza_lista(t, b["titulo_sospechoso"]):
        score += p["titulo_sospechoso"]
    if o["tipo"] == "digital" and not rel and o["normal_usd"] < 10:
        score += p["barato_sin_relevancia"]
    o["score"] = max(0, min(100, round(score)))
    a = cfg["ajustes"]
    o["nivel"] = ("bombazo" if o["score"] >= a["umbral_bombazo"] and rel
                  else "destacado" if o["score"] >= a["umbral_destacado"] else "resto")


# --------------------------------------------------------------- estado

def cargar_estado():
    return load_json(ESTADO_FILE, {"publicados": {}, "ultima_llamada": {}, "dias": {}, "posts": []})


def dia_de(est, t):
    hoy = t.strftime("%Y-%m-%d")
    d = est["dias"].setdefault(hoy, {"posts": 0, "bombazos": 0, "slots": []})
    for k in sorted(est["dias"])[:-14]:
        est["dias"].pop(k)
    return d


def ya_publicado(cfg, est, o, t, grupo="publicados"):
    p = est[grupo].get(o["id"])
    if not p:
        return False
    dias = (t.date() - datetime.strptime(p["fecha"], "%Y-%m-%d").date()).days
    baja = o["precio"] < p["precio"] * (1 - cfg["ajustes"]["repetir_si_baja_pct"] / 100)
    return dias < cfg["ajustes"]["no_repetir_dias"] and not baja


def marcar(est, items, t, slot, grupo="publicados"):
    for o in items:
        est[grupo][o["id"]] = {"fecha": t.strftime("%Y-%m-%d"), "precio": o["precio"], "slot": slot}
    limite = (t - timedelta(days=30)).strftime("%Y-%m-%d")
    for g in ("publicados", "ultima_llamada"):
        est[g] = {k: v for k, v in est[g].items() if v["fecha"] >= limite}


# --------------------------------------------------------------- formato

MEDALLAS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣"]
DIAS_TXT = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
LINEA = "━━━━━━━━━━━━━━"


def corto(nombre, n=48):
    return nombre if len(nombre) <= n else nombre[:n - 1].rstrip(" :-–") + "…"


def precio_txt(o):
    return clp(o["precio"])


def plat(o):
    return " (S2)" if o.get("plataforma") == "Switch 2" else ""


def fecha_txt(t):
    return f"{DIAS_TXT[t.weekday()]} {fecha_corta(t.strftime('%Y-%m-%d'))}"


def etiqueta(cfg, o):
    if o["minimo"]:
        return " 📉"
    if o.get("dias") is not None and o["dias"] <= 1:
        return " ⏳"
    if o["tipo"] == "digital" and o["precio"] < cfg["ajustes"]["regalado_bajo_clp"]:
        return " 💸"
    if o["nivel"] == "bombazo":
        return " 🔥"
    return ""


def quedan(o):
    """'⏳ Termina en 5 h' / '⏳ Hasta el 7 nov' (fecha en que termina la oferta)."""
    d = o.get("dias")
    if d is None:
        return None
    if d < 1:
        horas = max(1, round(d * 24))
        return f"⏳ Termina en {horas} h"
    if d < 2:
        return "⏳ Termina mañana"
    return f"⏳ Hasta el {fecha_corta(o['fin'])}"


def veredicto(o):
    if o["minimo"]:
        return "Nunca había estado tan barato."
    if o.get("dias") is not None and o["dias"] < 1:
        return "Último día: después vuelve a su precio."
    if o["pct"] >= 70:
        return "Precio pa' no pensarlo."
    if "saga_estrella" in o.get("senales", []):
        return "Si lo estabas esperando, es ahora."
    if "joya" in o.get("senales", []):
        return "Joya: si no lo has jugado, este es el momento."
    return "Buen momento para comprarlo."


def mejor_opcion(o):
    """Donde conviene comprarlo: (texto, url). Compara digital vs fisico si hay ambos."""
    f = o.get("fisico")
    if o["tipo"] == "digital" and f and f["precio"] < o["precio"]:
        return f"📦 {f['tienda']}", f["url"]
    if o["tipo"] == "digital":
        return "🎮 eShop", o["url"]
    return f"📦 {o['tienda']}", o["url"]


def bloque(cfg, o, medalla):
    """Tarjeta corta de un juego para las listas: nombre, precio, contexto y comparacion."""
    tachado = f" <s>{clp(o['normal'])}</s>" if o.get("normal") and o["normal"] > o["precio"] else ""
    donde = f" en {esc(o['tienda'])}" if o["tipo"] == "fisico" else ""
    l = [f"{medalla} <b>{esc(corto(o['nombre'], 44))}</b>{plat(o)}",
         f"💰 <b>{precio_txt(o)}</b>{tachado} · −{round(o['pct'])}%{donde}"]
    extra = []
    if o["minimo"]:
        extra.append("📉 mínimo histórico")
    if quedan(o):
        extra.append(quedan(o)[2:].lower() if extra else quedan(o))
    if extra:
        l.append(" · ".join(extra))
    f = o.get("fisico")
    if o["tipo"] == "digital" and f:
        dif = o["precio"] - f["precio"]
        if dif > 0:
            l.append(f"📦 Físico más barato: <b>{clp(f['precio'])}</b> en {esc(f['tienda'])} (−{clp(dif)})")
        else:
            l.append(f"📦 Físico: {clp(f['precio'])} en {esc(f['tienda'])}")
    elif o["tipo"] == "fisico" and len(o.get("avisos") or []) > 1:
        sig = o["avisos"][1]
        l.append(f"🏬 Le sigue {esc(sig['tienda'])} a {clp(sig['precio'])}")
    return "\n".join(l)


def boton(texto, url):
    return {"text": texto, "url": url}


def botones_juegos(items):
    """Un boton por juego, directo a donde conviene comprarlo."""
    filas = []
    for i, o in enumerate(items):
        donde, url = mejor_opcion(o)
        filas.append([boton(f"{MEDALLAS[i]} {corto(o['nombre'], 26)} · {donde}", url)])
    return filas


def pie(cfg):
    a = cfg["ajustes"]
    fila = []
    if a.get("web_url"):
        fila.append(boton("📚 Todas las ofertas", a["web_url"]))
    if a.get("canal_url"):
        texto = quote("Ofertas de Switch en Chile, filtradas y en pesos 🎮")
        fila.append(boton("📣 Invitar a un amigo",
                          f"https://t.me/share/url?url={quote(a['canal_url'], safe='')}&text={texto}"))
    return [fila] if fila else []


def post_bombazo(cfg, o):
    a = cfg["ajustes"]
    cab = "💣 <b>BOMBAZO</b>" + (" · 📉 <b>MÍNIMO HISTÓRICO</b>" if o["minimo"] else "")
    tachado = f"  <s>{clp(o['normal'])}</s>" if o.get("normal") and o["normal"] > o["precio"] else ""
    l = [cab, "", f"<b>{esc(o['nombre'])}</b>"]
    if o["tipo"] == "digital":
        l.append(f"💰 <b>{precio_txt(o)}</b>{tachado}  (−{round(o['pct'])}%) en eShop Chile")
        if o["ext"]:
            l.append(f"🇺🇸 Con saldo de EE. UU. sale ~{clp(o['ext']['clp'])} ({o['ext']['ahorro']}% menos)")
        f = o.get("fisico")
        if f:
            dif = o["precio"] - f["precio"]
            l.append(f"📦 Físico: {clp(f['precio'])} en {esc(f['tienda'])}" +
                     (f" (−{clp(dif)})" if dif > 0 else ""))
    else:
        l.append(f"💰 <b>{precio_txt(o)}</b>{tachado}  (−{round(o['pct'])}%) en {esc(o['tienda'])}")
        if len(o["avisos"]) > 1:
            sig = o["avisos"][1]
            l.append(f"🏬 Le sigue {esc(sig['tienda'])} a {clp(sig['precio'])}")
    info = ["Switch 2" if o.get("plataforma") == "Switch 2" else "Switch"]
    if o.get("editora"):
        info.append(esc(o["editora"].split(",")[0]))
    l.append("🕹️ " + " · ".join(info))
    if quedan(o):
        l.append(quedan(o))
    l += ["", f"👉 {veredicto(o)}"]
    donde, url = mejor_opcion(o)
    filas = [[boton(f"🛒 Comprar · {donde}", url)]]
    if o["tipo"] == "digital" and o.get("fisico") and url != o["url"]:
        filas.append([boton("🎮 Ver en eShop", o["url"])])
    if o["ext"] and a.get("saldo_url"):
        filas.append([boton("💳 Comprar saldo", a["saldo_url"])])
    return {"texto": "\n".join(l), "botones": filas + pie(cfg),
            "preview": o["url"] if o["tipo"] == "digital" else None}


def post_lista(cfg, titulo, items, t, extra=None, cierre=None):
    """Lista con tarjetas cortas, foto del primero arriba y un boton por juego."""
    fuente = "eShop Chile 🇨🇱 y tiendas" if any(o["tipo"] == "fisico" or o.get("fisico") for o in items) \
        else "eShop Chile 🇨🇱"
    l = [titulo, f"<i>{fecha_txt(t)} · precios {fuente}</i>"]
    for i, o in enumerate(items):
        l += ["", bloque(cfg, o, MEDALLAS[i])]
    if extra:
        l += ["", extra]
    ahorro = sum(o.get("ahorro") or 0 for o in items)
    l += ["", LINEA, cierre or f"💡 Ahorras {clp(ahorro)} si te llevas los {len(items)}"]
    primero = next((o for o in items if o["tipo"] == "digital"), None)
    return {"texto": "\n".join(l), "botones": botones_juegos(items) + pie(cfg),
            "preview": primero["url"] if primero else None}


# --------------------------------------------------------------- slots

def clave_saga(o):
    return norm(o["saga"]) if o.get("saga") else " ".join(norm(o["nombre"]).split()[:2])


def elegir(cfg, items, n, regalados=True):
    """Los n mejores, sin repetir saga y con pocos 'regalados', para que la lista tenga variedad."""
    a = cfg["ajustes"]
    out, sagas, baratos = [], {}, 0
    for o in sorted(items, key=lambda o: (-o["score"], -o.get("ahorro", 0))):
        k = clave_saga(o)
        if sagas.get(k, 0) >= a["max_por_saga"]:
            continue
        barato = o["tipo"] == "digital" and o["precio"] < a["regalado_bajo_clp"]
        if barato and regalados and baratos >= a["max_regalados_en_listas"]:
            continue
        out.append(o)
        sagas[k] = sagas.get(k, 0) + 1
        baratos += barato
        if len(out) == n:
            break
    return out


def elegibles(cfg, est, cands, t, minimo="destacado"):
    orden = {"bombazo": 2, "destacado": 1, "resto": 0}
    return sorted((o for o in cands if orden[o["nivel"]] >= orden[minimo]
                   and not ya_publicado(cfg, est, o, t)),
                  key=lambda o: -o["score"])


def slot_top(cfg, est, cands, t):
    items = elegir(cfg, elegibles(cfg, est, cands, t), 5)
    if not items:
        return None, []
    titulo = "☕ <b>TOP 5 DE HOY</b>" if len(items) == 5 else "☕ <b>LO MEJOR DE HOY</b>"
    return post_lista(cfg, titulo, items, t), items


def filtro_pack(cfg, o, filtro):
    a = cfg["ajustes"]
    if filtro == "regalado":
        return o["tipo"] == "digital" and o["precio"] < a["regalado_bajo_clp"] and o.get("senales")
    if filtro == "saga":
        return bool(o["saga"])
    if filtro == "joya":
        return "joya" in o.get("senales", [])
    if filtro == "editora_a":
        return "editora_a" in o.get("senales", []) or "saga_estrella" in o.get("senales", [])
    if filtro == "rebajados":
        return o["pct"] >= 60 and o["score"] >= a["umbral_destacado"] - 10
    if filtro == "nintendo":
        nin = {norm(x) for x in cfg["sagas_nintendo"]}
        return (o["saga"] and norm(o["saga"]) in nin) or contiene(norm(o.get("editora") or ""), "nintendo")
    return False


def slot_pack(cfg, est, cands, t):
    pack = cfg["packs"].get(DIAS[t.weekday()])
    if not pack:
        return None, []
    con_fisicos = pack["filtro"] in ("nintendo", "saga")
    base = [o for o in cands if (o["tipo"] == "digital" or con_fisicos) and not ya_publicado(cfg, est, o, t)
            and o.get("senales") and o["score"] >= cfg["ajustes"]["umbral_destacado"] - 15]
    items = elegir(cfg, [o for o in base if filtro_pack(cfg, o, pack["filtro"])], 4,
                   regalados=pack["filtro"] != "regalado")
    if len(items) < 2:
        return None, []
    extra, r = None, None
    if pack["filtro"] != "regalado":
        baratos = [o for o in base if o not in items and filtro_pack(cfg, o, "regalado")]
        if baratos:
            r = max(baratos, key=lambda o: o["score"])
            extra = (f"💸 <b>Regalado del día:</b> <a href=\"{esc_attr(r['url'])}\">{esc(corto(r['nombre']))}</a>"
                     f" a <b>{precio_txt(r)}</b>")
    post = post_lista(cfg, f"🍔 <b>PACK DEL DÍA</b>\n<b>{esc(pack['titulo'])}</b>", items, t, extra)
    return post, items + ([r] if r else [])


def slot_fisicos(cfg, est, cands, t):
    fis = [o for o in cands if o["tipo"] == "fisico" and not ya_publicado(cfg, est, o, t)
           and (o.get("senales") or o["score"] >= cfg["ajustes"]["umbral_destacado"])]
    fis = elegir(cfg, fis, cfg["fisicos"]["max_juegos"])
    if not fis:
        return None, []
    l = ["📦 <b>FÍSICOS EN CHILE</b>", f"<i>{fecha_txt(t)} · comparamos {len(cfg_tiendas(fis))} tiendas</i>"]
    med = ["🥇", "🥈", "🥉"]
    for i, o in enumerate(fis):
        l += ["", f"{MEDALLAS[i]} <b>{esc(corto(o['nombre'], 44))}</b>{plat(o)} · −{round(o['pct'])}%"
              + (" · 📉 mínimo" if o["minimo"] else "")]
        for j, av in enumerate(o["avisos"][:3]):
            l.append(f"   {med[j]} {esc(av['tienda'])} <b>{clp(av['precio'])}</b>" if j == 0
                     else f"   {med[j]} {esc(av['tienda'])} {clp(av['precio'])}")
    l += ["", LINEA, "💡 Precio más bajo primero; el botón te lleva a esa tienda"]
    return {"texto": "\n".join(l), "botones": botones_juegos(fis) + pie(cfg)}, fis


def cfg_tiendas(items):
    return {av["tienda"] for o in items for av in o.get("avisos") or []}


def slot_ultima(cfg, est, cands, t):
    horas = cfg["ajustes"]["ultima_llamada_horas"] / 24
    items = elegir(cfg, [o for o in cands if o["tipo"] == "digital" and o.get("dias") is not None
                         and 0 < o["dias"] <= horas and o["nivel"] != "resto"
                         and not ya_publicado(cfg, est, o, t, "ultima_llamada")], 5)
    if not items:
        return None, []
    post = post_lista(cfg, "⏳ <b>ÚLTIMA LLAMADA</b> · se acaban pronto", items, t,
                      cierre="⚠️ Después de esto vuelven a su precio normal")
    return post, items


def slot_semanal(cfg, est, cands, t):
    items = elegir(cfg, [o for o in cands if o["nivel"] != "resto"], 5)
    if not items:
        return None, []
    post = post_lista(cfg, "🏆 <b>TOP DE LA SEMANA</b> · siguen vigentes", items, t)
    enc = cfg.get("encuesta_semanal") or {
        "pregunta": "¿Qué te gustaría ver más en el canal?",
        "opciones": ["Más ofertas digitales", "Más juegos físicos", "Más juegos baratos", "Así está bien"]}
    post["encuesta"] = enc
    return post, []


SLOTS = {"top": slot_top, "pack": slot_pack, "fisicos": slot_fisicos,
         "ultima": slot_ultima, "semanal": slot_semanal}


# --------------------------------------------------------------- envio

def destino():
    if os.environ.get("CANAL_PRUEBA"):
        return os.environ.get("TELEGRAM_CHAT_ID")
    return os.environ.get("TELEGRAM_CHANNEL_ID")


FALLOS = []


def tg(metodo, cuerpo):
    token = os.environ.get("TELEGRAM_TOKEN")
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{metodo}", json=cuerpo, timeout=TIMEOUT)
    except requests.RequestException as e:
        log(f"ERROR Telegram {metodo}: {e}")
        FALLOS.append(metodo)
        return None
    if r.status_code != 200:
        FALLOS.append(metodo)
        log(f"ERROR Telegram {metodo} {r.status_code}: {r.text[:300]}")
        if r.status_code == 429:
            seg = int((r.json().get("parameters") or {}).get("retry_after") or 0)
            log(f"  Telegram bloqueó al bot en este chat por {seg // 3600} h {seg % 3600 // 60} min "
                "por exceso de mensajes. No es un error del canal: hay que esperar.")
        elif r.status_code in (400, 403):
            log("  Revisa que TELEGRAM_CHANNEL_ID sea el número -100… completo y que el bot sea "
                "administrador del canal con permiso de publicar.")
        return None
    return r.json().get("result")


def enviar(post, sonido):
    if os.environ.get("DRY_RUN"):
        log("─" * 40 + "\n" + re.sub(r"<[^>]+>", "", post["texto"]))
        for fila in post.get("botones") or []:
            log("   " + "  ".join(f"[{b['text']}]" for b in fila))
        if post.get("encuesta"):
            log(f"   📊 {post['encuesta']['pregunta']} " + " / ".join(post["encuesta"]["opciones"]))
        return SIN_ENVIO
    chat = destino()
    if not os.environ.get("TELEGRAM_TOKEN") or not chat:
        log("ERROR: faltan TELEGRAM_TOKEN o TELEGRAM_CHANNEL_ID (o TELEGRAM_CHAT_ID con CANAL_PRUEBA)")
        return None
    cuerpo = {"chat_id": chat, "text": post["texto"], "parse_mode": "HTML",
              "disable_notification": not sonido}
    if post.get("preview"):
        cuerpo["link_preview_options"] = {"url": post["preview"], "prefer_large_media": True,
                                          "show_above_text": True}
    else:
        cuerpo["link_preview_options"] = {"is_disabled": True}
    if post.get("botones"):
        cuerpo["reply_markup"] = {"inline_keyboard": post["botones"]}
    res = tg("sendMessage", cuerpo)
    if res and post.get("encuesta"):
        e = post["encuesta"]
        tg("sendPoll", {"chat_id": chat, "question": e["pregunta"],
                        "options": [{"text": x} for x in e["opciones"]],
                        "is_anonymous": True, "disable_notification": True})
    return res


def publicar(cfg, est, t, slot, post, items, sonido, grupo="publicados"):
    d = dia_de(est, t)
    if d["posts"] >= cfg["ajustes"]["max_posts_dia"]:
        log(f"  {slot}: tope de {d['posts']} posts diarios alcanzado")
        return False
    res = enviar(post, sonido)
    if res is None:
        return False
    if res is SIN_ENVIO:
        return True
    d["posts"] += 1
    marcar(est, items, t, slot, grupo)
    est["ultimo_post"] = t.isoformat()
    est["posts"] = (est.get("posts") or [])[-199:] + [{
        "fecha": t.isoformat(timespec="minutes"), "slot": slot,
        "message_id": res.get("message_id"), "ids": [o["id"] for o in items]}]
    return True


# --------------------------------------------------------------- modos

def correr_slot(cfg, est, cands, t, slot, sonido, forzar=False):
    d = dia_de(est, t)
    if slot in d["slots"] and not forzar:
        log(f"  {slot}: ya se publicó hoy")
        return
    post, items = SLOTS[slot](cfg, est, cands, t)
    if not post:
        log(f"  {slot}: nada que pase el filtro, se salta")
        if not os.environ.get("DRY_RUN"):
            d["slots"].append(slot)
        return
    grupo = "ultima_llamada" if slot == "ultima" else "publicados"
    if publicar(cfg, est, t, slot, post, items, sonido, grupo) and not os.environ.get("DRY_RUN"):
        d["slots"].append(slot)
        log(f"  {slot}: publicado ({len(items)} juegos)")


def toca_hoy(dias, t):
    hoy = DIAS[t.weekday()]
    if dias == "todos":
        return True
    if "-" in dias and "," not in dias:
        a, b = dias.split("-")
        return DIAS.index(a) <= DIAS.index(hoy) <= DIAS.index(b)
    return hoy in dias.split(",")


def modo_auto(cfg, est, cands, t):
    for h in cfg["horarios"]:
        if not toca_hoy(h["dias"], t):
            continue
        hh, mm = hhmm(h["hora"])
        inicio = t.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if inicio <= t < inicio + timedelta(minutes=VENTANA_MIN):
            correr_slot(cfg, est, cands, t, h["slot"], h["sonido"])
    modo_bombazos(cfg, est, cands, t)


def modo_bombazos(cfg, est, cands, t):
    a = cfg["ajustes"]
    if en_silencio(cfg, t):
        log("  bombazos: horario de silencio, quedan para después")
        return
    d = dia_de(est, t)
    for o in elegibles(cfg, est, cands, t, "bombazo"):
        if d["bombazos"] >= a["max_bombazos_dia"]:
            log("  bombazos: tope diario alcanzado")
            return
        if est.get("ultimo_post"):
            previo = datetime.fromisoformat(est["ultimo_post"])
            if t - previo < timedelta(minutes=a["min_minutos_entre_posts"]):
                log("  bombazos: muy pronto desde el último post, espera a la próxima corrida")
                return
        if publicar(cfg, est, t, "bombazo", post_bombazo(cfg, o), [o], True):
            if not os.environ.get("DRY_RUN"):
                d["bombazos"] += 1
            log(f"  bombazo: {o['nombre']} ({o['score']})")
        if os.environ.get("DRY_RUN"):
            return


def modo_vista(cfg, est, cands, t):
    os.environ["DRY_RUN"] = "1"
    niveles = {}
    for o in cands:
        niveles[o["nivel"]] = niveles.get(o["nivel"], 0) + 1
    log(f"Candidatos: {len(cands)} · {niveles}")
    for o in sorted(cands, key=lambda o: -o["score"])[:25]:
        log(f"  {o['score']:3d} {o['nivel']:9s} {o['tipo']:7s} {o['nombre'][:45]:45s} "
            f"{clp(o['precio']):>9s} −{round(o['pct'])}% {','.join(o.get('senales') or [])}")
    log("\n=== BOMBAZO (el mejor) ===")
    b = elegibles(cfg, est, cands, t, "bombazo")
    if b:
        enviar(post_bombazo(cfg, b[0]), True)
    else:
        log("(ninguno hoy)")
    sim = {"publicados": dict(est["publicados"]), "ultima_llamada": dict(est["ultima_llamada"]),
           "dias": {}, "posts": []}
    for slot in ("top", "pack", "fisicos", "ultima", "semanal"):
        log(f"\n=== {slot.upper()} ===")
        post, items = SLOTS[slot](cfg, sim, cands, t)
        if not post:
            log("(nada que pase el filtro)")
            continue
        enviar(post, False)
        marcar(sim, items, t, slot, "ultima_llamada" if slot == "ultima" else "publicados")


def modo_sugerir(cfg, state):
    """Editoras con mas juegos que caen en las reglas de basura: candidatas para la lista negra."""
    c2 = dict(cfg)
    c2["editoras"] = {**cfg["editoras"], "basura": []}
    cuenta = {}
    for o in candidatos(c2, state, incluir_excluidos=True):
        ed = o.get("editora")
        if not ed:
            continue
        x = cuenta.setdefault(ed, [0, 0])
        x[0] += 1
        if o.get("excluido") or o.get("score", 0) < 20:
            x[1] += 1
    if not cuenta:
        log("Todavía no hay editoras guardadas: aparecen después de la próxima revisión de bot.py.")
        return
    log("Editora · juegos en oferta · juegos que caen en basura")
    for ed, (n, malos) in sorted(cuenta.items(), key=lambda x: (-x[1][1], -x[1][0]))[:30]:
        if malos >= 3 and malos / n >= 0.7:
            log(f"  {ed}: {n} · {malos}")


def main():
    cfg = load_json(CANAL_FILE, None)
    if cfg is None:
        log("ERROR: falta canal.json")
        sys.exit(1)
    state = load_json(STATE_FILE, {})
    if "--sugerir" in sys.argv:
        modo_sugerir(cfg, state)
        return
    est = cargar_estado()
    t = ahora(cfg)
    cands = candidatos(cfg, state)
    log(f"Canal · {t:%Y-%m-%d %H:%M} · {len(cands)} ofertas candidatas")
    if "--vista" in sys.argv:
        modo_vista(cfg, est, cands, t)
        return
    if "--slot" in sys.argv:
        i = sys.argv.index("--slot")
        slot = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
        if slot not in SLOTS:
            log(f"Slots válidos: {', '.join(SLOTS)}")
            sys.exit(1)
        sonido = next((h["sonido"] for h in cfg["horarios"] if h["slot"] == slot), False)
        correr_slot(cfg, est, cands, t, slot, sonido, forzar=True)
    elif "--bombazos" in sys.argv:
        modo_bombazos(cfg, est, cands, t)
    else:
        modo_auto(cfg, est, cands, t)
    if not os.environ.get("DRY_RUN"):
        save_json(ESTADO_FILE, est)
    if FALLOS:
        sys.exit(1)                     # que Actions lo marque en rojo


if __name__ == "__main__":
    main()
