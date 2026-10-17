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
SEG_FILE = BASE / "seguimiento.json"     # lo escribe el Worker con los botones de Telegram
SEG = {}

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

PLATAFORMA = {"switch", "nsw", "ns2", "sw2", "switch2", "nsw2"}
EXCLUIR_SIEMPRE = {
    "amiibo", "funda", "estuche", "case", "carcasa", "protector", "mica",
    "control", "controles", "joy", "joycon", "figura", "figuras", "peluche",
    "polera", "poleron", "poster", "libro", "guia", "artbook", "llavero",
    "taza", "mochila", "lamina", "laminas", "skin", "skins", "cable",
    "soporte", "consola", "audifono", "audifonos", "tarjeta", "gift",
    "cargador", "dock", "grip", "volante", "teclado", "mouse", "bolso",
    "cartas", "carta", "tcg", "booster", "sobre", "sobres", "mazo", "album",
    "juguete", "plush", "pin", "pins", "gorro", "chaqueta", "lampara",
    "cojin", "manta", "billetera", "stickers", "sticker", "auriculares", "audifonos",
    "almacenamiento", "volantes", "compatibles", "compatible", "accesorio", "accesorios",
    "premgear", "kit", "estuches", "fundas", "protectores", "lapiz", "tarjetero", "triptico",
    "powera", "hori", "organizador", "portajuegos", "porta", "cartuchos", "vidrio", "templado",
    "bateria", "joystick", "joysticks", "stand", "bolsa", "maletin", "adaptador", "hub", "led",
    "mousepad", "alfombra", "camiseta", "pijama", "disfraz", "vaso", "botella", "cuaderno",
    "membresia", "suscripcion", "codigo", "recarga", "prepago", "voucher",
}

# juegos encontrados fuera de la lista de sagas (catalogo completo de tiendas
# Shopify y ofertas de la eShop)
JUEGO_OTROS = {"nombre": "🔍 Otros juegos", "buscar": ["__descubiertos__"], "auto": True}

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

# Datos extra del catalogo para curar el canal (editora, generos...). Nintendo no
# documenta estos campos, asi que se prueban varios nombres y se ignora lo que falte.
META_ALGOLIA = {
    "editora": ("softwarePublisher", "publisher", "publishers"),
    "desarrollador": ("softwareDeveloper", "developer", "developers"),
    "generos": ("genres", "genre"),
    "lanzamiento": ("releaseDate", "releaseDateDisplay"),
}


def meta_algolia(h):
    out = {}
    for campo, claves in META_ALGOLIA.items():
        for k in claves:
            v = h.get(k)
            if isinstance(v, list):
                v = [str(x).strip() for x in v if str(x).strip()]
                if campo in ("editora", "desarrollador"):
                    v = ", ".join(v)
            if isinstance(v, str):
                v = v.strip()
            if v:
                out[campo] = v
                break
    return out


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
            **meta_algolia(h),
        })
    return out


# eShop de America comparten el mismo ID de juego (NSUID)
REGIONES_AMERICA = ["US", "CA", "MX", "BR", "CL", "CO", "PE"]
NOMBRE_PAIS = {"US": "🇺🇸 EE. UU.", "CA": "🇨🇦 Canadá", "MX": "🇲🇽 México", "BR": "🇧🇷 Brasil",
               "CL": "🇨🇱 Chile", "CO": "🇨🇴 Colombia", "PE": "🇵🇪 Perú", "AR": "🇦🇷 Argentina"}
FX_URL = "https://open.er-api.com/v6/latest/USD"


def tipo_cambio(state):
    """{moneda: unidades por 1 USD}. Si falla, usa el ultimo guardado."""
    try:
        r = get(FX_URL)
        r.raise_for_status()
        rates = r.json().get("rates") or {}
        if rates.get("CLP"):
            state["fx"] = {"rates": rates, "fecha": ahora_iso()}
            return rates
    except (requests.RequestException, ValueError) as e:
        log(f"  dolar: no pude actualizar ({e}); uso el ultimo guardado")
    return (state.get("fx") or {}).get("rates") or {}


def a_usd(valor, moneda, rates):
    if valor is None:
        return None
    if moneda == "USD":
        return valor
    r = rates.get(moneda)
    return valor / r if r else None


def precios_eshop_regiones(nsuids, regiones, rates):
    """{nsuid: {pais: {regular_usd, actual_usd, local, moneda, fin}}}"""
    out = {}
    for pais in regiones:
        try:
            pr = precios_eshop(nsuids, pais, crudo=True)
        except (requests.RequestException, ValueError) as e:
            log(f"  eShop {pais}: fallo ({e})")
            continue
        for nsuid, p in pr.items():
            mon = p["moneda"]
            reg = a_usd(p["regular"], mon, rates)
            act = a_usd(p["oferta"] if p["oferta"] is not None else p["regular"], mon, rates)
            if reg is None or act is None:
                continue
            out.setdefault(nsuid, {})[pais] = {
                "regular_usd": round(reg, 2), "actual_usd": round(act, 2),
                "local": p["oferta"] if p["oferta"] is not None else p["regular"],
                "moneda": mon, "fin": p["fin"], "oferta": p["oferta"] is not None,
            }
        time.sleep(0.3)
    return out


FILTROS_OFERTA = ('topLevelFilters:"Deals"', "price.discounted:true", 'availability:"On sale"')


def buscar_ofertas_eshop(maximo=3000):
    """Todos los juegos con descuento en la eShop de EE. UU. (no solo tus sagas).
    Prueba varios filtros porque Nintendo los cambia de vez en cuando."""
    for filtro in FILTROS_OFERTA:
        out, page = [], 0
        try:
            while len(out) < maximo:
                r = get(ALGOLIA_URL, headers={**HEADERS, **ALGOLIA_HEADERS},
                        params={"query": "", "filters": filtro, "hitsPerPage": 500, "page": page})
                if r.status_code != 200:
                    break
                data = r.json()
                for h in data.get("hits", []):
                    nsuid = str(h.get("nsuid") or "")
                    if not nsuid.startswith("7001"):
                        continue
                    url = h.get("url") or ""
                    out.append({"titulo": re.sub(r"[™®©]", "", h.get("title", nsuid)).strip(),
                                "nsuid": nsuid, "url": urljoin(NINTENDO_WEB, url) if url else "",
                                "plataforma": "Switch 2" if h.get("platformCode") == "NINTENDO_SWITCH_2" else "Switch",
                                **meta_algolia(h)})
                page += 1
                if page >= (data.get("nbPages") or 0):
                    break
                time.sleep(0.3)
        except (requests.RequestException, ValueError) as e:
            log(f"  eShop ofertas [{filtro}]: {e}")
            continue
        if out:
            log(f"  eShop ofertas: filtro {filtro} -> {len(out)} juegos")
            return out
        log(f"  eShop ofertas: filtro {filtro} sin resultados")
    return []


def precios_eshop(nsuids, pais, crudo=False):
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
                "moneda": reg.get("currency") or "USD",
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
                            "normal": normal if normal and normal > precio else None,
                            "vendedor": _vendedor(nodo)})
    for v in nodo.values():
        if isinstance(v, (dict, list)):
            productos_en_json(v, url_pagina, out, vistos)


def _vendedor(nodo):
    """Nombre del vendedor si la pagina lo informa (marketplace)."""
    for k, v in nodo.items():
        kl = k.lower()
        if "seller" not in kl and "vendedor" not in kl and kl != "soldby":
            continue
        if "id" in kl and "name" not in kl:
            continue
        if isinstance(v, str) and v.strip() and not v.strip().isdigit():
            return v.strip()
        if isinstance(v, list) and v and isinstance(v[0], (str, dict)):
            v = v[0]
            if isinstance(v, str) and v.strip():
                return v.strip()
        if isinstance(v, dict):
            for kk in ("name", "displayName", "sellerName", "nombre"):
                if isinstance(v.get(kk), str) and v[kk].strip():
                    return v[kk].strip()
    return None


# vendedores externos que se cuelan en los marketplaces (Falabella, Lider…)
EXTERNOS_RE = re.compile(r"sniper|megagames?\b|\bimportado por\b|\bvendido por\b", re.I)


def es_externo(producto, tienda):
    """True si el aviso es de un vendedor externo y no de la tienda misma."""
    if EXTERNOS_RE.search(producto.get("titulo", "")) or EXTERNOS_RE.search(producto.get("url", "")):
        return True
    v = producto.get("vendedor")
    if not v:
        return False
    oficiales = [norm(tienda["nombre"])] + [norm(x) for x in tienda.get("vendedores_ok", [])]
    nv = norm(v)
    return not any(o and (o in nv or nv in o) for o in oficiales)


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


def _texto_rsc(pagina):
    """Junta los datos de paginas Next.js nuevas (self.__next_f.push) ya desescapados."""
    trozos = []
    for s in re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', pagina):
        try:
            trozos.append(json.loads('"' + s + '"'))
        except ValueError:
            pass
    return "".join(trozos) if trozos else pagina


def _cent(p):
    if isinstance(p, dict):
        v = p.get("value", p)
        if isinstance(v, dict) and v.get("centAmount") is not None:
            return float(v["centAmount"]) / (10 ** int(v.get("fractionDigits") or 0))
    return None


def parse_rsc(pagina, url_pagina):
    """Listados tipo Paris/Cencosud: "products":[{name, slug, prices{regular,offer}, sellers}]"""
    texto = _texto_rsc(pagina)
    dec = json.JSONDecoder()
    out, vistos = [], set()
    for m in re.finditer(r'"products":\[', texto):
        try:
            lista, _ = dec.raw_decode(texto, m.end() - 1)
        except ValueError:
            continue
        for p in lista:
            if not isinstance(p, dict) or not p.get("name") or not p.get("slug"):
                continue
            pr = p.get("prices") or (p.get("masterVariant") or {}).get("prices") or {}
            oferta, regular = _cent(pr.get("offer")), _cent(pr.get("regular"))
            precio = oferta or regular
            if not precio_valido(precio):
                continue
            url = urljoin(url_pagina, "/" + p["slug"].strip("/") + ".html")
            if url in vistos:
                continue
            vistos.add(url)
            sellers = p.get("sellers") if isinstance(p.get("sellers"), list) else []
            out.append({"titulo": html.unescape(p["name"]), "url": url, "precio": precio,
                        "normal": regular if regular and regular > precio else None,
                        "vendedor": sellers[0] if sellers and isinstance(sellers[0], str) else None})
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
        prods = parse_magento(r.text, r.url) or parse_rsc(r.text, r.url) or parse_embebido(r.text, r.url)
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


