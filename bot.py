#!/usr/bin/env python3
"""
Alertas de ofertas de juegos de Switch / Switch 2 por Telegram.

- Digitales: eShop de Nintendo (por defecto EE. UU.), usando el mismo servicio
  de precios que consulta la tienda de Nintendo.
- Fisicos: paginas de producto de tiendas chilenas; lee el precio desde los
  datos estructurados (JSON-LD) que la mayoria de las tiendas publica.

Uso:
    python bot.py            revisa precios y manda alertas
    python bot.py --test     manda un mensaje de prueba a Telegram
    DRY_RUN=1 python bot.py  revisa precios pero solo imprime las alertas

Variables de entorno: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""

import html
import json
import os
import re
import sys
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "juegos.json"
STATE_FILE = BASE / "state.json"

PRICE_API = "https://api.ec.nintendo.com/v1/price"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
}
TIMEOUT = 25
PLACEHOLDER = "PEGA_AQUI"
FALLOS_PARA_AVISAR = 5  # corridas seguidas fallando antes de avisar


# ---------------------------------------------------------------- utilidades

def log(msg):
    print(msg, flush=True)


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, data):
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def esc(text):
    return html.escape(str(text), quote=False)


def clp(n):
    return "$" + f"{int(round(n)):,}".replace(",", ".")


def usd(n):
    return f"US${n:.2f}"


def dominio(url):
    m = re.match(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else url


# ------------------------------------------------------------------ telegram

def send_telegram(text):
    if os.environ.get("DRY_RUN"):
        log("[DRY_RUN] " + text.replace("\n", "\n          "))
        return True
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log("ERROR: faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            log(f"ERROR Telegram {r.status_code}: {r.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        log(f"ERROR Telegram: {e}")
        return False


# --------------------------------------------------------------------- eShop

def resolver_nsuid(url):
    """Saca el NSUID (id de 14 digitos) desde la pagina del juego en nintendo.com."""
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    m = re.search(r'"nsuid"\s*:\s*"?(70\d{12})', r.text)
    if not m:
        m = re.search(r"\b(70(?:01|05|07)\d{10})\b", r.text)
    return m.group(1) if m else None


def precios_eshop(nsuids, pais):
    """Devuelve {nsuid: {regular, oferta, fin}} para los ids pedidos."""
    out = {}
    ids = list(nsuids)
    for i in range(0, len(ids), 50):
        lote = ids[i:i + 50]
        r = requests.get(
            PRICE_API,
            params={"country": pais, "lang": "en", "ids": ",".join(lote)},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        for p in r.json().get("prices", []):
            reg = p.get("regular_price")
            if not reg:
                continue
            desc = p.get("discount_price")
            out[str(p["title_id"])] = {
                "regular": float(reg["raw_value"]),
                "oferta": float(desc["raw_value"]) if desc else None,
                "fin": (desc or {}).get("end_datetime"),
            }
    return out


def revisar_digitales(cfg, state, alertas):
    pais = cfg.get("ajustes", {}).get("pais_eshop", "US")
    cache = state.setdefault("nsuids", {})
    est = state.setdefault("digitales", {})

    juegos = []
    for j in cfg.get("digitales", []):
        url = j.get("url", "")
        nsuid = str(j.get("nsuid") or "").strip()
        if PLACEHOLDER in url and not nsuid:
            continue
        if not nsuid:
            nsuid = cache.get(url)
        if not nsuid:
            try:
                nsuid = resolver_nsuid(url)
            except requests.RequestException as e:
                log(f"  eShop: no pude abrir {url}: {e}")
            if nsuid:
                cache[url] = nsuid
            else:
                log(f"  eShop: sin NSUID para '{j.get('nombre')}'. "
                    "Agrega el campo \"nsuid\" a mano en juegos.json.")
                continue
        juegos.append((j, nsuid))

    if not juegos:
        return
    try:
        precios = precios_eshop({n for _, n in juegos}, pais)
    except (requests.RequestException, ValueError) as e:
        log(f"  eShop: fallo la consulta de precios: {e}")
        return

    for j, nsuid in juegos:
        nombre = j.get("nombre", nsuid)
        p = precios.get(nsuid)
        if not p:
            log(f"  eShop: sin precio para '{nombre}' ({nsuid})")
            continue
        actual = p["oferta"] if p["oferta"] is not None else p["regular"]
        objetivo = j.get("precio_objetivo_usd")
        s = est.setdefault(nsuid, {})
        avisado = s.get("avisado")
        log(f"  eShop: {nombre}: {usd(actual)} (normal {usd(p['regular'])})")

        en_oferta = p["oferta"] is not None
        bajo_objetivo = objetivo is not None and actual <= objetivo
        # Con objetivo: avisa solo al llegar al objetivo. Sin objetivo: cualquier oferta.
        interesa = bajo_objetivo if objetivo is not None else en_oferta

        if interesa and (avisado is None or actual < avisado - 0.005):
            pct = round(100 * (1 - actual / p["regular"])) if p["regular"] else 0
            lineas = [
                f"🎮 <b>{esc(nombre)}</b> (eShop {esc(pais)})",
                f"💸 {usd(actual)}"
                + (f"  <s>{usd(p['regular'])}</s>  −{pct}%" if en_oferta else ""),
            ]
            if objetivo is not None:
                lineas.append(f"🎯 Tu objetivo: {usd(objetivo)}")
            if p["fin"]:
                lineas.append(f"⏳ Hasta: {esc(p['fin'][:10])}")
            if j.get("url") and PLACEHOLDER not in j["url"]:
                lineas.append(esc(j["url"]))
            alertas.append("\n".join(lineas))
            s["avisado"] = actual
        elif not interesa:
            s["avisado"] = None  # se acabo la oferta: se rearma la alerta
        s["ultimo"] = actual


# ------------------------------------------------------------ tiendas Chile

def parse_precio(v):
    """Convierte '49.990', '$49.990', '49990.00', 49990 -> 49990.0"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.,]", "", str(v))
    if not s:
        return None
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):        # 49.990 / 1.049.990
        return float(re.sub(r"[.,]", "", s))
    if re.fullmatch(r"\d{1,3}(\.\d{3})+,\d+", s):       # 49.990,00
        return float(s.replace(".", "").replace(",", "."))
    if re.fullmatch(r"\d{1,3}(,\d{3})+\.\d+", s):       # 49,990.00
        return float(s.replace(",", ""))
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def _precios_de_offers(offers):
    """Recorre 'offers' de JSON-LD y entrega los precios con stock."""
    if isinstance(offers, list):
        for o in offers:
            yield from _precios_de_offers(o)
        return
    if not isinstance(offers, dict):
        return
    disp = str(offers.get("availability", "")).lower()
    if "outofstock" in disp or "soldout" in disp or "discontinued" in disp:
        return
    for clave in ("price", "lowPrice"):
        p = parse_precio(offers.get(clave))
        if p:
            yield p
    spec = offers.get("priceSpecification")
    for e in spec if isinstance(spec, list) else [spec]:
        if isinstance(e, dict):
            p = parse_precio(e.get("price"))
            if p:
                yield p
    if "offers" in offers:
        yield from _precios_de_offers(offers["offers"])


