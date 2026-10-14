#!/usr/bin/env python3
"""
SwitchBot: alertas de ofertas de juegos de Nintendo Switch / Switch 2 por Telegram.

Busca cada juego POR NOMBRE (no hace falta pegar links):
- Digitales: catalogo de la eShop de EE. UU. + servicio de precios de Nintendo.
- Fisicos: buscador de cada tienda chilena. Detecta solo como leer cada tienda
  (Shopify, WooCommerce, VTEX, Magento, paginas Next.js, datos JSON-LD).

Uso:
    python bot.py                 revisa precios y manda alertas
    python bot.py --test          manda un mensaje de prueba a Telegram
    python bot.py --diagnostico   prueba cada tienda y muestra que encuentra
    DRY_RUN=1 python bot.py       revisa pero solo imprime (no manda nada)

Variables de entorno: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""

import html
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import requests

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "juegos.json"
STATE_FILE = BASE / "state.json"
HIST_FILE = BASE / "history.json"

PRICE_API = "https://api.ec.nintendo.com/v1/price"
ALGOLIA_URL = "https://U3B6GR4UA3-dsn.algolia.net/1/indexes/store_game_en_us"
ALGOLIA_HEADERS = {
    "X-Algolia-Application-Id": "U3B6GR4UA3",
    "X-Algolia-API-Key": "a29c6927638bfd8cee23993e51e721c9",
}
NINTENDO_WEB = "https://www.nintendo.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
}
TIMEOUT = 25
PAUSA = 1.0                 # segundos entre consultas a una misma tienda
FALLOS_PARA_AVISAR = 5      # corridas seguidas sin resultados antes de avisar
MAX_PAGINAS_SHOPIFY = 40    # 250 productos por pagina
TELEGRAM_MAX = 3900

PLATAFORMA = {"switch", "nsw", "ns2", "sw2", "switch2"}
EXCLUIR_SIEMPRE = {
    "amiibo", "funda", "estuche", "case", "carcasa", "protector", "mica",
    "control", "controles", "joy", "joycon", "figura", "figuras", "peluche",
    "polera", "poleron", "poster", "libro", "guia", "artbook", "llavero",
    "taza", "mochila", "lamina", "laminas", "skin", "skins", "cable",
    "soporte", "consola", "audifono", "audifonos", "tarjeta", "gift",
    "cargador", "dock", "grip", "volante", "teclado", "mouse", "bolso",
    "cartas", "carta", "tcg", "booster", "sobre", "sobres", "mazo", "album",
    "lego", "juguete", "plush", "pin", "pins", "gorro", "chaqueta", "lampara",
    "cojin", "manta", "billetera", "stickers", "sticker", "auriculares", "audifonos",
    "almacenamiento", "volantes", "compatibles", "compatible", "accesorio", "accesorios",
    "premgear", "kit", "estuches", "fundas", "protectores", "lapiz",
}

VOLCAR = None               # carpeta donde el diagnostico guarda las paginas
PENDIENTES = []             # alertas de esta corrida (para deshacer si Telegram falla)

PRECIO_MIN_CLP = 1000
PRECIO_MAX_CLP = 3_000_000


# ---------------------------------------------------------------- utilidades

def log(msg=""):
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


def esc_attr(text):
    return html.escape(str(text), quote=True)


def clp(n):
    return "$" + f"{int(round(n)):,}".replace(",", ".")


def usd(n):
    return f"US${n:.2f}"


def norm(texto):
    """minusculas, sin tildes ni simbolos: 'Pokémon™ Legends: Z-A' -> 'pokemon legends z a'"""
    t = re.sub(r"[’'´`]", "", re.sub(r"[™®©]", " ", str(texto)))
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def palabras(texto):
    return set(norm(texto).split())


def parse_precio(v):
    """'49.990', '$49.990', '49990.00', 49990 -> 49990.0"""
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


def precio_valido(p):
    return p is not None and PRECIO_MIN_CLP <= p <= PRECIO_MAX_CLP


def get(url, **kw):
    kw.setdefault("headers", HEADERS)
    kw.setdefault("timeout", TIMEOUT)
    return requests.get(url, **kw)


# ------------------------------------------------------------------ juegos

def terminos(juego):
    b = juego.get("buscar") or juego.get("nombre", "")
    return [b] if isinstance(b, str) else list(b)


def coincide(titulo, juego, exigir_plataforma):
    """El titulo corresponde al juego si tiene todas las palabras de algun
    termino de busqueda, no tiene palabras excluidas y (en tiendas) dice Switch."""
    w = palabras(titulo)
    if not any(palabras(t) <= w for t in terminos(juego)):
        return False
    excl = {norm(x) for x in juego.get("excluir", [])}
    if exigir_plataforma:
        if not (w & PLATAFORMA):
            return False
        excl |= EXCLUIR_SIEMPRE
    return not (w & excl)


# --------------------------------------------------------------------- eShop

def buscar_eshop(termino):
    """Busca en el catalogo de la eShop de EE. UU. Devuelve [{titulo, nsuid, url}]."""
    r = get(ALGOLIA_URL, headers={**HEADERS, **ALGOLIA_HEADERS},
            params={"query": termino, "hitsPerPage": 100})
    r.raise_for_status()
    out = []
    for h in r.json().get("hits", []):
        nsuid = str(h.get("nsuid") or "")
        if not nsuid.startswith("7001"):      # 7001 = juego (7005 = DLC)
            continue
        url = h.get("url") or ""
        out.append({
            "titulo": re.sub(r"[™®©]", "", h.get("title", nsuid)).strip(),
            "nsuid": nsuid,
            "url": urljoin(NINTENDO_WEB, url) if url else "",
            "plataforma": "Switch 2" if h.get("platformCode") == "NINTENDO_SWITCH_2" else "Switch",
        })
    return out


def precios_eshop(nsuids, pais):
    """{nsuid: {regular, oferta, fin}}"""
    out = {}
    ids = sorted(set(nsuids))
    for i in range(0, len(ids), 50):
        r = get(PRICE_API, params={"country": pais, "lang": "en",
                                   "ids": ",".join(ids[i:i + 50])})
        r.raise_for_status()
        for p in r.json().get("prices", []):
            reg = p.get("regular_price")
            if not reg or not float(reg.get("raw_value") or 0):
                continue
            desc = p.get("discount_price")
            out[str(p["title_id"])] = {
                "regular": float(reg["raw_value"]),
                "oferta": float(desc["raw_value"]) if desc else None,
                "fin": (desc or {}).get("end_datetime"),
            }
    return out


# ------------------------------------------------- lectores de tiendas

def leer_shopify(base, termino, cache):
    """Catalogo completo via /products.json (una vez por corrida)."""
    if "shopify" not in cache:
        prods = []
        for page in range(1, MAX_PAGINAS_SHOPIFY + 1):
            r = get(f"{base}/products.json", params={"limit": 250, "page": page})
            if r.status_code != 200:
                break
            try:
                lote = r.json().get("products", [])
            except ValueError:
                break
            if not lote:
                break
            for p in lote:
                vs = [v for v in p.get("variants", []) if v.get("available", True)]
                if not vs:
                    continue
                v = min(vs, key=lambda v: parse_precio(v.get("price")) or 9e18)
                prods.append({
                    "titulo": p.get("title", ""),
                    "url": f"{base}/products/{p.get('handle')}",
                    "precio": parse_precio(v.get("price")),
                    "normal": parse_precio(v.get("compare_at_price")),
                })
            time.sleep(PAUSA)
        if not prods:
            return None
        cache["shopify"] = prods
    return cache["shopify"]


def leer_woocommerce(base, termino, cache):
    r = get(f"{base}/wp-json/wc/store/v1/products",
            params={"search": termino, "per_page": 50})
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    out = []
    for p in data:
        if not isinstance(p, dict) or "prices" not in p:
            return None
        if p.get("is_in_stock") is False:
            continue
        pr = p["prices"]
        div = 10 ** int(pr.get("currency_minor_unit") or 0)
        precio = parse_precio(pr.get("price"))
        normal = parse_precio(pr.get("regular_price"))
        out.append({
            "titulo": html.unescape(p.get("name", "")),
            "url": p.get("permalink", ""),
            "precio": precio / div if precio else None,
            "normal": normal / div if normal else None,
        })
    return out


def leer_vtex(base, termino, cache):
    r = get(f"{base}/api/catalog_system/pub/products/search",
            params={"ft": termino, "_from": 0, "_to": 49})
    if r.status_code not in (200, 206):
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if not isinstance(data, list) or (data and "productName" not in data[0]):
        return None
    out = []
    for p in data:
        mejor = None
        for item in p.get("items", []):
            for s in item.get("sellers", []):
                o = s.get("commertialOffer", {})
                if not o.get("AvailableQuantity"):
                    continue
                precio = parse_precio(o.get("Price"))
                if precio and (mejor is None or precio < mejor[0]):
                    mejor = (precio, parse_precio(o.get("ListPrice")))
        if mejor:
            out.append({"titulo": p["productName"], "url": p.get("link", ""),
                        "precio": mejor[0], "normal": mejor[1]})
    return out


def _attr(tag, nombre):
    m = re.search(nombre + r'\s*=\s*["\']([^"\']*)["\']', tag, flags=re.I)
    return html.unescape(m.group(1)) if m else None


def parse_magento(pagina, url_pagina):
    out = []
    bloques = re.split(r'class="[^"]*\bproduct-item-info\b', pagina)[1:]
    for b in bloques:
        a = re.search(r'<a\b[^>]*\bproduct-item-link\b[^>]*>(.*?)</a>', b, flags=re.S | re.I)
        if not a:
            continue
        tag = a.group(0)[: a.group(0).find(">") + 1]
        href = _attr(tag, "href")
        titulo = html.unescape(re.sub(r"<[^>]+>", "", a.group(1))).strip()
        precios = {}
        for t in re.findall(r"<[^>]*data-price-amount[^>]*>", b, flags=re.I):
            tipo = _attr(t, "data-price-type") or ""
            monto = parse_precio(_attr(t, "data-price-amount"))
            if monto:
                precios.setdefault(tipo, monto)
        precio = precios.get("finalPrice") or precios.get("minPrice")
        if not precio:
            continue
        if re.search(r"\b(agotado|sin stock|out of stock)\b", b, flags=re.I) and \
                "tocart" not in b:
            continue
        out.append({"titulo": titulo, "url": urljoin(url_pagina, href or ""),
                    "precio": precio, "normal": precios.get("oldPrice")})
    return out


# --- datos embebidos (JSON-LD y __NEXT_DATA__)

CLAVES_NOMBRE = ("displayName", "productName", "name", "title")
CLAVES_URL = ("url", "canonicalUrl", "productUrl", "link", "permalink", "href")
CLAVES_PRECIO = ("prices", "price", "priceInfo", "offerPrice", "salePrice",
                 "bestPrice", "sellingPrice", "currentPrice", "finalPrice",
                 "internetPrice", "offers", "lowPrice")
MARCAS_IGNORAR = ("cmr", "cuota", "installment", "unit", "perunit", "shipping",
                  "despacho", "points", "puntos", "saving", "ahorro", "discount")
MARCAS_NORMAL = ("normal", "was", "list", "regular", "old", "before", "antes",
                 "strike", "original", "highprice")


def _recolectar_precios(v, marca, acc):
    """Junta precios (actuales y 'normales') dentro de un bloque de precio."""
    if isinstance(v, (int, float, str)) and not isinstance(v, bool):
        p = parse_precio(v) if not isinstance(v, str) or re.search(r"\d", v) else None
        if precio_valido(p):
            (acc["normal"] if any(m in marca for m in MARCAS_NORMAL) else acc["actual"]).append(p)
    elif isinstance(v, list):
        for x in v:
            _recolectar_precios(x, marca, acc)
    elif isinstance(v, dict):
        etiqueta = marca + " " + " ".join(
            str(v.get(k, "")) for k in ("type", "label", "priceType", "@type")
        ).lower()
        if any(m in etiqueta for m in MARCAS_IGNORAR):
            return
        for k, x in v.items():
            kl = k.lower()
            if any(m in kl for m in MARCAS_IGNORAR) or "currency" in kl or "high" in kl:
                continue
            if isinstance(x, (dict, list)):
                _recolectar_precios(x, etiqueta + " " + kl, acc)
            elif re.search(r"price|amount|value|valor|precio", kl):
                _recolectar_precios(x, etiqueta + " " + kl, acc)


AGOTADO_RE = re.compile(r"out.?of.?stock|sold.?out|agotado|sin stock|unavailable|no.?disponible",
                        re.I)


def _agotado(d):
    for k, v in d.items():
        kl = k.lower()
        if "avail" in kl or "stock" in kl:
            if isinstance(v, bool):
                if ("out" in kl) == v:      # isOutOfStock=True  /  inStock=False
                    return True
            elif isinstance(v, str) and AGOTADO_RE.search(v):
                return True
            elif isinstance(v, dict) and any(isinstance(x, str) and AGOTADO_RE.search(x)
                                             for x in v.values()):
                return True
        if kl == "offers" and isinstance(v, dict) and _agotado(v):
            return True
    return False


def productos_en_json(nodo, url_pagina, out, vistos):
    if isinstance(nodo, list):
        for x in nodo:
            productos_en_json(x, url_pagina, out, vistos)
        return
    if not isinstance(nodo, dict):
        return
    nombre = next((nodo[k] for k in CLAVES_NOMBRE if isinstance(nodo.get(k), str)), None)
    url = next((nodo[k] for k in CLAVES_URL
                if isinstance(nodo.get(k), str) and nodo[k].startswith(("/", "http"))), None)
    if nombre and url:
        acc = {"actual": [], "normal": []}
        for k in CLAVES_PRECIO:
            if k in nodo:
                _recolectar_precios(nodo[k], k.lower(), acc)
        if acc["actual"] and not _agotado(nodo):
            u = urljoin(url_pagina, url)
            if u not in vistos:
                vistos.add(u)
                precio = min(acc["actual"])
                normal = max(acc["normal"]) if acc["normal"] else None
                out.append({"titulo": html.unescape(nombre), "url": u, "precio": precio,
                            "normal": normal if normal and normal > precio else None})
    for v in nodo.values():
        if isinstance(v, (dict, list)):
            productos_en_json(v, url_pagina, out, vistos)


def parse_embebido(pagina, url_pagina):
    out, vistos = [], set()
    for bloque in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        pagina, flags=re.S | re.I,
    ):
        try:
            productos_en_json(json.loads(bloque.strip()), url_pagina, out, vistos)
        except ValueError:
            pass
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                  pagina, flags=re.S | re.I)
    if m:
        try:
            productos_en_json(json.loads(m.group(1)), url_pagina, out, vistos)
        except ValueError:
            pass
    return out


BUSQUEDAS_HTML = (
    "{base}/catalogsearch/result/?q={q}&product_list_limit=48",
    "{base}/search?q={q}",
    "{base}/buscar?q={q}",
    "{base}/busqueda?q={q}",
    "{base}/?s={q}&post_type=product",
)


def leer_html(base, termino, cache, plantilla=None):
    plantillas = [plantilla] if plantilla else BUSQUEDAS_HTML
    q = quote_plus(termino)
    for i, t in enumerate(plantillas):
        url = t.format(base=base, q=q)
        if i:
            time.sleep(PAUSA)
        try:
            r = get(url)
        except requests.RequestException:
            continue
        cache.setdefault("diag", []).append(f"{url} -> HTTP {r.status_code}, {len(r.text)} bytes")
        if VOLCAR:
            VOLCAR.mkdir(exist_ok=True)
            nombre = re.sub(r"[^a-z0-9]+", "_", re.sub(r"^https?://(www\.)?", "", url.lower()))[:80] + f"_{r.status_code}.html"
            (VOLCAR / nombre).write_text(r.text, encoding="utf-8", errors="replace")
        if r.status_code != 200:
            continue
        prods = parse_magento(r.text, r.url) or parse_embebido(r.text, r.url)
        if prods:
            cache["plantilla_html"] = t
            return prods
    return None


LECTORES = {
    "shopify": leer_shopify,
    "woocommerce": leer_woocommerce,
    "vtex": leer_vtex,
    "html": leer_html,
}


def buscar_en_tienda(tienda, termino, state, cache):
    """Devuelve lista de productos o None si la tienda no respondio con nada util."""
    base = tienda["url"].rstrip("/")
    metodos = state.setdefault("metodos", {})
    conocido = metodos.get(tienda["nombre"])
    orden = [conocido] if conocido in LECTORES else []
    if tienda.get("busqueda"):
        orden.append("html")
    orden += [m for m in LECTORES if m not in orden]
    for m in orden:
        try:
            if m == "html":
                plantilla = tienda.get("busqueda") or cache.get("plantilla_html")
                prods = leer_html(base, termino, cache, plantilla)
                if prods is None and plantilla and not tienda.get("busqueda"):
                    prods = leer_html(base, termino, cache)
            else:
                prods = LECTORES[m](base, termino, cache)
        except (requests.RequestException, ValueError) as e:
            cache.setdefault("diag", []).append(f"{m}: {type(e).__name__}: {str(e)[:120]}")
            prods = None
        if prods is not None:
            cache.setdefault("diag", []).append(f"{m}: OK ({len(prods)} productos)")
            metodos[tienda["nombre"]] = m
            cache["metodo"] = m
            return prods
        cache.setdefault("diag", []).append(f"{m}: sin resultados")
        time.sleep(PAUSA / 2)
    return None


def precio_de_pagina(url):
    """Para links puestos a mano: lee el precio de la pagina de producto."""
    r = get(url)
    if r.status_code != 200:
        return None
    prods = parse_embebido(r.text, r.url)
    precios = [p["precio"] for p in prods if precio_valido(p["precio"])]
    if not precios:
        m = re.search(r'(?:product|og):price:amount["\'][^>]+content=["\']([^"\']+)', r.text)
        p = parse_precio(m.group(1)) if m else None
        precios = [p] if precio_valido(p) else []
    if not precios:
        return None
    normales = [p["normal"] for p in prods if p.get("normal")]
    return {"precio": min(precios), "normal": max(normales) if normales else None}


# -------------------------------------------------------------- reglas

# ---------------------------------------------------------------- productos
#
# Un "producto" junta todos los avisos del mismo juego (misma plataforma y
# edicion) aunque cada tienda lo escriba distinto. El nombre oficial se toma
# del catalogo de la eShop cuando hay coincidencia.

STOP = {"the", "of", "and", "de", "la", "el", "los", "las", "y", "del", "for", "para",
        "a", "an", "en", "to", "with", "con", "por"}
RUIDO = {"juego", "videojuego", "videojuegos", "nintendo", "switch", "nsw", "fisico",
         "physical", "game", "games", "card", "version", "americano", "americana", "nuevo",
         "sellado", "original", "sniper", "snipercl", "cl", "megagames", "standard",
         "estandar", "digital", "rpg", "latam", "ntsc", "us", "usa", "edition"}
SINONIMOS = {"ed": "edition", "edicion": "edition"}
VARIANTES = {"collector", "coleccionista", "coleccion", "limited", "limitada", "steelbook",
             "japan", "japon", "japones", "import", "importado", "asia", "asiatica", "europe",
             "europa", "eur", "pal", "bundle", "signature", "premium"}
PLAT2_RE = re.compile(r"\b(nintendo switch 2 edition|switch 2 edition|switch 2|switch2|ns2|nsw2|sw2)\b")
SUFIJO_S2_RE = re.compile(r"\s*[–—-]\s*Nintendo Switch 2 Edition\s*|\s+Nintendo Switch 2 Edition\s*", re.I)


def plataforma_de(titulo):
    t = norm(re.sub(r"[’'´`]", "", titulo))
    return "Switch 2" if PLAT2_RE.search(t) else "Switch"


def tokens_de(titulo):
    t = norm(re.sub(r"[’'´`]", "", titulo))
    t = PLAT2_RE.sub(" ", t)
    out = []
    for w in t.split():
        w = SINONIMOS.get(w, w)
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if w not in STOP and w not in RUIDO:
            out.append(w)
    return out


def _con_pegados(toks):
    """tokens + pares pegados ('dream land' -> 'dreamland')."""
    s = set(toks)
    s.update(a + b for a, b in zip(toks, toks[1:]))
    return s


def emparejar(titulo, plataforma, candidatos):
    """Busca el juego de la eShop que corresponde a un titulo de tienda.
    candidatos: [{nombre, toks, plataforma, nsuid}] del mismo juego/saga."""
    st_toks = tokens_de(titulo)
    st = _con_pegados(st_toks)
    mejor, mejor_score = None, None
    for c in candidatos:
        ct = c["toks"]
        if not ct:
            continue
        cubiertos = {w for w in ct if w in st}
        for a, b in zip(ct, ct[1:]):
            if a + b in st:
                cubiertos.update((a, b))
        cov = len(cubiertos) / len(set(ct))
        if cov < 0.7 or len(cubiertos) < min(2, len(set(ct))):
            continue
        extra = len({w for w in st_toks if w not in ct and w not in VARIANTES
                     and not any(w == a + b for a, b in zip(ct, ct[1:]))})
        if cov < 1 and extra:
            continue      # le faltan palabras Y tiene otras distintas: es otro juego
        score = (cov, c["plataforma"] == plataforma, -extra, -len(ct))
        if mejor_score is None or score > mejor_score:
            mejor, mejor_score = c, score
    return mejor


def candidatos_eshop(state):
    """Catalogo eShop conocido, agrupado por juego (saga)."""
    por_juego = {}
    for nsuid, it in state.get("eshop", {}).items():
        if not it.get("titulo") or not it.get("juego"):
            continue
        nombre = SUFIJO_S2_RE.sub(" ", it["titulo"]).strip(" -–—")
        nombre = re.sub(r"\s{2,}", " ", nombre)
        plat = it.get("plataforma") or plataforma_de(it["titulo"])
        por_juego.setdefault(it["juego"], []).append({
            "nombre": nombre, "toks": tokens_de(nombre), "plataforma": plat, "nsuid": nsuid,
        })
    return por_juego


LIMPIAR_RE = re.compile(
    r"\b(video)?juegos?\b|\bnintendo\s+switch\s*2\b|\bnintendo\s+switch\b|\bswitch\s*2\b|"
    r"\bswitch\b|\bnsw2?\b|\bns2\b|\bsw2\b|\(\s*\)|\bf[ií]sico\b|\bphysical( game card)?\b|"
    r"\bpara\b(?=\s*$)|\bfor\b(?=\s*$)", re.I)


def limpiar_titulo(titulo):
    """'Videojuego Nintendo Switch 2 Hyrule Warriors - Switch' -> 'Hyrule Warriors'"""
    t = titulo
    for _ in range(3):
        t = LIMPIAR_RE.sub(" ", t)
        t = re.sub(r"\(\s*\)", " ", t)
        t = re.sub(r"\s*[-–—|]\s*(?=[-–—|]|$)", " ", t)    # guiones que quedaron colgando
        t = re.sub(r"\s+(for|para)\s*(?=[-–—|]|$)", " ", t, flags=re.I)
        t = re.sub(r"\s{2,}", " ", t).strip(" -–—|,:")
    return t or titulo.strip()


def clave_producto(listado, candidatos):
    """-> (clave, nombre, plataforma, variante, nsuid_o_None)"""
    plat = plataforma_de(listado["titulo"])
    c = emparejar(listado["titulo"], plat, candidatos)
    toks = set(tokens_de(listado["titulo"]))
    if c:
        mismos = [x for x in candidatos if x["nombre"] == c["nombre"]]
        if any(x["plataforma"] == plat for x in mismos):
            c = next(x for x in mismos if x["plataforma"] == plat)
        elif plat == "Switch":
            plat = c["plataforma"]          # la tienda no lo dice; el juego solo existe en S2
        variante = sorted((toks & VARIANTES) - set(c["toks"]))
        nombre, base, nsuid = c["nombre"], norm(c["nombre"]), c["nsuid"]
    else:
        variante = []
        nombre = limpiar_titulo(listado["titulo"])
        base, nsuid = " ".join(tokens_de(listado["titulo"])), None
    clave = f"{base}|{plat}" + (f"|{'-'.join(variante)}" if variante else "")
    return clave, nombre, plat, variante, nsuid


# ---------------------------------------------------------------- historial

DIAS_HISTORIAL = 400
DIAS_TIPICO = 90
MIN_DIAS = 7          # dias de datos antes de confiar en "tipico" y "minimo historico"


def hoy_chile():
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Santiago")).strftime("%Y-%m-%d")
    except Exception:
        return time.strftime("%Y-%m-%d", time.gmtime(time.time() - 3 * 3600))


def _mediana(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def registrar(serie, hoy, precio, tienda=None):
    """Guarda el minimo del dia y recorta lo antiguo."""
    previo = serie.get(hoy)
    valor_prev = previo[0] if isinstance(previo, list) else previo
    if valor_prev is None or precio < valor_prev:
        serie[hoy] = [precio, tienda] if tienda is not None else precio
    if len(serie) > DIAS_HISTORIAL:
        for d in sorted(serie)[: len(serie) - DIAS_HISTORIAL]:
            del serie[d]


def estadisticas(serie, hoy):
    """Usa solo dias anteriores a hoy."""
    prev = {d: (v[0] if isinstance(v, list) else v) for d, v in serie.items() if d < hoy}
    if not prev:
        return {"dias": 0}
    d_min = min(prev, key=lambda d: prev[d])
    tienda_min = serie[d_min][1] if isinstance(serie[d_min], list) else None
    recientes = [v for d, v in sorted(prev.items())[-DIAS_TIPICO:]]
    return {
        "dias": len(prev),
        "min": prev[d_min], "min_fecha": d_min, "min_tienda": tienda_min,
        "max": max(prev.values()),
        "tipico": _mediana(recientes) if len(prev) >= MIN_DIAS else None,
    }


# ---------------------------------------------------------------- reglas

def decidir(s, precio, motivos):
    """Anti-repeticion: avisa si hay motivo y el precio es menor que el ultimo avisado."""
    avisado = s.get("avisado")
    avisar = bool(motivos) and (avisado is None or precio < avisado)
    if avisar:
        PENDIENTES.append((s, avisado))
        s["avisado"] = precio
    elif not motivos and (avisado is None or precio > avisado):
        s["avisado"] = None
    return avisar


def ahora_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- revisar

def revisar_eshop(cfg, state, hist, alertas):
    aj = cfg.get("ajustes", {})
    pais = aj.get("pais_eshop", "US")
    desc_min = aj.get("descuento_minimo_pct", 25)
    hoy = hoy_chile()
    encontrados = {}  # nsuid -> (juego, hit)
    for juego in cfg.get("juegos", []):
        if juego.get("eshop") is False:
            continue
        for t in terminos(juego):
            try:
                hits = buscar_eshop(t)
            except (requests.RequestException, ValueError) as e:
                log(f"  eShop: fallo la busqueda '{t}': {e}")
                continue
            for h in hits:
                if coincide(h["titulo"], juego, exigir_plataforma=False):
                    encontrados.setdefault(h["nsuid"], (juego, h))
            time.sleep(0.3)
    log(f"  eShop: {len(encontrados)} juegos encontrados")
    if not encontrados:
        return
    try:
        precios = precios_eshop(encontrados.keys(), pais)
    except (requests.RequestException, ValueError) as e:
        log(f"  eShop: fallo la consulta de precios: {e}")
        return
    items = state.setdefault("eshop", {})
    hist_e = hist.setdefault("eshop", {})
    for nsuid, (juego, h) in sorted(encontrados.items(), key=lambda x: x[1][1]["titulo"]):
        p = precios.get(nsuid)
        if not p:
            continue
        actual = p["oferta"] if p["oferta"] is not None else p["regular"]
        serie = hist_e.setdefault(nsuid, {})
        est = estadisticas(serie, hoy)
        registrar(serie, hoy, actual)

        s = items.setdefault(nsuid, {})
        anterior = s.get("ultimo")
        s.update({"titulo": h["titulo"], "url": h["url"], "juego": juego["nombre"],
                  "plataforma": h.get("plataforma") or plataforma_de(h["titulo"]),
                  "origen": "eShop " + pais, "moneda": "USD", "fin": p["fin"],
                  "normal": p["regular"] if p["oferta"] is not None else None,
                  "regular": p["regular"], "ultimo": actual, "visto": ahora_iso(),
                  "min_hist": est.get("min"), "min_fecha": est.get("min_fecha"),
                  "dias": est["dias"]})

        pct = 100 * (1 - actual / p["regular"]) if p["regular"] else 0
        objetivo = juego.get("objetivo_usd")
        motivos = []
        if objetivo is not None and actual <= objetivo:
            motivos.append("objetivo")
        if est["dias"] >= MIN_DIAS and est.get("min") and actual < est["min"] * 0.99:
            motivos.append("minimo")
        if pct >= desc_min:
            motivos.append("descuento")
        if anterior and 100 * (1 - actual / anterior) >= aj.get("baja_minima_pct", 10):
            motivos.append("baja")
        log(f"    {h['titulo']}: {usd(actual)}" + (f" (normal {usd(p['regular'])})"
                                                   if p["oferta"] is not None else ""))
        if decidir(s, actual, motivos):
            linea = f'<a href="{esc_attr(h["url"])}">{esc(h["titulo"])}</a>\n   💸 {usd(actual)}'
            if p["oferta"] is not None:
                linea += f"  <s>{usd(p['regular'])}</s>  −{round(pct)}%"
            if p["fin"]:
                linea += f"  ⏳ hasta {esc(p['fin'][:10])}"
            if "minimo" in motivos:
                linea += "\n   🏆 mínimo histórico"
            if "objetivo" in motivos:
                linea += "  🎯"
            alertas.setdefault(("🎮", juego["nombre"], "eShop " + pais), []).append(linea)


def leer_tiendas(cfg, state, alertas):
    """Busca en cada tienda. Devuelve [(juego, tienda, listado)]."""
    fallos = state.setdefault("fallos", {})
    juegos = cfg.get("juegos", [])
    listados = []
    for tienda in cfg.get("tiendas", []):
        if tienda.get("activa") is False:
            continue
        nombre_t = tienda["nombre"]
        cache = {}
        hallados = {}  # url -> (juego, producto)
        respondio = False
        for juego in juegos:
            if juego.get("fisico") is False:
                continue
            for t in terminos(juego):
                prods = buscar_en_tienda(tienda, t, state, cache)
                if prods is None:
                    continue
                respondio = True
                for p in prods:
                    if precio_valido(p.get("precio")) and coincide(p["titulo"], juego, True):
                        hallados.setdefault(p["url"], (juego, p))
                if cache.get("metodo") != "shopify":
                    time.sleep(PAUSA)
            if not respondio and cache.get("diag") and len(cache["diag"]) > 12:
                break  # la tienda no responde: no seguir insistiendo esta corrida

        if not respondio:
            fallos[nombre_t] = fallos.get(nombre_t, 0) + 1
            log(f"  {nombre_t}: sin respuesta util (fallo {fallos[nombre_t]} seguido)")
            for d in cache.get("diag", [])[-4:]:
                log(f"      {d}")
            if fallos[nombre_t] == FALLOS_PARA_AVISAR:
                alertas.setdefault(("⚠️", "Avisos", ""), []).append(
                    f"Llevo {FALLOS_PARA_AVISAR} revisiones sin poder leer "
                    f"<b>{esc(nombre_t)}</b>. Puede que bloquee al bot o que haya "
                    f"cambiado su página. Corre el diagnóstico y pásame el resultado."
                )
            continue
        fallos.pop(nombre_t, None)
        log(f"  {nombre_t} [{cache.get('metodo')}]: {len(hallados)} avisos que calzan")
        listados += [(j, nombre_t, p) for j, p in hallados.values()]

    # links puestos a mano (para tiendas que no se dejan buscar)
    for juego in juegos:
        for url in juego.get("links", []):
            try:
                info = precio_de_pagina(url)
            except requests.RequestException:
                info = None
            if not info:
                log(f"  link manual sin precio: {url}")
                continue
            dom = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
            listados.append((juego, dom, {"titulo": juego["nombre"], "url": url,
                                          "precio": info["precio"], "normal": info["normal"]}))
    return listados


def revisar_productos(cfg, state, hist, listados, alertas):
    aj = cfg.get("ajustes", {})
    desc_min = aj.get("descuento_minimo_pct", 25)
    baja_min = aj.get("baja_minima_pct", 10)
    hoy = hoy_chile()
    cands = candidatos_eshop(state)
    eshop = state.get("eshop", {})
    hist_p = hist.setdefault("productos", {})
    productos = state.setdefault("productos", {})

    grupos = {}
    for juego, tienda, l in listados:
        clave, nombre, plat, variante, nsuid = clave_producto(l, cands.get(juego["nombre"], []))
        g = grupos.setdefault(clave, {"juego": juego, "nombre": nombre, "plataforma": plat,
                                      "variante": variante, "nsuid": nsuid, "avisos": []})
        g["avisos"].append({"tienda": tienda, "precio": l["precio"], "url": l["url"],
                            "titulo": l["titulo"], "normal": l.get("normal")})
    log(f"  {len(listados)} avisos agrupados en {len(grupos)} productos")

    for clave, g in sorted(grupos.items(), key=lambda x: (x[1]["juego"]["nombre"], x[1]["nombre"])):
        juego = g["juego"]
        # el mejor precio de cada tienda
        por_tienda = {}
        for a in g["avisos"]:
            if a["tienda"] not in por_tienda or a["precio"] < por_tienda[a["tienda"]]["precio"]:
                por_tienda[a["tienda"]] = a
        avisos = sorted(por_tienda.values(), key=lambda a: a["precio"])
        mejor = avisos[0]
        precio = mejor["precio"]

        h = hist_p.setdefault(clave, {})
        h.update({"nombre": g["nombre"], "plataforma": g["plataforma"], "juego": juego["nombre"]})
        serie = h.setdefault("f", {})
        est = estadisticas(serie, hoy)
        registrar(serie, hoy, precio, mejor["tienda"])

        # precio de referencia: tipico propio > otras tiendas > lo que dice la tienda
        otras = [a["precio"] for a in avisos[1:]]
        if est.get("tipico"):
            ref, ref_tipo = est["tipico"], "tipico"
        elif len(otras) >= 2:
            ref, ref_tipo = _mediana(otras), "mercado"
        elif mejor.get("normal") and mejor["normal"] > precio:
            ref, ref_tipo = mejor["normal"], "tienda"
        elif otras:
            ref, ref_tipo = otras[0], "mercado"
        else:
            ref, ref_tipo = None, None
        pct = 100 * (1 - precio / ref) if ref else 0

        s = productos.setdefault(clave, {})
        anterior = s.get("ultimo")
        e = eshop.get(g["nsuid"]) if g["nsuid"] else None
        s.update({
            "nombre": g["nombre"], "plataforma": g["plataforma"], "variante": g["variante"],
            "juego": juego["nombre"], "precio": precio, "tienda": mejor["tienda"],
            "url": mejor["url"], "ref": ref, "ref_tipo": ref_tipo, "pct": round(pct, 1),
            "min_hist": est.get("min"), "min_fecha": est.get("min_fecha"),
            "min_tienda": est.get("min_tienda"), "tipico": est.get("tipico"), "dias": est["dias"],
            "avisos": [{k: a[k] for k in ("tienda", "precio", "url", "titulo")} for a in avisos],
            "nsuid": g["nsuid"], "ultimo": precio, "visto": ahora_iso(),
        })

        motivos = []
        objetivo = juego.get("objetivo_clp")
        if objetivo is not None and precio <= objetivo:
            motivos.append("objetivo")
        if est["dias"] >= MIN_DIAS and est.get("min") and precio < est["min"] * 0.99:
            motivos.append("minimo")
        if ref and pct >= desc_min:
            motivos.append("descuento")
        if anterior and 100 * (1 - precio / anterior) >= baja_min:
            motivos.append("baja")

        if decidir(s, precio, motivos):
            alertas.setdefault(("📦", juego["nombre"], "tiendas"), []).append(
                linea_producto(s, motivos, anterior, e))


def linea_producto(s, motivos, anterior, e):
    nombre = s["nombre"] + (f" ({' '.join(s['variante'])})" if s.get("variante") else "")
    l = (f'<a href="{esc_attr(s["url"])}">{esc(nombre)}</a> · {esc(s["plataforma"])}\n'
         f'   💸 {clp(s["precio"])} en {esc(s["tienda"])}')
    if "minimo" in motivos:
        l += "  🏆 mínimo histórico"
    if "objetivo" in motivos:
        l += "  🎯"
    detalles = []
    if s.get("ref") and s["pct"] >= 1:
        txt = {"tipico": "vs su precio típico", "mercado": "vs otras tiendas",
               "tienda": "según la tienda"}[s["ref_tipo"]]
        detalles.append(f"−{round(s['pct'])}% {txt} ({clp(s['ref'])})")
    elif "baja" in motivos and anterior:
        detalles.append(f"📉 bajó desde {clp(anterior)}")
    if len(s["avisos"]) > 1:
        detalles.append(f"{len(s['avisos'])} tiendas")
    if e and e.get("ultimo"):
        detalles.append(f"🎮 {usd(e['ultimo'])}")
    if detalles:
        l += "\n   📊 " + " · ".join(detalles)
    return l


# ------------------------------------------------------------------ telegram

def send_telegram(text):
    if os.environ.get("DRY_RUN"):
        log("[DRY_RUN]\n" + re.sub(r"<[^>]+>", "", text))
        return True
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log("ERROR: faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            log(f"ERROR Telegram {r.status_code}: {r.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        log(f"ERROR Telegram: {e}")
        return False


def armar_mensajes(alertas):
    """Agrupa por juego y corta en mensajes que caben en Telegram."""
    bloques = []
    for (icono, juego, origen), lineas in sorted(alertas.items(),
                                                 key=lambda x: (x[0][0] == "⚠️", x[0][1], x[0][0])):
        cab = f"{icono} <b>{esc(juego)}</b>" + (f" — {esc(origen)}" if origen else "")
        bloques.append([cab] + lineas)
    mensajes, actual = [], ""
    for b in bloques:
        for i, linea in enumerate(b):
            sep = "" if not actual else ("\n\n" if i == 0 else "\n")
            if actual and len(actual) + len(sep) + len(linea) > TELEGRAM_MAX:
                mensajes.append(actual)
                actual = (b[0] + " (sigue)\n" + linea) if i else linea
            else:
                actual += sep + linea
    if actual:
        mensajes.append(actual)
    return mensajes


# ------------------------------------------------------------- diagnostico

def diagnostico(cfg):
    termino = sys.argv[sys.argv.index("--diagnostico") + 1] \
        if len(sys.argv) > sys.argv.index("--diagnostico") + 1 else "zelda"
    juego = {"nombre": termino, "buscar": termino}
    log(f"=== DIAGNOSTICO con la busqueda '{termino}' ===\n")
    log("eShop EE. UU.:")
    try:
        hits = [h for h in buscar_eshop(termino) if coincide(h["titulo"], juego, False)]
        precios = precios_eshop([h["nsuid"] for h in hits[:20]], "US")
        log(f"  {len(hits)} juegos")
        for h in hits[:8]:
            p = precios.get(h["nsuid"])
            log(f"  - {h['titulo']} [{h['nsuid']}] "
                + (usd(p['oferta'] if p['oferta'] is not None else p['regular']) if p else "sin precio"))
    except (requests.RequestException, ValueError) as e:
        log(f"  ERROR: {e}")
    for tienda in cfg.get("tiendas", []):
        log(f"\n{tienda['nombre']} ({tienda['url']}):")
        cache = {}
        prods = buscar_en_tienda(tienda, termino, {}, cache)
        for d in cache.get("diag", []):
            log(f"  · {d}")
        if prods is None:
            log("  => NO FUNCIONA")
            continue
        ok = [p for p in prods if precio_valido(p.get("precio")) and coincide(p["titulo"], juego, True)]
        log(f"  => metodo {cache.get('metodo')}: {len(prods)} productos leidos, {len(ok)} calzan")
        for p in ok[:6]:
            log(f"  - {p['titulo']} | {clp(p['precio'])}"
                + (f" (normal {clp(p['normal'])})" if p.get("normal") else "") + f" | {p['url']}")
        if not ok:
            for p in prods[:3]:
                log(f"  ? {p.get('titulo')} | {p.get('precio')} | {p.get('url')}")


# ---------------------------------------------------------------------- main

def main():
    if "--test" in sys.argv:
        ok = send_telegram("👑 SwitchBot está conectado y funcionando.")
        sys.exit(0 if ok else 1)

    cfg = load_json(CONFIG_FILE, None)
    if cfg is None:
        log(f"ERROR: no existe {CONFIG_FILE.name}")
        sys.exit(1)

    if "--diagnostico" in sys.argv:
        global VOLCAR
        VOLCAR = BASE / "diagnostico"
        diagnostico(cfg)
        return

    state = load_json(STATE_FILE, {})
    primera_vez = "productos" not in state
    alertas = {}

    hist = load_json(HIST_FILE, {})
    log("Revisando eShop...")
    revisar_eshop(cfg, state, hist, alertas)
    log("Revisando tiendas...")
    listados = leer_tiendas(cfg, state, alertas)
    revisar_productos(cfg, state, hist, listados, alertas)
    state.pop("tiendas", None)          # formato anterior (por aviso), ya no se usa
    for viejo in ("digitales", "fisicos", "nsuids"):
        state.pop(viejo, None)
    state["ultima_revision"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    mensajes = armar_mensajes(alertas)
    if mensajes and primera_vez:
        mensajes.insert(0, "👑 <b>Primera revisión de SwitchBot</b>\n"
                           "Estas son las ofertas que ya estaban activas. "
                           "Desde ahora solo te aviso de las nuevas.")
    enviados = sum(1 for m in mensajes if send_telegram(m))
    if enviados < len(mensajes):
        # no llegaron: se deshacen las marcas para que se vuelvan a avisar la proxima vez
        for s, previo in PENDIENTES:
            s["avisado"] = previo
        log("Telegram fallo: estas ofertas se volveran a intentar en la proxima revision.")
    save_json(STATE_FILE, state)
    save_json(HIST_FILE, hist)
    log(f"Listo: {enviados}/{len(mensajes)} mensajes enviados.")


if __name__ == "__main__":
    main()