# paginas de busqueda conocidas (si la tienda no trae "busqueda" propia)
BUSQUEDA_CONOCIDA = {
    "paris.cl": "https://www.paris.cl/search/?q={q}",
    "falabella.com": "https://www.falabella.com/falabella-cl/search?Ntt={q}",
    "lider.cl": "https://www.lider.cl/search?q={q}",
}


def buscar_en_tienda(tienda, termino, state, cache):
    if not tienda.get("busqueda"):
        for dom, plantilla in BUSQUEDA_CONOCIDA.items():
            if dom in tienda["url"]:
                tienda = {**tienda, "busqueda": plantilla}
                break
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
SINONIMOS = {"ed": "edition", "edicion": "edition", "encarcelation": "imprisonment",
             "encarcelamiento": "imprisonment"}
VARIANTES = {"collector", "coleccionista", "coleccion", "limited", "limitada", "steelbook",
             "japan", "japon", "japones", "import", "importado", "asia", "asiatica", "europe",
             "europa", "eur", "pal", "bundle", "signature", "premium", "usado"}
USADO_RE = re.compile(r"\b(usad[oa]s?|seminuev[oa]|reacondicionad[oa]|refurbished|open.?box|"
                      r"caja abierta|segunda mano|pre.?owned)\b", re.I)
PLAT2_RE = re.compile(r"\b(nintendo switch 2 edition|switch 2 edition|switch 2|switch2|ns2|nsw2|sw2)\b")
SUFIJO_S2_RE = re.compile(r"\s*[–—-]\s*Nintendo Switch 2 Edition\s*|\s+Nintendo Switch 2 Edition\s*", re.I)


# palabras de empaque o de la tienda que no cambian el juego (no separan productos)
NEUTRAS = {"re", "run", "rerun", "lrg", "mapa", "dlc", "regalo", "exclusivo", "incluye", "bloc",
           "nota", "adhesiva", "cover", "uae", "especial", "special", "goty", "year", "anniversary",
           "aniversario", "booster", "course", "pas", "pass", "expansion", "bonus", "preventa",
           "sellado", "nuevo", "fisica", "fisico", "square", "enix", "bandai", "namco",
           "entertainment", "capcom", "sega", "atlus", "konami", "ubisoft", "warner", "nicalis"}
# juegos cuyo nombre es una sola palabra comun: exigen calce exacto
GENERICAS = {"sport", "sports", "party", "racing", "world", "adventure", "battle", "collection",
             "fighter", "golf", "tennis", "soccer", "football", "dance", "music", "puzzle"}


def plataforma_de(titulo):
    t = norm(re.sub(r"[’'´`]", "", titulo))
    return "Switch 2" if PLAT2_RE.search(t) else "Switch"


# titulos que las tiendas chilenas escriben en espanol -> nombre de la eShop
TRADUCCIONES = [
    ("lagrimas del reino", "tears of the kingdom"),
    ("aliento de lo salvaje", "breath of the wild"),
    ("aliento salvaje", "breath of the wild"),
    ("ecos de la sabiduria", "echoes of wisdom"),
    ("el despertar de link", "links awakening"),
    ("la tierra olvidada", "the forgotten land"),
    ("tierra olvidada", "forgotten land"),
    ("la era del cataclismo", "age of calamity"),
    ("era del cataclismo", "age of calamity"),
    ("la era del destierro", "age of imprisonment"),
    ("era del destierro", "age of imprisonment"),
    ("la era del encarcelamiento", "age of imprisonment"),
    ("espada celeste", "skyward sword"),
    ("leyendas pokemon", "pokemon legends"),
    ("pokemon escarlata", "pokemon scarlet"),
    ("pokemon purpura", "pokemon violet"),
    ("pokemon espada", "pokemon sword"),
    ("pokemon escudo", "pokemon shield"),
    ("diamante brillante", "brilliant diamond"),
    ("perla reluciente", "shining pearl"),
    ("vamos pikachu", "lets go pikachu"),
    ("vamos eevee", "lets go eevee"),
    ("el regreso a dream land", "return to dream land"),
    ("regreso a dream land", "return to dream land"),
    ("estrellas aliadas", "star allies"),
    ("la leyenda de zelda", "the legend of zelda"),
    ("leyenda de zelda", "legend of zelda"),
]
_TRAD_RE = [(re.compile(r"\b" + a + r"\b"), c) for a, c in TRADUCCIONES]


def traducir(texto_norm):
    for rx, c in _TRAD_RE:
        texto_norm = rx.sub(c, texto_norm)
    return texto_norm


def tokens_de(titulo):
    t = traducir(norm(re.sub(r"[’'´`]", "", titulo)))
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
        extra = len({w for w in st_toks if w not in ct and w not in VARIANTES and w not in NEUTRAS
                     and not any(w == a + b for a, b in zip(ct, ct[1:]))})
        if cov < 1 and extra:
            continue      # le faltan palabras Y tiene otras distintas: es otro juego
        if len(set(ct)) == 1 and ct[0] in GENERICAS and extra:
            continue      # ej. "Sports Party" no es "Nintendo Switch Sports"
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
    if USADO_RE.search(listado["titulo"]) or USADO_RE.search(listado.get("url", "").replace("-", " ")):
        toks.add("usado")
    if c:
        mismos = [x for x in candidatos if x["nombre"] == c["nombre"]]
        if any(x["plataforma"] == plat for x in mismos):
            c = next(x for x in mismos if x["plataforma"] == plat)
        elif plat == "Switch":
            plat = c["plataforma"]          # la tienda no lo dice; el juego solo existe en S2
        variante = sorted((toks & VARIANTES) - set(c["toks"]))
        nombre, base, nsuid = c["nombre"], norm(c["nombre"]), c["nsuid"]
    else:
        variante = ["usado"] if "usado" in toks else []
        nombre = limpiar_titulo(USADO_RE.sub(" ", listado["titulo"]))
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


def id_de(tipo, clave):
    """Id corto y estable de una tarjeta (cabe en los botones de Telegram)."""
    import hashlib
    return hashlib.sha1(f"{tipo}|{clave}".encode()).hexdigest()[:10]


def seguimiento(id_):
    """(objetivo, seguido, silenciado_hasta) puestos con los botones."""
    obj = (SEG.get("objetivos") or {}).get(id_)
    seguido = id_ in (SEG.get("seguidos") or {})
    sil = (SEG.get("silenciados") or {}).get(id_)
    if sil and sil.get("hasta") and sil["hasta"] < hoy_chile():
        sil = None
    return (obj.get("precio") if obj else None), seguido, (sil.get("hasta") or "siempre") if sil else None


def registrar_detalle(h, hoy, avisos):
    """Datos para funciones futuras (descuentos falsos, escasez, prediccion):
    - t: precio y "antes" de cada tienda por dia
    - n: en cuantas tiendas estaba disponible ese dia"""
    t = h.setdefault("t", {})
    for a in avisos:
        dia = t.setdefault(a["tienda"], {})
        dia[hoy] = [a["precio"], a.get("normal")] if a.get("normal") else a["precio"]
        if len(dia) > DIAS_HISTORIAL:
            for d in sorted(dia)[: len(dia) - DIAS_HISTORIAL]:
                del dia[d]
    n = h.setdefault("n", {})
    n[hoy] = len(avisos)
    if len(n) > DIAS_HISTORIAL:
        for d in sorted(n)[: len(n) - DIAS_HISTORIAL]:
            del n[d]


def registrar_oferta_eshop(hist, nsuid, titulo, hoy, regs):
    """Calendario de ofertas de la eShop (para predecir cuando vuelven):
    una entrada por oferta y pais, con inicio, fin y % de descuento."""
    cal = hist.setdefault("eshop_ofertas", {}).setdefault(nsuid, {"titulo": titulo, "o": []})
    cal["titulo"] = titulo
    for pais, r in regs.items():
        if not r.get("oferta") or not r.get("regular_usd"):
            continue
        pct = round(100 * (1 - r["actual_usd"] / r["regular_usd"]))
        fin = (r.get("fin") or "")[:10]
        prev = next((o for o in cal["o"] if o["pais"] == pais and o.get("fin") == fin and o["pct"] == pct), None)
        if prev:
            prev["visto"] = hoy
        else:
            cal["o"].append({"pais": pais, "desde": hoy, "visto": hoy, "fin": fin, "pct": pct,
                             "usd": r["actual_usd"]})
    cal["o"] = cal["o"][-200:]


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