def _buscar_productos(nodo):
    """Busca nodos tipo Product dentro de un JSON-LD (incluye @graph anidados)."""
    if isinstance(nodo, list):
        for x in nodo:
            yield from _buscar_productos(x)
    elif isinstance(nodo, dict):
        tipo = nodo.get("@type")
        tipos = tipo if isinstance(tipo, list) else [tipo]
        if "Product" in tipos and "offers" in nodo:
            yield nodo
        for v in nodo.values():
            if isinstance(v, (dict, list)):
                yield from _buscar_productos(v)


def extraer_precio(pagina):
    """Precio mas bajo con stock que publica la pagina, o None."""
    encontrados = []
    for bloque in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        pagina, flags=re.S | re.I,
    ):
        try:
            data = json.loads(bloque.strip())
        except ValueError:
            continue
        for prod in _buscar_productos(data):
            encontrados.extend(_precios_de_offers(prod["offers"]))
    if not encontrados:
        for patron in (
            r'<meta[^>]+(?:property|name)=["\'](?:product|og):price:amount["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:product|og):price:amount',
        ):
            m = re.search(patron, pagina, flags=re.I)
            if m:
                p = parse_precio(m.group(1))
                if p:
                    encontrados.append(p)
                    break
    return min(encontrados) if encontrados else None