def decidir(s, precio, motivos, nuevo=False):
    """Anti-repeticion: avisa si hay motivo y el precio es menor que el ultimo avisado.
    Un producto visto por primera vez se registra en silencio (se ve con /ofertas)."""
    avisado = s.get("avisado")
    if nuevo:
        s["avisado"] = precio if motivos else None
        return False
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
                    prev = encontrados.get(h["nsuid"])
                    esp = max(len(palabras(x)) for x in terminos(juego))
                    if not prev or esp > max(len(palabras(x)) for x in terminos(prev[0])):
                        encontrados[h["nsuid"]] = (juego, h)
            time.sleep(0.3)
    if aj.get("descubrir", True):
        nuevos = 0
        for h in buscar_ofertas_eshop():
            if h["nsuid"] not in encontrados:
                encontrados[h["nsuid"]] = (JUEGO_OTROS, h)
                nuevos += 1
        log(f"  eShop: {nuevos} juegos en oferta descubiertos fuera de tu lista")
    log(f"  eShop: {len(encontrados)} juegos encontrados")
    if not encontrados:
        return
    regiones = aj.get("regiones_eshop") or REGIONES_AMERICA
    if pais not in regiones:
        regiones = [pais] + list(regiones)
    rates = tipo_cambio(state)
    if not rates:
        regiones = [pais]
    porreg = precios_eshop_regiones(list(encontrados.keys()), regiones, rates or {"USD": 1})
    if not porreg:
        log("  eShop: no hubo precios")
        return
    clp_x_usd = (rates or {}).get("CLP")
    items = state.setdefault("eshop", {})
    hist_e = hist.setdefault("eshop", {})
    for nsuid, (juego, h) in sorted(encontrados.items(), key=lambda x: x[1][1]["titulo"]):
        regs = porreg.get(nsuid)
        if not regs:
            continue
        base = regs.get(pais) or next(iter(regs.values()))
        mejor_pais = min(regs, key=lambda k: regs[k]["actual_usd"])
        mejor = regs[mejor_pais]
        actual = mejor["actual_usd"]
        regular = base["regular_usd"]
        # oferta real = rebaja dentro de ese mismo pais (no la diferencia entre paises)
        pct_oferta = 100 * (1 - actual / mejor["regular_usd"]) if mejor["oferta"] else 0
        ahorro_region = 100 * (1 - actual / base["actual_usd"]) if base["actual_usd"] else 0
        p = {"oferta": base["actual_usd"] if base["oferta"] else None, "regular": regular,
             "fin": mejor["fin"] or base["fin"]}
        serie = hist_e.setdefault(nsuid, {})
        est = estadisticas(serie, hoy)
        registrar(serie, hoy, actual, mejor_pais)
        registrar_oferta_eshop(hist, nsuid, h["titulo"], hoy, regs)

        s = items.setdefault(nsuid, {})
        anterior = s.get("ultimo")
        s.update({"titulo": h["titulo"], "url": h["url"], "juego": juego["nombre"],
                  "plataforma": h.get("plataforma") or plataforma_de(h["titulo"]),
                  "origen": "eShop " + mejor_pais, "moneda": "USD", "fin": p["fin"],
                  "normal": mejor["regular_usd"] if mejor["oferta"] else None,
                  "ahorro_region": round(ahorro_region, 1),
                  "regular": regular, "ultimo": actual, "visto": ahora_iso(),
                  "pais": mejor_pais, "local": mejor["local"], "moneda_local": mejor["moneda"],
                  "precio_us": base["actual_usd"],
                  "clp": round(actual * clp_x_usd) if clp_x_usd else None,
                  "regiones": {k: v["actual_usd"] for k, v in regs.items()},
                  "regiones_normal": {k: v["regular_usd"] for k, v in regs.items()},
                  **{k: h[k] for k in META_ALGOLIA if h.get(k)},
                  "min_hist": est.get("min"), "min_fecha": est.get("min_fecha"),
                  "pct_oferta": round(pct_oferta, 1), "objetivo": juego.get("objetivo_usd"),
                  "dias": est["dias"]})

        pct = pct_oferta
        id_ = id_de("d", nsuid)
        obj_seg, seguido, silenciado = seguimiento(id_)
        objetivo = obj_seg if obj_seg is not None else juego.get("objetivo_usd")
        s.update({"id": id_, "objetivo": objetivo, "seguido": seguido, "silenciado": silenciado})
        motivos = []
        if seguido and anterior and actual < anterior - 0.005:
            motivos.append("seguido")
        if objetivo is not None and actual <= objetivo:
            motivos.append("objetivo")
        if est["dias"] >= MIN_DIAS and est.get("min") and actual < est["min"] * 0.99:
            motivos.append("minimo")
        if pct >= desc_min:
            motivos.append("descuento")
        if anterior and 100 * (1 - actual / anterior) >= aj.get("baja_minima_pct", 10):
            motivos.append("baja")
        log(f"    {h['titulo']}: {usd(actual)} {mejor_pais}" +
            (f" (normal {usd(regular)})" if actual < regular - 0.005 else ""))
        silencio = anterior is None and (not juego.get("auto") or not state.get("descubrimiento_listo"))
        if decidir(s, actual, motivos, nuevo=silencio) and not silenciado:
            linea = f'<a href="{esc_attr(h["url"])}">{esc(h["titulo"])}</a>\n   💸 {usd(actual)}'
            if clp_x_usd:
                linea += f" (≈{clp(actual * clp_x_usd)})"
            if pct >= 1:
                linea += f"  <s>{usd(mejor['regular_usd'])}</s>  −{round(pct)}% oferta"
            linea += f"\n   {NOMBRE_PAIS.get(mejor_pais, mejor_pais)}"
            if mejor_pais != pais and ahorro_region >= 1:
                linea += f" · {round(ahorro_region)}% menos que {pais} ({usd(base['actual_usd'])})"
            if p["fin"]:
                linea += f"  ⏳ hasta {esc(p['fin'][:10])}"
            if "minimo" in motivos:
                linea += "\n   🏆 mínimo histórico"
            if "objetivo" in motivos:
                linea += "  🎯"
            agregar_alerta(alertas, "digital", juego["nombre"], linea,
                           puntaje(pct, motivos), ref=nsuid,
                           extra=extra_motivos(motivos, anterior, actual, usd))


def _leer_una_tienda(tienda, juegos, state):
    cache = {}
    hallados = {}  # url -> (juego, producto)
    respondio = False
    vistos_terminos = set()
    for juego in juegos:
        if juego.get("fisico") is False:
            continue
        for t in terminos(juego):
            nt = norm(t)
            if nt in vistos_terminos:
                continue
            vistos_terminos.add(nt)
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
    # un mismo producto puede calzar con varias busquedas (ej. "mario" y "mario kart"):
    # se asigna al juego cuya busqueda es mas especifica
    for url, (j, p) in list(hallados.items()):
        mejor = max((jj for jj in juegos if jj.get("fisico") is not False
                     and coincide(p["titulo"], jj, True)),
                    key=lambda jj: max(len(palabras(t)) for t in terminos(jj)), default=j)
        hallados[url] = (mejor, p)
    # tiendas Shopify: ya se bajo el catalogo completo, se aprovechan todos los juegos de Switch
    if state.get("_descubrir", True):
        for p in cache.get("shopify") or []:
            if p["url"] not in hallados and precio_valido(p.get("precio")) and es_juego_switch(p["titulo"]):
                hallados[p["url"]] = (JUEGO_OTROS, p)
    # memorias microSD (van aparte de los juegos: tienen su propio filtro)
    terminos_mem = state.get("_memorias") or []
    if terminos_mem and respondio:
        mem = {}
        fuentes = [cache.get("shopify")] if cache.get("metodo") == "shopify" else []
        if not fuentes:
            for t in terminos_mem:
                prods = buscar_en_tienda(tienda, t, state, cache)
                fuentes.append(prods or [])
                time.sleep(PAUSA)
        for prods in fuentes:
            for p in prods or []:
                if precio_valido(p.get("precio")) and clasificar_memoria(p["titulo"], p["precio"]):
                    mem.setdefault(p["url"], p)
        cache["memorias"] = mem
    return respondio, hallados, cache


def es_juego_switch(titulo):
    w = palabras(titulo)
    return bool(w & PLATAFORMA) and not (w & EXCLUIR_SIEMPRE)


def _leer_seguro(tienda, juegos, state):
    """Si una tienda revienta, se cuenta como fallo y las demas siguen."""
    try:
        return _leer_una_tienda(tienda, juegos, state)
    except Exception as e:  # noqa: BLE001
        return False, {}, {"diag": [f"error inesperado: {type(e).__name__}: {str(e)[:150]}"]}