def revisar_fisicos(cfg, state, alertas):
    baja_min = cfg.get("ajustes", {}).get("baja_minima_pct", 10)
    est = state.setdefault("fisicos", {})
    fallos = state.setdefault("fallos", {})

    for j in cfg.get("fisicos", []):
        nombre = j.get("nombre", "?")
        urls = [u for u in j.get("urls", []) if PLACEHOLDER not in u]
        if not urls:
            continue
        mejor = None
        for url in urls:
            precio = None
            try:
                r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
                if r.status_code == 200:
                    precio = extraer_precio(r.text)
                else:
                    log(f"  {dominio(url)}: HTTP {r.status_code}")
            except requests.RequestException as e:
                log(f"  {dominio(url)}: {e}")
            if precio is None:
                fallos[url] = fallos.get(url, 0) + 1
                log(f"  {nombre} @ {dominio(url)}: sin precio "
                    f"(fallo {fallos[url]} seguido)")
                if fallos[url] == FALLOS_PARA_AVISAR:
                    alertas.append(
                        f"⚠️ Llevo {FALLOS_PARA_AVISAR} revisiones sin poder leer "
                        f"el precio de <b>{esc(nombre)}</b> en {esc(dominio(url))}. "
                        f"Puede que la tienda bloquee al bot, que no haya stock "
                        f"o que el link haya cambiado.\n{esc(url)}"
                    )
                continue
            fallos.pop(url, None)
            log(f"  {nombre} @ {dominio(url)}: {clp(precio)}")
            if mejor is None or precio < mejor[0]:
                mejor = (precio, url)

        if mejor is None:
            continue
        precio, url = mejor
        s = est.setdefault(nombre, {})
        anterior = s.get("ultimo")
        avisado = s.get("avisado")
        objetivo = j.get("precio_objetivo_clp")

        bajo_objetivo = objetivo is not None and precio <= objetivo
        baja_pct = (100 * (1 - precio / anterior)) if anterior else 0
        bajo_fuerte = objetivo is None and baja_pct >= baja_min
        interesa = bajo_objetivo or bajo_fuerte

        if interesa and (avisado is None or precio < avisado):
            lineas = [
                f"📦 <b>{esc(nombre)}</b> (físico)",
                f"💸 {clp(precio)} en {esc(dominio(url))}",
            ]
            if anterior and precio < anterior:
                lineas.append(f"📉 Antes: {clp(anterior)} (−{round(baja_pct)}%)")
            if objetivo is not None:
                lineas.append(f"🎯 Tu objetivo: {clp(objetivo)}")
            lineas.append(esc(url))
            alertas.append("\n".join(lineas))
            s["avisado"] = precio
        elif objetivo is not None and not bajo_objetivo:
            s["avisado"] = None  # volvio a subir: se rearma la alerta
        elif objetivo is None and anterior and precio > anterior:
            s["avisado"] = None
        s["ultimo"] = precio


# ---------------------------------------------------------------------- main

def main():
    if "--test" in sys.argv:
        ok = send_telegram("👑 El bot de ofertas está conectado y funcionando.")
        sys.exit(0 if ok else 1)

    cfg = load_json(CONFIG_FILE, None)
    if cfg is None:
        log(f"ERROR: no existe {CONFIG_FILE.name}")
        sys.exit(1)
    state = load_json(STATE_FILE, {})
    alertas = []

    log("Revisando eShop...")
    revisar_digitales(cfg, state, alertas)
    log("Revisando tiendas chilenas...")
    revisar_fisicos(cfg, state, alertas)

    enviadas = sum(1 for a in alertas if send_telegram(a))
    save_json(STATE_FILE, state)
    log(f"Listo: {enviadas}/{len(alertas)} alertas enviadas.")


if __name__ == "__main__":
    main()