def leer_tiendas(cfg, state, alertas):
    """Busca en todas las tiendas a la vez. Devuelve [(juego, tienda, listado)]."""
    from concurrent.futures import ThreadPoolExecutor
    fallos = state.setdefault("fallos", {})
    juegos = cfg.get("juegos", [])
    listados = []
    tiendas = [t for t in cfg.get("tiendas", []) if t.get("activa") is not False]
    state["_descubrir"] = cfg.get("ajustes", {}).get("descubrir", True)
    mem_cfg = cfg.get("memorias") or {}
    state["_memorias"] = mem_cfg.get("buscar", MEMORIAS_BUSCAR) if mem_cfg.get("activa", True) else []
    MEMORIAS_LISTADOS.clear()
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, len(tiendas))) as ex:
        resultados = list(ex.map(lambda t: (t, *_leer_seguro(t, juegos, state)), tiendas))
    state.pop("_descubrir", None)
    state.pop("_memorias", None)
    for tienda, respondio, _, cache in resultados:
        for p in (cache.get("memorias") or {}).values():
            p["externo"] = es_externo(p, tienda)
            MEMORIAS_LISTADOS.append((tienda["nombre"], p))
    log(f"  tiendas leidas en {time.time() - t0:.0f} s")
    for tienda, respondio, hallados, cache in resultados:
        nombre_t = tienda["nombre"]
        if not respondio:
            fallos[nombre_t] = fallos.get(nombre_t, 0) + 1
            log(f"  {nombre_t}: sin respuesta util (fallo {fallos[nombre_t]} seguido)")
            for d in cache.get("diag", [])[-4:]:
                log(f"      {d}")
            if fallos[nombre_t] == FALLOS_PARA_AVISAR:
                agregar_alerta(alertas, "aviso", "", (
                    f"No he podido leer <b>{esc(nombre_t)}</b> en las últimas "
                    f"{FALLOS_PARA_AVISAR} revisiones. Puede que bloquee al bot o que haya "
                    f"cambiado su página."), 0)
            continue
        fallos.pop(nombre_t, None)
        log(f"  {nombre_t} [{cache.get('metodo')}]: {len(hallados)} avisos que calzan")
        for _, p in hallados.values():
            p["externo"] = es_externo(p, tienda)
        if cfg.get("ajustes", {}).get("excluir_externos", False):
            n = sum(1 for _, p in hallados.values() if p["externo"])
            if n:
                log(f"    {n} de vendedores externos descartados")
            hallados = {u: (j, p) for u, (j, p) in hallados.items() if not p["externo"]}
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
    todos_cands = [c for lst in cands.values() for c in lst]
    eshop = state.get("eshop", {})
    hist_p = hist.setdefault("productos", {})
    productos = state.setdefault("productos", {})

    grupos = {}
    for juego, tienda, l in listados:
        lista = todos_cands if juego.get("auto") else cands.get(juego["nombre"], [])
        clave, nombre, plat, variante, nsuid = clave_producto(l, lista)
        g = grupos.setdefault(clave, {"juego": juego, "nombre": nombre, "plataforma": plat,
                                      "variante": variante, "nsuid": nsuid, "avisos": []})
        g["avisos"].append({"tienda": tienda, "precio": l["precio"], "url": l["url"],
                            "titulo": l["titulo"], "normal": l.get("normal"),
                            "externo": bool(l.get("externo")), "vendedor": l.get("vendedor")})
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
        registrar_detalle(h, hoy, avisos)

        # precio de referencia: tipico propio > otras tiendas > lo que dice la tienda
        # precios fuera de rango (otra edicion mezclada, error de la tienda) no cuentan
        med_todos = _mediana([a["precio"] for a in avisos])
        otras = [a["precio"] for a in avisos[1:] if a["precio"] <= 2 * med_todos]
        if est.get("tipico"):
            ref, ref_tipo = est["tipico"], "tipico"
        elif otras:
            ref, ref_tipo = _mediana(otras), "mercado"
        elif mejor.get("normal") and mejor["normal"] > precio:
            ref, ref_tipo = mejor["normal"], "tienda"
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
            "avisos": [{k: a.get(k) for k in ("tienda", "precio", "url", "titulo", "externo", "vendedor")}
                       for a in avisos],
            "externo": bool(mejor.get("externo")), "vendedor": mejor.get("vendedor"),
            "normal_tienda": mejor.get("normal"), "objetivo": juego.get("objetivo_clp"),
            "nsuid": g["nsuid"], "ultimo": precio, "visto": ahora_iso(),
        })

        id_ = id_de("f", clave)
        obj_seg, seguido, silenciado = seguimiento(id_)
        objetivo = obj_seg if obj_seg is not None else juego.get("objetivo_clp")
        s.update({"id": id_, "objetivo": objetivo, "seguido": seguido, "silenciado": silenciado})
        motivos = []
        if seguido and anterior and precio < anterior:
            motivos.append("seguido")
        if objetivo is not None and precio <= objetivo:
            motivos.append("objetivo")
        if est["dias"] >= MIN_DIAS and est.get("min") and precio < est["min"] * 0.99:
            motivos.append("minimo")
        if ref and pct >= desc_min:
            motivos.append("descuento")
        if anterior and 100 * (1 - precio / anterior) >= baja_min:
            motivos.append("baja")

        silencio = anterior is None and (not juego.get("auto") or not state.get("descubrimiento_listo"))
        if decidir(s, precio, motivos, nuevo=silencio) and not silenciado:
            agregar_alerta(alertas, "fisico", juego["nombre"],
                           linea_producto(s, motivos, anterior, e),
                           puntaje(s.get("pct") or 0, motivos), ref=clave,
                           extra=extra_motivos(motivos, anterior, precio, clp))


# --------------------------------------------------------------- memorias

MEMORIAS_BUSCAR = ["microsd express", "microsd", "micro sd"]
MEMORIAS_LISTADOS = []      # (tienda, producto) de esta corrida, los llena leer_tiendas
MEM_RE = re.compile(r"\bmicro ?sd(hc|xc)?\b")
CAP_RE = re.compile(r"(?<![\d.,])(\d+(?:[.,]\d)?)\s?(gb|tb)\b")
CAPACIDADES_GB = {32, 64, 128, 256, 512, 1024, 1536, 2048}
MARCAS_MEM = ["sandisk", "samsung", "kingston", "lexar", "pny", "sony", "adata", "kioxia",
              "toshiba", "transcend", "netac", "hikvision", "teamgroup", "silicon power",
              "verbatim", "western digital", "crucial"]
EXCLUIR_MEM = {"lector", "reader", "usb", "pendrive", "hub", "estuche", "funda", "case", "porta",
               "combo", "pack", "kit", "x2", "x3", "x4", "2x", "3x", "unidades", "consola", "bundle",
               "dock", "ssd", "disco", "camara", "dashcam", "celular", "telefono", "tablet",
               "reacondicionado", "usado", "usada", "seminuevo", "gopro", "dron"}
NOMBRE_MARCA = {"sandisk": "SanDisk", "pny": "PNY", "adata": "ADATA", "kioxia": "KIOXIA",
                "teamgroup": "TeamGroup"}
# por debajo de esto por GB casi siempre es una tarjeta falsa (capacidad trucada)
MIN_CLP_POR_GB = {"normal": 25, "express": 80}


def clasificar_memoria(titulo, precio=None):
    """'Tarjeta microSD Express SanDisk 256GB para Switch 2' ->
    {'tipo': 'express', 'gb': 256, 'marca': 'SanDisk'}. None si no es una microSD suelta."""
    t = norm(titulo)
    if not MEM_RE.search(t) or set(t.split()) & EXCLUIR_MEM:
        return None
    caps = set()
    for num, unidad in CAP_RE.findall(str(titulo).lower()):
        try:
            n = float(num.replace(",", "."))
        except ValueError:
            continue
        caps.add(int(round(n * 1024)) if unidad == "tb" else int(n))
    caps &= CAPACIDADES_GB
    if len(caps) != 1:
        return None                     # sin capacidad o con varias (aviso de variantes)
    gb = caps.pop()
    tipo = "express" if "express" in t.split() else "normal"
    if precio and precio / gb < MIN_CLP_POR_GB[tipo]:
        return None
    marca = next((m for m in MARCAS_MEM if contiene_palabras(t, m)), None)
    return {"tipo": tipo, "gb": gb, "marca": NOMBRE_MARCA.get(marca, marca.title()) if marca else None}


def contiene_palabras(texto_norm, frase):
    return re.search(r"(^| )" + re.escape(frase) + r"( |$)", texto_norm) is not None


def nombre_memoria(m):
    cap = f"{m['gb'] // 1024} TB" if m["gb"] >= 1024 and m["gb"] % 1024 == 0 else \
        f"{m['gb'] / 1024:.1f} TB" if m["gb"] >= 1024 else f"{m['gb']} GB"
    return f"microSD{' Express' if m['tipo'] == 'express' else ''} {cap}" + \
        (f" · {m['marca']}" if m.get("marca") else "")


def revisar_memorias(cfg, state, hist, alertas):
    """Agrupa las microSD por tipo, capacidad y marca, guarda el historial y avisa
    de minimos historicos, descuentos reales y bajas (igual que con los juegos)."""
    mem_cfg = cfg.get("memorias") or {}
    if mem_cfg.get("activa", True) is False:
        return
    aj = cfg.get("ajustes", {})
    desc_min = mem_cfg.get("descuento_minimo_pct", aj.get("descuento_minimo_pct", 25))
    baja_min = aj.get("baja_minima_pct", 10)
    excluir_ext = mem_cfg.get("excluir_externos", True)
    hoy = hoy_chile()
    grupos = {}
    for tienda, p in MEMORIAS_LISTADOS:
        if excluir_ext and p.get("externo"):
            continue
        m = clasificar_memoria(p["titulo"], p["precio"])
        if not m:
            continue
        clave = f"{m['tipo']}|{m['gb']}|{norm(m['marca'] or 'otra')}"
        g = grupos.setdefault(clave, {"m": m, "por_tienda": {}})
        prev = g["por_tienda"].get(tienda)
        if not prev or p["precio"] < prev["precio"]:
            g["por_tienda"][tienda] = {"tienda": tienda, "precio": p["precio"], "url": p["url"],
                                       "titulo": p["titulo"], "normal": p.get("normal")}
    log(f"  memorias: {len(MEMORIAS_LISTADOS)} avisos, {len(grupos)} modelos")
    items = state.setdefault("memorias", {})
    hist_m = hist.setdefault("memorias", {})
    for clave, g in sorted(grupos.items()):
        m = g["m"]
        avisos = sorted(g["por_tienda"].values(), key=lambda a: a["precio"])
        mejor = avisos[0]
        precio = mejor["precio"]
        serie = hist_m.setdefault(clave, {})
        est = estadisticas(serie, hoy)
        registrar(serie, hoy, precio, mejor["tienda"])
        otras = [a["precio"] for a in avisos[1:] if a["precio"] <= 2 * _mediana([x["precio"] for x in avisos])]
        if est.get("tipico"):
            ref, ref_tipo = est["tipico"], "tipico"
        elif otras:
            ref, ref_tipo = _mediana(otras), "mercado"
        else:
            ref, ref_tipo = None, None
        pct = 100 * (1 - precio / ref) if ref else 0
        s = items.setdefault(clave, {})
        anterior = s.get("ultimo")
        s.update({"nombre": nombre_memoria(m), "tipo": m["tipo"], "gb": m["gb"], "marca": m["marca"],
                  "consola": "Switch 2" if m["tipo"] == "express" else "Switch 1",
                  "precio": precio, "tienda": mejor["tienda"], "url": mejor["url"],
                  "por_gb": round(precio / m["gb"]), "ref": ref, "ref_tipo": ref_tipo,
                  "pct": round(pct, 1), "min_hist": est.get("min"), "min_fecha": est.get("min_fecha"),
                  "tipico": est.get("tipico"), "dias": est["dias"], "ultimo": precio, "visto": ahora_iso(),
                  "avisos": [{k: a[k] for k in ("tienda", "precio", "url", "titulo")} for a in avisos]})
        if not m["marca"]:
            continue                    # sin marca conocida: se registra pero no se avisa
        motivos = []
        if est["dias"] >= MIN_DIAS and est.get("min") and precio < est["min"] * 0.99:
            motivos.append("minimo")
        if ref and pct >= desc_min:
            motivos.append("descuento")
        if anterior and 100 * (1 - precio / anterior) >= baja_min:
            motivos.append("baja")
        if decidir(s, precio, motivos, nuevo=anterior is None):
            agregar_alerta(alertas, "memoria", "💾 Memorias", linea_memoria(s, motivos, anterior),
                           puntaje(pct, motivos), extra="compra" if "minimo" in motivos or pct >= 30 else "bueno")


def linea_memoria(s, motivos, anterior):
    l = [f"💾 <b>{esc(s['nombre'])}</b> · para {s['consola']}",
         f"💰 <b>{clp(s['precio'])}</b> en {esc(s['tienda'])} ({clp(s['por_gb'])} por GB)"
         + (f" · −{round(s['pct'])}%" if s["pct"] >= 5 else "")]
    if "minimo" in motivos:
        l.append("🏆 Es el precio más bajo que he visto")
    elif "baja" in motivos and anterior:
        l.append(f"📉 Bajó desde {clp(anterior)}")
    if s.get("ref") and s["pct"] >= 5:
        contra = "su precio típico" if s["ref_tipo"] == "tipico" else "las otras tiendas"
        l.append(f"   Ahorras {clp(s['ref'] - s['precio'])} frente a {contra} ({clp(s['ref'])})")
    med = ["🥈", "🥉"]
    for i, a in enumerate(s["avisos"][1:3]):
        l.append(f"   {med[i]} <a href=\"{esc_attr(a['url'])}\">{clp(a['precio'])} {esc(a['tienda'])}</a>")
    if s["tipo"] == "normal":
        l.append("⚠️ No sirve para juegos de Switch 2 (esa consola solo usa microSD Express)")
    l.append(f"🛒 <a href=\"{esc_attr(s['url'])}\">Ir a comprar en {esc(s['tienda'])}</a>")
    return "\n".join(l)


def linea_producto(s, motivos, anterior, e):
    nombre = s["nombre"] + (f" ({' '.join(s['variante'])})" if s.get("variante") else "")
    l = (f'<a href="{esc_attr(s["url"])}">{esc(nombre)}</a> · {esc(s["plataforma"])}\n'
         f'   💸 {clp(s["precio"])} en {esc(s["tienda"])}'
         + (f' 🏪 {esc(s.get("vendedor") or "vendedor externo")}' if s.get("externo") else ""))
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
        if e.get("clp"):
            dif = e["clp"] - s["precio"]
            cual = "conviene el físico" if dif > 0 else "conviene el digital"
            detalles.append(f"🎮 {usd(e['ultimo'])} {e.get('pais', '')} ≈{clp(e['clp'])} → {cual}")
        else:
            detalles.append(f"🎮 {usd(e['ultimo'])}")
    if detalles:
        l += "\n   📊 " + " · ".join(detalles)
    return l


# ------------------------------------------------------------------ telegram

PAUSA_TELEGRAM = 1.5        # segundos entre mensajes: Telegram bloquea al bot si manda rafagas
ESPERA_MAX_429 = 60         # si Telegram pide esperar mas que esto, se deja para la proxima corrida
_ULTIMO_ENVIO = [0.0]
BLOQUEO = {}                # {"hasta": iso} cuando Telegram bloqueo al bot por exceso de mensajes


def send_telegram(text, teclado=None):
    if isinstance(text, tuple):
        text, teclado = text
    if os.environ.get("DRY_RUN"):
        log("[DRY_RUN]\n" + re.sub(r"<[^>]+>", "", text) +
            ("".join(f"\n   [{f[0]['text']}]" for f in teclado["inline_keyboard"]) if teclado else ""))
        return True
    if BLOQUEO:
        return False
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log("ERROR: faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")
        return False
    for intento in (1, 2):
        espera = PAUSA_TELEGRAM - (time.time() - _ULTIMO_ENVIO[0])
        if espera > 0:
            time.sleep(espera)
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True,
                      **({"reply_markup": teclado} if teclado else {})},
                timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            log(f"ERROR Telegram: {e}")
            return False
        _ULTIMO_ENVIO[0] = time.time()
        if r.status_code == 429:
            try:
                seg = int(r.json().get("parameters", {}).get("retry_after") or 0)
            except ValueError:
                seg = 0
            if intento == 1 and 0 < seg <= ESPERA_MAX_429:
                log(f"  Telegram pide esperar {seg} s; espero y reintento")
                time.sleep(seg + 1)
                continue
            hasta = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seg))
            BLOQUEO["hasta"] = hasta
            log(f"ERROR Telegram 429: bloqueó al bot por {seg // 3600} h {seg % 3600 // 60} min "
                f"(hasta {hasta}) por mandar demasiados mensajes. No mando nada más hasta entonces.")
            return False
        if r.status_code != 200:
            log(f"ERROR Telegram {r.status_code}: {r.text[:200]}")
            return False
        return True
    return False


def recortar_mensajes(mensajes, maximo):
    """Deja los primeros mensajes (las mejores ofertas van primero) y cambia el resto por un
    aviso corto: decenas de mensajes seguidos hacen que Telegram bloquee al bot por horas."""
    if len(mensajes) <= maximo:
        return mensajes
    resto = len(mensajes) - (maximo - 1)
    aviso = (f"📦 <b>Y hay más</b>\n{LINEA}\nNo te mando {resto} mensajes más para que Telegram no bloquee "
             f"al bot.\nVer todo lo vigente: /ofertas · Por saga: /precios")
    return mensajes[:maximo - 1] + [aviso]


MAX_ALERTAS = 30          # mas que esto va resumido a /ofertas
LINEA = "━━━━━━━━━━━━━━"


def puntaje(pct, motivos):
    return ((pct or 0) + (100 if "minimo" in motivos else 0) + (80 if "objetivo" in motivos else 0)
            + (60 if "seguido" in motivos else 0))


def agregar_alerta(alertas, tipo, juego, linea, score, ref=None, extra=None):
    alertas.append({"tipo": tipo, "juego": juego, "linea": linea, "score": score,
                    "ref": ref, "extra": extra})


def filtrar_alertas(alertas, state, cfg):
    """Solo lo contundente, con el mismo criterio del canal (canal.json):
    - juegos de tu lista: siempre, salvo que sean relleno que solo comparte una palabra
      con la saga ("Hades Uprising");
    - juegos descubiertos: solo los destacados;
    - lo que sigues o llego a tu objetivo, memorias y avisos del bot: siempre."""
    if cfg.get("ajustes", {}).get("solo_contundentes", True) is False:
        return alertas
    try:
        import canal
        ccfg = load_json(canal.CANAL_FILE, None)
        if not ccfg:
            return alertas
        cands = {o["id"]: o for o in canal.candidatos(ccfg, state, incluir_excluidos=True)}
    except Exception as e:  # noqa: BLE001 - si el filtro falla, mejor avisar todo
        log(f"  filtro de alertas: error {type(e).__name__}: {e}; mando todo")
        return alertas
    out, fuera = [], 0
    for a in alertas:
        grupo = {"digital": "eshop", "fisico": "productos"}.get(a["tipo"])
        s = (state.get(grupo) or {}).get(a.get("ref")) if grupo else None
        if not s:
            out.append(a)
            continue
        precio = s.get("ultimo") if a["tipo"] == "digital" else s.get("precio")
        if s.get("seguido") or (s.get("objetivo") is not None and precio is not None
                                and precio <= s["objetivo"]):
            out.append(a)
            continue
        o = cands.get(s.get("id"))
        de_tu_lista = norm(re.sub(r"^\W+", "", a.get("juego") or "")) != "otros juegos"
        if o is None:
            ok = de_tu_lista              # no se pudo evaluar: se respeta tu lista
        elif o.get("excluido"):
            ok = False
        elif de_tu_lista:
            ok = bool(o.get("saga")) or o.get("nivel") != "resto"
        else:
            ok = o.get("nivel") in ("destacado", "bombazo")
        if ok:
            out.append(a)
        else:
            fuera += 1
    if fuera:
        log(f"  filtro de alertas: {fuera} de relleno o poco relevantes no se avisan (siguen en /ofertas)")
    return out


def extra_motivos(motivos, anterior=None, precio=None, dinero=None):
    """Etiqueta corta de por que llega esta alerta."""
    if "objetivo" in motivos:
        return None
    if "minimo" in motivos:
        return "🏆 mínimo histórico"
    if "seguido" in motivos and anterior and precio is not None:
        return f"⭐ bajó desde {dinero(anterior)}"
    if "baja" in motivos and anterior and precio is not None:
        return f"📉 bajó desde {dinero(anterior)}"
    return None


def fecha_hora_chile():
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        d = datetime.now(ZoneInfo("America/Santiago"))
    except Exception:
        from datetime import datetime, timedelta
        d = datetime.utcnow() - timedelta(hours=3)
    meses = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
    return f"{d.day} {meses[d.month - 1]} · {d:%H:%M}"


def _con_juego(a):
    return f"<i>{esc(a['juego'])}</i> · {a['linea']}" if a["juego"] else a["linea"]


# ------------------------------------------------------------------ tarjetas
#
# Cada producto se muestra como una "tarjeta de decision": veredicto arriba,
# precio, por que es (o no) buen precio, comparacion entre tiendas, historial,
# digital vs fisico y el link para comprar. Las mismas tarjetas se usan en las
# alertas y en los comandos (/ofertas, /precios), que las leen ya armadas
# desde la carpeta vistas/.

VISTAS_DIR = BASE / "vistas"
SEP_TARJETA = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
PAISES = {
    "US": ("🇺🇸", "EE. UU.", "en-us"), "CA": ("🇨🇦", "Canadá", "en-ca"),
    "MX": ("🇲🇽", "México", "es-mx"), "BR": ("🇧🇷", "Brasil", "pt-br"),
    "CL": ("🇨🇱", "Chile", "es-cl"), "CO": ("🇨🇴", "Colombia", "es-co"),
    "PE": ("🇵🇪", "Perú", "es-pe"), "AR": ("🇦🇷", "Argentina", "es-ar"),
}
SIMBOLO = {"CLP": "$", "USD": "US$", "CAD": "CA$", "MXN": "MX$", "BRL": "R$", "COP": "COL$",
           "PEN": "S/", "ARS": "AR$"}
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
BARRAS = "▁▂▃▄▅▆▇█"
NIVELES = {  # orden, emoji, titulo
    "objetivo": (0, "🎯", "LLEGÓ A TU OBJETIVO"),
    "compra": (1, "🟢", "COMPRA YA"),
    "bueno": (2, "🟡", "BUEN PRECIO"),
    "regular": (3, "🟠", "PRECIO REGULAR"),
    "normal": (4, "⚪", "PRECIO NORMAL"),
}


def fecha_corta(ymd):
    try:
        y, m, d = (int(x) for x in str(ymd)[:10].split("-"))
        return f"{d} {MESES[m - 1]}"
    except (ValueError, IndexError):
        return str(ymd)


def dias_para(fin_iso):
    try:
        from datetime import datetime, timezone
        fin = datetime.fromisoformat(str(fin_iso).replace("Z", "+00:00"))
        return (fin - datetime.now(timezone.utc)).total_seconds() / 86400
    except (ValueError, TypeError):
        return None


def sparkline(vals):
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    return "".join(BARRAS[3 if hi == lo else round((v - lo) / (hi - lo) * 7)] for v in vals)


def slug(texto):
    return norm(texto).replace(" ", "-") or "x"


def pais_txt(cod):
    f, n, _ = PAISES.get(cod, ("🌎", cod, ""))
    return f"{f} {n}"


def url_pais(url, cod):
    loc = PAISES.get(cod, (0, 0, ""))[2]
    if not url or not loc or cod == "US":
        return url
    return re.sub(r"nintendo\.com/(us|[a-z]{2}-[a-z]{2})/", f"nintendo.com/{loc}/", url)


def dinero_local(valor, moneda):
    if valor is None:
        return ""
    sim = SIMBOLO.get(moneda, moneda + " ")
    if moneda in ("CLP", "COP", "ARS"):
        return sim + f"{int(round(valor)):,}".replace(",", ".") + f" {moneda}"
    num = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{sim}{num} {moneda}"


def plat_txt(p):
    return "Switch 2" if p == "Switch 2" else "Switch 1"


def serie_de(hist, grupo, clave):
    h = (hist.get(grupo) or {}).get(clave) or {}
    f = h.get("f", h) if grupo == "productos" else h
    fechas = sorted(k for k in f if re.match(r"\d{4}-\d{2}-\d{2}$", k))
    return [(d, f[d][0] if isinstance(f[d], list) else f[d],
             f[d][1] if isinstance(f[d], list) else None) for d in fechas]


# ---------------------------------------------------------------- veredictos

def veredicto_fisico(s):
    pct = s.get("pct") or 0
    fiable = s.get("ref_tipo") in ("tipico", "mercado")
    minimo = (s.get("dias") or 0) >= MIN_DIAS and s.get("min_hist") and s["precio"] <= s["min_hist"]
    if s.get("objetivo") and s["precio"] <= s["objetivo"]:
        return "objetivo", f"llegó a tu objetivo de {clp(s['objetivo'])}"
    if minimo:
        return "compra", "es el precio más bajo que he visto"
    n_otras = len(s.get("avisos") or []) - 1
    contra = ("su precio típico" if s.get("ref_tipo") == "tipico"
              else "la otra tienda" if n_otras == 1 else "el resto de las tiendas")
    # con una sola tienda para comparar no se asegura "compra ya": esa otra puede estar cara
    if fiable and pct >= 30 and (s.get("ref_tipo") == "tipico" or n_otras >= 2):
        return "compra", f"{round(pct)}% bajo {contra}"
    if fiable and pct >= 15:
        extra = " (solo comparé con una tienda: revisa que no esté cara)" if n_otras == 1 and s.get("ref_tipo") != "tipico" else ""
        return "bueno", f"{round(pct)}% bajo {contra}{extra}"
    if s.get("ref_tipo") == "tienda" and pct >= 25:
        return "bueno", f"−{round(pct)}% según la tienda (sin otra tienda para confirmarlo)"
    if pct >= 5:
        return "regular", f"apenas {round(pct)}% bajo la referencia"
    return "normal", "está a su precio de siempre"


def veredicto_digital(e):
    pct = e.get("pct_oferta") or 0
    minimo = (e.get("dias") or 0) >= MIN_DIAS and e.get("min_hist") and e["ultimo"] <= e["min_hist"]
    if e.get("objetivo") and e["ultimo"] <= e["objetivo"]:
        return "objetivo", f"llegó a tu objetivo de {usd(e['objetivo'])}"
    if minimo and pct:
        return "compra", "es el precio más bajo que he visto"
    if pct >= 40:
        return "compra", f"oferta de −{round(pct)}% en la eShop"
    if pct >= 20:
        return "bueno", f"oferta de −{round(pct)}% en la eShop"
    if pct >= 5:
        return "regular", f"oferta chica de −{round(pct)}%"
    return "normal", "sin oferta, precio regular"


def confianza(s):
    n = len(s.get("avisos") or [])
    d = (s.get("dias") or 0) + 1          # dias con datos, contando hoy
    if d >= MIN_DIAS and n >= 3:
        return "alta", f"{n} tiendas y {d} días de historial"
    if d >= MIN_DIAS or n >= 2:
        return "media", f"{n} tienda{'s' if n != 1 else ''} y {d} día{'s' if d != 1 else ''} de historial"
    return "baja", f"una sola tienda y {d} día{'s' if d != 1 else ''} de historial"


# ---------------------------------------------------------------- tarjetas

def lineas_seguimiento(s, precio, dinero):
    l = []
    if s.get("objetivo"):
        falta = precio - s["objetivo"]
        l.append(f"🎯 Tu objetivo: {dinero(s['objetivo'])}" +
                 (" · ¡lo alcanzó!" if falta <= 0 else f" · faltan {dinero(falta)}"))
    marcas = []
    if s.get("seguido"):
        marcas.append("⭐ Lo sigues: te aviso de cualquier baja")
    if s.get("silenciado"):
        marcas.append("🔕 Silenciado" + ("" if s["silenciado"] == "siempre" else f" hasta el {fecha_corta(s['silenciado'])}"))
    l += marcas
    return l


def boton_de(s, tipo):
    nombre = s.get("nombre") or s.get("titulo") or "?"
    precio = clp(s["precio"]) if tipo == "fisico" else usd(s["ultimo"])
    corto = nombre if len(nombre) <= 30 else nombre[:29] + "…"
    s2 = " S2" if s.get("plataforma") == "Switch 2" else ""
    return {"text": f"⚙️ {corto}{s2} · {precio}", "callback_data": f"a:{s.get('id')}"}


def tarjeta_fisico(clave, s, state, hist, extra=None):
    nivel, razon = veredicto_fisico(s)
    _, emo, tit = NIVELES[nivel]
    pct = s.get("pct") or 0
    nombre = s["nombre"] + (f" ({' '.join(s['variante'])})" if s.get("variante") else "")
    l = [f"{emo} <b>{tit}</b>" + (f" · −{round(pct)}%" if pct >= 5 else "") + (f" · {extra}" if extra else ""),
         f"<b>{esc(nombre)}</b>",
         f"🕹️ {plat_txt(s['plataforma'])} · 📦 Físico" + (f" · {esc(s['juego'])}" if norm(s["juego"]) not in norm(nombre) else "")]
    vend = f" 🏪 <i>vende {esc(s.get('vendedor') or 'tercero')}</i>" if s.get("externo") else ""
    l.append(f"💰 <b>{clp(s['precio'])}</b> en {esc(s['tienda'])}{vend}")
    l.append(f"💡 {razon[0].upper() + razon[1:]}")
    if s.get("ref") and pct >= 1:
        ahorro = s["ref"] - s["precio"]
        txt = {"tipico": "a su precio típico",
               "mercado": "a la otra tienda" if len(s.get("avisos") or []) == 2 else "al promedio de las otras tiendas",
               "tienda": "al «antes» que marca la tienda"}.get(s.get("ref_tipo"), "a la referencia")
        l.append(f"   Ahorras {clp(ahorro)} frente {txt} ({clp(s['ref'])})")
    # "antes" de la tienda inflado
    otros = [a["precio"] for a in (s.get("avisos") or [])[1:]]
    nt = s.get("normal_tienda")
    if nt and nt > s["precio"]:
        if otros and nt > 1.25 * _mediana(otros):
            l.append(f"🏷️ La tienda dice «antes {clp(nt)}», pero las demás lo venden cerca de {clp(_mediana(otros))}: ese descuento está inflado")
        elif s.get("ref_tipo") != "tienda":
            l.append(f"🏷️ La tienda marca «antes {clp(nt)}» (−{round(100 * (1 - s['precio'] / nt))}%)")
    # comparacion
    avisos = s.get("avisos") or []
    if len(avisos) > 1:
        l.append(f"🏬 <b>Comparación ({len(avisos)} tiendas)</b>")
        med = ["🥇", "🥈", "🥉"]
        for i, a in enumerate(avisos[:8]):
            m = med[i] if i < 3 else "▫️"
            ext = f" 🏪 {esc(a.get('vendedor') or '')}".rstrip() if a.get("externo") else ""
            dif = f" (+{clp(a['precio'] - s['precio'])})" if i else ""
            if i and a["precio"] > 2 * _mediana([x["precio"] for x in avisos]):
                dif += " ⚠️ ¿otra edición?"
            l.append(f"   {m} <a href=\"{esc_attr(a['url'])}\">{clp(a['precio'])} {esc(a['tienda'])}</a>{ext}{dif}")
        if len(avisos) > 8:
            l.append(f"   … y {len(avisos) - 8} más")
    else:
        l.append("🏬 Solo lo encontré en esta tienda")
    # historial
    serie = serie_de(hist, "productos", clave)
    if serie:
        vals = [v for _, v, _ in serie]
        dmin = min(serie, key=lambda x: x[1])
        txt = f"📈 Mínimo {clp(dmin[1])} ({fecha_corta(dmin[0])}{', ' + esc(dmin[2]) if dmin[2] else ''})"
        if len(vals) >= MIN_DIAS:
            txt += f" · típico {clp(_mediana(vals[-DIAS_TIPICO:]))}"
        txt += f" · máximo {clp(max(vals))}"
        l.append(txt)
        if len(vals) >= 2:
            l.append(f"   {sparkline(vals[-30:])}  últimos {min(30, len(vals))} días")
        else:
            l.append("   Historial recién empezando: en 7 días sabré su precio típico")
    # digital
    e = (state.get("eshop") or {}).get(s.get("nsuid")) if s.get("nsuid") else None
    if e and e.get("ultimo") is not None:
        dig = f"🎮 Digital: {usd(e['ultimo'])}"
        if e.get("clp"):
            dig += f" ≈ {clp(e['clp'])}"
        dig += f" en {pais_txt(e.get('pais', 'US'))}"
        if e.get("pct_oferta"):
            dig += f" (−{round(e['pct_oferta'])}%)"
        l.append(dig)
        if e.get("clp"):
            dif = e["clp"] - s["precio"]
            if dif > 0:
                l.append(f"   ➜ El físico te ahorra {clp(dif)} y además se puede revender")
            else:
                l.append(f"   ➜ El digital es {clp(-dif)} más barato, pero no se puede revender")
    nivel_c, por = confianza(s)
    l.append(f"🔎 Confianza {nivel_c}: {por}")
    l += lineas_seguimiento(s, s["precio"], clp)
    l.append(f"🛒 <a href=\"{esc_attr(s['url'])}\">Ir a comprar en {esc(s['tienda'])}</a>")
    return nivel, "\n".join(l)


def tarjeta_digital(nsuid, e, state, hist, extra=None):
    nivel, razon = veredicto_digital(e)
    _, emo, tit = NIVELES[nivel]
    pct = e.get("pct_oferta") or 0
    l = [f"{emo} <b>{tit}</b>" + (f" · −{round(pct)}%" if pct >= 5 else "") + (f" · {extra}" if extra else ""),
         f"<b>{esc(e['titulo'])}</b>",
         f"🕹️ {plat_txt(e.get('plataforma'))} · 🎮 Digital (eShop)" +
         (f" · {esc(e['juego'])}" if norm(e["juego"]) not in norm(e["titulo"]) else "")]
    pais = e.get("pais", "US")
    precio = f"💰 <b>{usd(e['ultimo'])}</b>"
    if e.get("clp"):
        precio += f" ≈ {clp(e['clp'])}"
    precio += f" en {pais_txt(pais)}"
    l.append(precio)
    if e.get("moneda_local") and e.get("moneda_local") != "USD":
        l.append(f"   Precio en la tienda: {dinero_local(e.get('local'), e['moneda_local'])}")
    l.append(f"💡 {razon[0].upper() + razon[1:]}")
    if pct >= 1 and e.get("normal"):
        fin = ""
        if e.get("fin"):
            d = dias_para(e["fin"])
            fin = f" · ⏳ termina el {fecha_corta(e['fin'])}"
            if d is not None and d < 1:
                fin += " (¡hoy!)"
            elif d is not None:
                fin += f" (en {int(d) + 1} días)" if d >= 1 else ""
        l.append(f"🏷️ Normal {usd(e['normal'])} → ahorras {usd(e['normal'] - e['ultimo'])}{fin}")
    regs = e.get("regiones") or {}
    if len(regs) > 1:
        orden = sorted(regs.items(), key=lambda x: x[1])
        l.append("🌎 <b>Por país</b>")
        for cod, v in orden[:5]:
            l.append(f"   {PAISES.get(cod, ('🌎',))[0]} <a href=\"{esc_attr(url_pais(e.get('url'), cod))}\">{usd(v)}</a>"
                     + (" ← el más barato" if cod == orden[0][0] else ""))
        caro = orden[-1]
        if len(orden) > 5 and caro[1] > orden[0][1] * 1.05:
            l.append(f"   El más caro: {PAISES.get(caro[0], ('🌎',))[0]} {usd(caro[1])}")
    # fisico equivalente
    fis = [s for s in (state.get("productos") or {}).values() if s.get("nsuid") == nsuid]
    if fis:
        f = min(fis, key=lambda s: s["precio"])
        txt = f"📦 Físico: {clp(f['precio'])} en {esc(f['tienda'])}"
        if e.get("clp"):
            dif = f["precio"] - e["clp"]
            txt += (f" → el digital te ahorra {clp(dif)}" if dif > 0
                    else f" → el físico es {clp(-dif)} más barato y se puede revender")
        l.append(txt)
    serie = serie_de(hist, "eshop", nsuid)
    if serie:
        dmin = min(serie, key=lambda x: x[1])
        vals = [v for _, v, _ in serie]
        txt = f"📈 Mínimo {usd(dmin[1])} ({fecha_corta(dmin[0])}{' en ' + pais_txt(dmin[2]) if dmin[2] else ''})"
        if len(vals) >= 2:
            txt += f"\n   {sparkline(vals[-30:])}  últimos {min(30, len(vals))} días"
        l.append(txt)
    l += lineas_seguimiento(e, e["ultimo"], usd)
    l.append(f"🛒 <a href=\"{esc_attr(url_pais(e.get('url'), pais))}\">Ver en la eShop de {PAISES.get(pais, ('', pais))[1]}</a>")
    return nivel, "\n".join(l)


# ---------------------------------------------------------------- mensajes

def cabecera(titulo, sub):
    return f"{titulo}\n<i>{sub}</i>\n{LINEA}"


def empaquetar(bloques, titulo):
    """Junta bloques en mensajes de Telegram sin partir ninguno.
    Un bloque puede ser texto o (texto, boton): los botones de las tarjetas de
    cada mensaje van al pie de ese mensaje. Devuelve [(texto, teclado|None)]."""
    msgs, actual, botones = [], "", []
    for b in bloques:
        if not b:
            continue
        txt, bot_ = (b if isinstance(b, tuple) else (b, None))
        sep = "\n\n" if actual else ""
        if actual and len(actual) + len(sep) + len(txt) > TELEGRAM_MAX:
            msgs.append((actual, botones))
            actual, botones = txt, []
        else:
            actual += sep + txt
        if bot_:
            botones.append(bot_)
    if actual:
        msgs.append((actual, botones))
    n = len(msgs)
    out = []
    for i, (m, bts) in enumerate(msgs):
        if n > 1:
            m = m.replace(titulo, f"{titulo} <i>({i + 1}/{n})</i>", 1) if i == 0 \
                else f"{titulo} <i>({i + 1}/{n})</i>\n\n{m}"
        if bts:
            m += "\n\n<i>⚙️ Toca un juego para ponerle objetivo, seguirlo o silenciarlo</i>"
        out.append((m, {"inline_keyboard": [[x] for x in bts]} if bts else None))
    return out


def resumen_niveles(items):
    cuenta = {}
    for it in items:
        cuenta[it["nivel"]] = cuenta.get(it["nivel"], 0) + 1
    l = []
    for k in ("objetivo", "compra", "bueno", "regular", "normal"):
        if cuenta.get(k):
            _, emo, tit = NIVELES[k]
            l.append(f"{emo} {cuenta[k]} · {tit.lower()}")
    return l


def bloques_por_nivel(items):
    """Secciones por veredicto, tarjetas separadas por una linea fina."""
    bloques = []
    for k in ("objetivo", "compra", "bueno", "regular", "normal"):
        grupo = [it for it in items if it["nivel"] == k]
        if not grupo:
            continue
        _, emo, tit = NIVELES[k]
        bloques.append(f"━━━ {emo} <b>{tit}</b> ({len(grupo)}) ━━━")
        for it in grupo:
            bloques.append((it["texto"] + f"\n{SEP_TARJETA}", it.get("boton")))
    return bloques


LEYENDA = ("🟢 compra ya · 🟡 buen precio · 🟠 regular · ⚪ normal · 🎯 tu objetivo\n"
           "🏪 vendedor externo del marketplace · 🥇🥈🥉 ranking de tiendas")


def armar_mensajes(alertas, state=None, hist=None):
    """Alerta automatica: resumen ejecutivo + tarjetas ordenadas por veredicto."""
    state, hist = state or {}, hist or {}
    items, avisos = [], []
    for a in alertas:
        if a["tipo"] == "aviso":
            avisos.append(a["linea"])
            continue
        if a.get("ref") is not None:
            if a["tipo"] == "fisico":
                s = (state.get("productos") or {}).get(a["ref"])
                if s:
                    nivel, texto = tarjeta_fisico(a["ref"], s, state, hist, a.get("extra"))
                    items.append({"nivel": nivel, "texto": texto, "score": a["score"], "s": s, "tipo": "fisico",
                                  "boton": boton_de(s, "fisico") if s.get("id") else None})
                    continue
            else:
                e = (state.get("eshop") or {}).get(a["ref"])
                if e:
                    nivel, texto = tarjeta_digital(a["ref"], e, state, hist, a.get("extra"))
                    items.append({"nivel": nivel, "texto": texto, "score": a["score"], "s": e, "tipo": "digital",
                                  "boton": boton_de(e, "digital") if e.get("id") else None})
                    continue
        nivel = a["extra"] if a["tipo"] == "memoria" and a.get("extra") in NIVELES else "bueno"
        items.append({"nivel": nivel, "texto": a["linea"], "score": a["score"], "tipo": a["tipo"]})
    if not items and not avisos:
        return []
    items.sort(key=lambda it: (NIVELES[it["nivel"]][0], -it["score"]))
    titulo = "🔔 <b>ALERTA DE OFERTAS</b>"
    sub = f"{fecha_hora_chile()} · revisión automática · {len(items)} novedad{'es' if len(items) != 1 else ''}"
    res = ["📋 <b>RESUMEN</b>"] + resumen_niveles(items)
    if items:
        top = items[0]
        s = top["s"] if "s" in top else None
        if s:
            nombre = s.get("nombre") or s.get("titulo")
            precio = clp(s["precio"]) if top["tipo"] == "fisico" else usd(s["ultimo"])
            donde = s.get("tienda") or pais_txt(s.get("pais", "US"))
            res.append(f"⭐ Mejor oportunidad: <b>{esc(nombre)}</b> a {precio} en {esc(donde)}")
    bloques = [cabecera(titulo, sub) + "\n" + "\n".join(res)]
    bloques += bloques_por_nivel(items)
    if avisos:
        bloques.append("⚠️ <b>AVISOS DEL BOT</b>\n" + "\n".join(f"• {x}" for x in avisos))
    bloques.append(f"{LINEA}\n{LEYENDA}\n\nVer todo lo vigente: /ofertas · Por saga: /precios")
    return empaquetar(bloques, titulo)


# ---------------------------------------------------------------- vistas

def generar_vistas(state, hist, cfg):
    """Deja armadas las tarjetas para que el Worker de Telegram solo las envie."""
    VISTAS_DIR.mkdir(exist_ok=True)
    (VISTAS_DIR / "saga").mkdir(exist_ok=True)
    revision = state.get("ultima_revision")
    corte = None
    if revision:
        try:
            from datetime import datetime, timedelta
            corte = (datetime.fromisoformat(revision.replace("Z", "+00:00")) - timedelta(hours=12)
                     ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            corte = None

    def vigente(x):
        return not corte or (x.get("visto") or "") >= corte

    ofertas, sagas, ids = [], {}, {}
    for clave, s in (state.get("productos") or {}).items():
        if not vigente(s):
            continue
        s.setdefault("id", id_de("f", clave))
        nivel, texto = tarjeta_fisico(clave, s, state, hist)
        it = {"nivel": nivel, "orden": NIVELES[nivel][0], "pct": round(s.get("pct") or 0, 1),
              "tipo": "fisico", "juego": s["juego"], "nombre": s["nombre"], "texto": texto,
              "precio": s["precio"], "id": s["id"], "boton": boton_de(s, "fisico")}
        ids[s["id"]] = {"tipo": "fisico", "clave": clave, "nombre": s["nombre"], "juego": s["juego"],
                        "plataforma": s.get("plataforma"), "precio": s["precio"], "moneda": "CLP",
                        "tienda": s.get("tienda"), "url": s.get("url"), "saga": slug(s["juego"]),
                        "min": s.get("min_hist")}
        sagas.setdefault(s["juego"], []).append(it)
        if nivel in ("objetivo", "compra", "bueno", "regular"):
            ofertas.append(it)
    ligados = {s.get("nsuid") for s in (state.get("productos") or {}).values() if s.get("nsuid")}
    for nsuid, e in (state.get("eshop") or {}).items():
        if not vigente(e) or e.get("ultimo") is None:
            continue
        e.setdefault("id", id_de("d", nsuid))
        nivel, texto = tarjeta_digital(nsuid, e, state, hist)
        it = {"nivel": nivel, "orden": NIVELES[nivel][0], "pct": round(e.get("pct_oferta") or 0, 1),
              "tipo": "digital", "juego": e["juego"], "nombre": e["titulo"], "texto": texto,
              "precio_usd": e["ultimo"], "con_fisico": nsuid in ligados,
              "id": e["id"], "boton": boton_de(e, "digital")}
        ids[e["id"]] = {"tipo": "digital", "clave": nsuid, "nombre": e["titulo"], "juego": e["juego"],
                        "plataforma": e.get("plataforma"), "precio": e["ultimo"], "moneda": "USD",
                        "tienda": "eShop " + e.get("pais", "US"), "url": url_pais(e.get("url"), e.get("pais", "US")),
                        "saga": slug(e["juego"]), "min": e.get("min_hist")}
        sagas.setdefault(e["juego"], []).append(it)
        if nivel in ("objetivo", "compra", "bueno", "regular"):
            ofertas.append(it)
    ofertas.sort(key=lambda it: (it["orden"], -it["pct"]))
    save_json(VISTAS_DIR / "ofertas.json", {"revision": revision, "items": ofertas})

    indice = []
    for juego, its in sorted(sagas.items(), key=lambda x: norm(x[0])):
        its.sort(key=lambda it: (it["orden"], -it["pct"]))
        fis = [i for i in its if i["tipo"] == "fisico"]
        dig = [i for i in its if i["tipo"] == "digital"]
        mejor = its[0]
        partes = []
        if fis:
            partes.append(f"📦 {len(fis)} desde {clp(min(i['precio'] for i in fis))}")
        if dig:
            partes.append(f"🎮 {len(dig)} desde {usd(min(i['precio_usd'] for i in dig))}")
        destacado = ""
        if mejor["orden"] <= 2:
            destacado = f"\n   {NIVELES[mejor['nivel']][1]} {esc(mejor['nombre'])}" + \
                (f" −{round(mejor['pct'])}%" if mejor["pct"] >= 5 else "")
        indice.append({"juego": juego, "slug": slug(juego), "orden": mejor["orden"],
                       "linea": f"{NIVELES[mejor['nivel']][1]} <b>{esc(juego)}</b> — {' · '.join(partes)}{destacado}",
                       "nombres": sorted({norm(i["nombre"]) for i in its})})
        save_json(VISTAS_DIR / "saga" / f"{slug(juego)}.json", {
            "revision": revision, "juego": juego,
            "items": [{k: i[k] for k in ("nivel", "tipo", "texto", "pct", "con_fisico", "nombre", "id", "boton")
                       if k in i} for i in its]})
    save_json(VISTAS_DIR / "indice.json", {"revision": revision, "sagas": indice,
                                           "fallos": state.get("fallos") or {},
                                           "fisicos": sum(1 for x in ofertas), "total": sum(len(v) for v in sagas.values())})
    save_json(VISTAS_DIR / "ids.json", ids)
    log(f"  vistas: {len(ofertas)} ofertas, {len(indice)} sagas")


# ------------------------------------------------------------- diagnostico

def diagnostico(cfg):
    termino = sys.argv[sys.argv.index("--diagnostico") + 1] \
        if len(sys.argv) > sys.argv.index("--diagnostico") + 1 else "zelda"
    juego = {"nombre": termino, "buscar": termino}
    log(f"=== DIAGNOSTICO con la busqueda '{termino}' ===\n")
    log("eShop EE. UU.:")
    try:
        r = get(ALGOLIA_URL, headers={**HEADERS, **ALGOLIA_HEADERS},
                params={"query": termino, "hitsPerPage": 1})
        hit = (r.json().get("hits") or [{}])[0]
        log(f"  campos del catalogo: {', '.join(sorted(hit))}")
        log(f"  datos para el canal: {meta_algolia(hit) or 'ninguno (revisar META_ALGOLIA)'}")
    except (requests.RequestException, ValueError) as e:
        log(f"  campos del catalogo: ERROR {e}")
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
        ok = send_telegram("✅ <b>PRUEBA DE CONEXIÓN</b>\nSwitchBot está conectado y funcionando.")
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
    alertas = []
    global SEG
    SEG = load_json(SEG_FILE, {})

    hist = load_json(HIST_FILE, {})
    log("Revisando eShop...")
    revisar_eshop(cfg, state, hist, alertas)
    log("Revisando tiendas...")
    listados = leer_tiendas(cfg, state, alertas)
    revisar_productos(cfg, state, hist, listados, alertas)
    try:
        revisar_memorias(cfg, state, hist, alertas)
    except Exception as e:  # noqa: BLE001 - las memorias nunca deben botar la revision
        log(f"  memorias: error {type(e).__name__}: {e}")
    state.pop("tiendas", None)          # formato anterior (por aviso), ya no se usa
    for viejo in ("digitales", "fisicos", "nsuids"):
        state.pop(viejo, None)
    state["ultima_revision"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["descubrimiento_listo"] = True

    # juegos que aparecen por primera vez: registrados en silencio
    nuevos_p = [s for s in state.get("productos", {}).values() if s.get("visto_desde") is None]
    nuevos_e = [s for s in state.get("eshop", {}).values() if s.get("visto_desde") is None]
    for s in nuevos_p + nuevos_e:
        s["visto_desde"] = hoy_chile()
    alertas = filtrar_alertas(alertas, state, cfg)
    mensajes = armar_mensajes(alertas, state, hist)
    if len(nuevos_p) + len(nuevos_e) >= 5:
        con_oferta = sum(1 for s in nuevos_p if (s.get("pct") or 0) >= 15) + \
            sum(1 for s in nuevos_e if s.get("normal"))
        mensajes.insert(0, "\n".join([
            "🆕 <b>BASE DE DATOS AMPLIADA</b>",
            f"<i>{fecha_hora_chile()}</i>", LINEA,
            f"📦 {len(nuevos_p)} productos físicos nuevos",
            f"🎮 {len(nuevos_e)} juegos de la eShop nuevos",
            f"🔥 {con_oferta} ya están con descuento",
            "", "Los registré en silencio para no llenarte de alertas.",
            "Desde ahora te aviso solo cuando bajen de precio.",
            LINEA, "Ver las ofertas de hoy: /ofertas"]))
    mensajes = recortar_mensajes(mensajes, cfg.get("ajustes", {}).get("max_mensajes", 12))
    bloqueado = state.get("telegram_bloqueado_hasta")
    if bloqueado and bloqueado > time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()):
        log(f"Telegram tiene bloqueado al bot hasta {bloqueado}: esta vez no mando nada.")
        BLOQUEO["hasta"] = bloqueado
    else:
        state.pop("telegram_bloqueado_hasta", None)
    enviados = 0
    for m in mensajes:
        if not send_telegram(m):
            break
        enviados += 1
    if BLOQUEO.get("hasta"):
        state["telegram_bloqueado_hasta"] = BLOQUEO["hasta"]
    if enviados < len(mensajes):
        # no llegaron: se deshacen las marcas para que se vuelvan a avisar la proxima vez
        for s, previo in PENDIENTES:
            s["avisado"] = previo
        log("Telegram fallo: estas ofertas se volveran a intentar en la proxima revision.")
    save_json(STATE_FILE, state)
    save_json(HIST_FILE, hist)
    try:
        generar_vistas(state, hist, cfg)
    except Exception as e:  # noqa: BLE001 - las vistas nunca deben botar la revision
        log(f"  vistas: error {type(e).__name__}: {e}")
    log(f"Listo: {enviados}/{len(mensajes)} mensajes enviados.")


if __name__ == "__main__":
    main()
