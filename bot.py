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
# Presupuesto de tiempo de una revision (GitHub la corta a los 45 min y entonces NO se guarda
# nada). Lo opcional (catalogos completos, MercadoLibre) se salta si ya se paso del limite.
INICIO = time.time()
LIMITE_CATALOGO_MIN = 14    # despues de esto, los catalogos completos se cortan
LIMITE_ML_MIN = 24          # despues de esto, no se consulta mas MercadoLibre en esta revision
LIMITE_TIENDAS_MIN = 20     # despues de esto, cada tienda deja de buscar (se queda con lo que tiene)


def minutos():
    return (time.time() - INICIO) / 60
FALLOS_PARA_AVISAR = 5      # corridas seguidas sin resultados antes de avisar
MAX_PAGINAS_SHOPIFY = 40    # 250 productos por pagina
TELEGRAM_MAX = 3900

PLATAFORMA = {"switch", "nsw", "nswi", "ns2", "sw2", "switch2", "nsw2"}
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


BLOQUEO_RE = re.compile(r"cf-chl|challenge-platform|Attention Required|Access Denied|captcha|"
                        r"Request unsuccessful|Incapsula|_Incapsula_|px-captcha|Pardon Our Interruption", re.I)
VIA_WORKER = set()          # dominios que se leen por el Worker (tiendas con "via": "worker")
RELAY_USADO = set()         # dominios que tuvieron que pasar por el Worker en esta corrida


def dominio(url):
    return re.sub(r"^https?://(www\.)?", "", str(url)).split("/")[0].lower()


def _relay(url, kw):
    """Pide la pagina a traves del Worker de Cloudflare (sale por otra red: muchas tiendas
    bloquean los servidores de GitHub pero no los de Cloudflare)."""
    base = os.environ.get("WORKER_URL", "").rstrip("/")
    clave = os.environ.get("PROXY_KEY", "")
    completa = requests.Request("GET", url, params=kw.get("params")).prepare().url
    r = requests.get(f"{base}/proxy", params={"u": completa},
                     headers={"X-Proxy-Key": clave}, timeout=kw.get("timeout", TIMEOUT) + 15)
    r.url = completa            # los links relativos de la pagina se arman con la tienda, no con el Worker
    return r


def bloqueado(r):
    return r.status_code in (401, 403, 429, 503) or (
        r.status_code == 200 and len(r.text) < 20000 and bool(BLOQUEO_RE.search(r.text[:20000])))


def get(url, **kw):
    kw.setdefault("headers", HEADERS)
    kw.setdefault("timeout", TIMEOUT)
    hay_relay = bool(os.environ.get("WORKER_URL") and os.environ.get("PROXY_KEY"))
    dom = dominio(url)
    if hay_relay and dom in VIA_WORKER:
        return _relay(url, kw)
    try:
        r = requests.get(url, **kw)
    except requests.RequestException:
        if not (hay_relay and dom.endswith(".cl")):
            raise
        r = None
    # bloqueado o caido: se reintenta por el Worker (solo tiendas chilenas)
    if hay_relay and dom.endswith(".cl") and (r is None or bloqueado(r)):
        try:
            r2 = _relay(url, kw)
            if r2.status_code == 200 and not bloqueado(r2):
                RELAY_USADO.add(dom)
                return r2
        except requests.RequestException:
            pass
        if r is None:
            raise requests.RequestException(f"{dom}: sin respuesta directa ni por el Worker")
    return r


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
    "generos": ("gameGenreLabels", "gameGenres", "genres"),
    "franquicias": ("franchises",),
    "popularidad": ("popularityRank",),
    "tendencia": ("trendingRank",),
    "lanzamiento": ("releaseDate", "releaseDateDisplay"),
    "imagen": ("productImage", "horizontalHeaderImage"),   # para la vista previa del canal
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


PREFIJOS_ESHOP = ("7001", "7007")    # 7001 = juegos, 7007 = paquetes ("Paquete" en la eShop)


def _hit_oferta(h):
    nsuid = str(h.get("nsuid") or "")
    if not nsuid.startswith(PREFIJOS_ESHOP):
        return None
    url = h.get("url") or ""
    precio = h.get("price") if isinstance(h.get("price"), dict) else {}
    return {"titulo": re.sub(r"[™®©]", "", h.get("title", nsuid)).strip(),
            "nsuid": nsuid, "url": urljoin(NINTENDO_WEB, url) if url else "",
            "plataforma": "Switch 2" if h.get("platformCode") == "NINTENDO_SWITCH_2" else "Switch",
            "paquete": nsuid.startswith("7007"),
            "normal_usd": precio.get("regPrice"),
            **meta_algolia(h)}


def _algolia(filtro, page=0, por_pagina=500, facetas=None):
    params = {"query": "", "filters": filtro, "hitsPerPage": por_pagina, "page": page}
    if facetas:
        params["facets"] = json.dumps(facetas)
        params["maxValuesPerFacet"] = 100
    r = get(ALGOLIA_URL, headers={**HEADERS, **ALGOLIA_HEADERS}, params=params)
    return r.json() if r.status_code == 200 else None


FACETAS_PARTIR = ["priceRange", "platformCode", "contentRatingCode", "gameGenreLabels", "playerCount",
                  "availability"]


def _partir(filtro, total, usadas, partes, profundidad=0):
    """Divide una consulta de mas de 1000 resultados usando las facetas del buscador."""
    if total <= 1000 or profundidad >= 4:
        partes.append(filtro)
        return
    libres = [f for f in FACETAS_PARTIR if f not in usadas]
    try:
        res = _algolia(filtro, por_pagina=0, facetas=libres) or {}
    except (requests.RequestException, ValueError):
        res = {}
    fac = res.get("facets") or {}
    # la faceta que deja el grupo mas grande mas chico
    # solo facetas que cubren (casi) todo el grupo: si no, los juegos sin ese dato se perderian
    utiles = [f for f in libres if fac.get(f) and len(fac[f]) > 1 and sum(fac[f].values()) >= 0.97 * total]
    mejor = min(utiles, key=lambda f: max(fac[f].values()), default=None)
    if not mejor:
        partes.append(filtro)
        return
    for valor, n in fac[mejor].items():
        valor_esc = str(valor).replace('"', '\\"')
        _partir(f'{filtro} AND {mejor}:"{valor_esc}"', n, usadas | {mejor}, partes, profundidad + 1)


def buscar_ofertas_eshop(maximo=9000):
    """Todos los juegos (y paquetes) con descuento en la eShop de EE. UU.
    El buscador de Nintendo entrega como maximo 1000 resultados por consulta, y una venta
    grande tiene 5000+; por eso se parte la busqueda (precio, consola, clasificacion, genero...)."""
    for filtro in FILTROS_OFERTA:
        try:
            resumen = _algolia(filtro, por_pagina=0)
        except (requests.RequestException, ValueError) as e:
            log(f"  eShop ofertas [{filtro}]: {e}")
            continue
        if not resumen or not resumen.get("nbHits"):
            log(f"  eShop ofertas: filtro {filtro} sin resultados")
            continue
        total = resumen["nbHits"]
        partes = []
        _partir(filtro, total, set(), partes)
        vistos, out = set(), []
        for f in partes:
            page = 0
            while len(out) < maximo:
                try:
                    data = _algolia(f, page)
                except (requests.RequestException, ValueError) as e:
                    log(f"  eShop ofertas [{f}]: {e}")
                    break
                if not data:
                    break
                for h in data.get("hits", []):
                    x = _hit_oferta(h)
                    if x and x["nsuid"] not in vistos:
                        vistos.add(x["nsuid"])
                        out.append(x)
                page += 1
                if page >= min(data.get("nbPages") or 0, 2):    # 2 x 500 = el maximo de 1000
                    break
                time.sleep(0.2)
        if out:
            log(f"  eShop ofertas: {total} en oferta segun Nintendo, {len(out)} leidos en {len(partes)} consultas")
            return out
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


def parse_filas_buscador(pagina, url_pagina):
    """Buscador rapido de TodoJuegos (buscador_ajax_plano.asp): filas con link y precio."""
    out = []
    for b in re.split(r'class="filaProductoBuscador"', pagina)[1:]:
        a = re.search(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, flags=re.S | re.I)
        m = re.search(r"\$\s*([\d.]+)", b)
        if not a or not m:
            continue                    # "$ - -" = sin precio (sin stock o por confirmar)
        titulo = html.unescape(re.sub(r"<[^>]+>", "", a.group(2))).strip()
        out.append({"titulo": titulo, "url": urljoin(url_pagina, a.group(1)),
                    "precio": parse_precio(m.group(1)), "normal": None})
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
    propia = norm(tienda["nombre"]).replace(" ", "")
    for texto in (producto.get("titulo", ""), re.sub(r"^https?://[^/]+", "", producto.get("url", ""))):
        m = EXTERNOS_RE.search(texto)
        # "Sniper" o "MegaGames" son externos en Falabella, pero no en su propia tienda
        if m and norm(m.group(0)).replace(" ", "") not in propia:
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
        prods = parse_magento(r.text, r.url) or parse_filas_buscador(r.text, r.url) or parse_rsc(r.text, r.url) or parse_embebido(r.text, r.url)
        if prods:
            cache["plantilla_html"] = t
            return prods
        if plantilla:
            # pagina de busqueda conocida que respondio bien pero sin resultados: la tienda no
            # tiene ese juego. Sin esto se probaban Shopify, WooCommerce y VTEX por cada juego
            # (asi TodoJuegos llego a tardar 18 min por revision).
            return []
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
    "todojuegos.cl": "https://www.todojuegos.cl/Productos/buscador_ajax_plano.asp?consulta={q}",
}


# catalogo completo de juegos de Switch en tiendas que no son Shopify: busquedas genericas
# paginadas. {p} = numero de pagina, {s} = desplazamiento ((p-1) * tamano). Se detiene cuando
# una pagina ya no trae productos nuevos (asi un parametro de pagina equivocado cuesta 2 consultas).
CATALOGO_CONOCIDO = {
    "falabella.com": ["https://www.falabella.com/falabella-cl/search?Ntt=juegos+nintendo+switch&page={p}",
                      "https://www.falabella.com/falabella-cl/search?Ntt=juego+nintendo+switch+2&page={p}"],
    "lider.cl": ["https://www.lider.cl/search?q=juegos+nintendo+switch&page={p}",
                 "https://www.lider.cl/search?q=juego+switch+2&page={p}"],
    "paris.cl": ["https://www.paris.cl/search/?q=juegos+nintendo+switch&page={p}",
                 "https://www.paris.cl/search/?q=juegos+nintendo+switch&start={s}&sz=24"],
}
CATALOGO_PAGINAS = 15


def recorrer_catalogo(tienda, cache, paginas=None):
    """Todos los juegos de Switch de la tienda, pagina por pagina. Devuelve [producto]."""
    conf = tienda.get("catalogo", True)
    if conf is False:
        return []
    if cache.get("metodo") == "woocommerce":
        return catalogo_woocommerce(tienda["url"].rstrip("/"), cache, paginas or 10)
    plantillas = conf.get("urls") if isinstance(conf, dict) else None
    if not plantillas:
        plantillas = next((v for k, v in CATALOGO_CONOCIDO.items() if k in tienda["url"]), [])
    paginas = paginas or (conf.get("paginas") if isinstance(conf, dict) else None) or CATALOGO_PAGINAS
    vistos, out = set(), []
    for t in plantillas:
        for p in range(1, paginas + 1):
            if minutos() > LIMITE_CATALOGO_MIN and not VOLCAR:
                cache.setdefault("diag", []).append(f"catalogo cortado a los {minutos():.0f} min")
                break
            url = t.format(p=p, s=(p - 1) * 24)
            try:
                r = get(url)
            except requests.RequestException as e:
                cache.setdefault("diag", []).append(f"catalogo {url}: {type(e).__name__}")
                break
            if r.status_code != 200:
                cache.setdefault("diag", []).append(f"catalogo {url} -> HTTP {r.status_code}")
                break
            prods = parse_magento(r.text, r.url) or parse_filas_buscador(r.text, r.url) or parse_rsc(r.text, r.url) or parse_embebido(r.text, r.url) or []
            nuevos = [x for x in prods if x.get("url") and x["url"] not in vistos]
            for x in nuevos:
                vistos.add(x["url"])
            out += nuevos
            if not prods or (not nuevos and p > 1):
                break                   # la pagina 1 puede repetir otra busqueda: se mira la 2 igual
            time.sleep(PAUSA)
    cache.setdefault("diag", []).append(f"catalogo: {len(out)} productos")
    return out


def catalogo_woocommerce(base, cache, paginas):
    """WooCommerce: todos los productos que dicen 'switch' o 'nsw', de a 100 por pagina."""
    out, vistos = [], set()
    agotada = set()
    for busca, p in [(b, n) for b in ("switch", "nsw") for n in range(1, paginas + 1)]:
        if busca in agotada:
            continue
        try:
            r = get(f"{base}/wp-json/wc/store/v1/products",
                    params={"search": busca, "per_page": 100, "page": p})
            data = r.json() if r.status_code == 200 else []
        except (requests.RequestException, ValueError):
            data = []
        if not isinstance(data, list) or not data:
            agotada.add(busca)          # sin mas paginas para esta busqueda
            continue
        for x in data:
            if not isinstance(x, dict) or x.get("is_in_stock") is False or x.get("permalink") in vistos:
                continue
            pr = x.get("prices") or {}
            div = 10 ** int(pr.get("currency_minor_unit") or 0)
            precio, normal = parse_precio(pr.get("price")), parse_precio(pr.get("regular_price"))
            vistos.add(x.get("permalink"))
            out.append({"titulo": html.unescape(x.get("name", "")), "url": x.get("permalink", ""),
                        "precio": precio / div if precio else None, "normal": normal / div if normal else None})
        time.sleep(PAUSA)               # algunas tiendas entregan menos de 100 por pagina: se sigue
    cache.setdefault("diag", []).append(f"catalogo woocommerce: {len(out)} productos")
    return out


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
RUIDO = {"juego", "videojuego", "videojuegos", "nintendo", "switch", "nsw", "nswi", "fisico",
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


PREVENTA_RE = re.compile(r"\bpre ?ventas?\b|\bpre ?orden(es)?\b|\bpre ?order\b|\breserva\b")
LIMPIAR_RE = re.compile(
    r"\bpre-?ventas?\b|\bpre-?orden\b|\bpre-?order\b|\b(video)?juegos?\b|\bnintendo\s+switch\s*2\b|\bnintendo\s+switch\b|\bswitch\s*2\b|"
    r"\bswitch\b|\bnswi\b|\bnsw2?\b|\bns2\b|\bsw2\b|\(\s*\)|\bf[ií]sico\b|\bphysical( game card)?\b|"
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


def negocio(h, s, hoy=None):
    """Lectura de negocio de un fisico: margen contra las otras tiendas, escasez y tendencia.
    Usa el historial por tienda (t) y la cantidad de tiendas por dia (n)."""
    hoy = hoy or hoy_chile()
    out = {}
    reales = [a for a in (s.get("avisos") or []) if not a.get("externo")]
    if len(reales) >= 2:
        otras = sorted(a["precio"] for a in reales[1:])
        ref = _mediana(otras)
        if ref <= 2 * s["precio"]:          # mas que el doble suele ser otra edicion
            out["margen"] = ref - s["precio"]
            out["margen_pct"] = 100 * out["margen"] / ref
            out["ref_tiendas"] = ref
            out["n_otras"] = len(otras)
    n = {d: v for d, v in (h.get("n") or {}).items() if d >= _sumar_dias(hoy, -14)}
    if n:
        hoy_n = n.get(hoy, len(reales))
        antes = max((v for d, v in n.items() if d < hoy), default=hoy_n)
        out["tiendas_hoy"], out["tiendas_max"] = hoy_n, antes
        out["escasez"] = antes >= 3 and hoy_n <= 1
    f = {d: (v[0] if isinstance(v, list) else v) for d, v in (h.get("f") or {}).items()}
    viejos = [v for d, v in f.items() if _sumar_dias(hoy, -14) <= d <= _sumar_dias(hoy, -7)]
    if viejos and s.get("precio"):
        out["tendencia_pct"] = 100 * (s["precio"] / _mediana(viejos) - 1)
    return out


def lineas_negocio(h, s):
    ng = negocio(h, s)
    l = []
    if ng.get("margen") and ng["margen_pct"] >= 10:
        quien = "la otra tienda" if ng.get("n_otras") == 1 else "las otras tiendas"
        l.append(f"💼 <b>Margen</b>: {quien} lo vende{'' if ng.get('n_otras') == 1 else 'n'} a {clp(ng['ref_tiendas'])} → "
                 f"~{clp(ng['margen'])} ({round(ng['margen_pct'])}%) de diferencia")
    if ng.get("escasez"):
        l.append(f"📦 <b>Se está agotando</b>: estaba en {ng['tiendas_max']} tiendas, hoy en {ng['tiendas_hoy']}")
    t = ng.get("tendencia_pct")
    if t is not None and abs(t) >= 8:
        l.append(f"{'📈' if t > 0 else '📉'} Precio {'subió' if t > 0 else 'bajó'} {abs(round(t))}% en las últimas 2 semanas")
    return l


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

_CFG_CANAL = []


def cfg_canal():
    if not _CFG_CANAL:
        _CFG_CANAL.append(load_json(BASE / "canal.json", None))
    return _CFG_CANAL[0]


def es_basura_descubierta(ccfg, h, normal_usd):
    try:
        import canal
        return canal.basura_por_nombre(ccfg, h["titulo"], h.get("editora"), normal_usd, h.get("popularidad"))
    except Exception as e:  # noqa: BLE001 - ante la duda, se guarda
        log(f"  filtro de basura: error {type(e).__name__}: {e}")
        return None


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
        ccfg_pre = cfg_canal()
        relleno_pre = 0
        for h in buscar_ofertas_eshop():
            if h["nsuid"] in encontrados:
                continue
            # el relleno obvio (por nombre, editora o precio normal) se descarta antes de pedir
            # precios en 7 paises: asi una venta de 5000 juegos no alarga la revision
            if ccfg_pre and es_basura_descubierta(ccfg_pre, h, h.get("normal_usd")):
                relleno_pre += 1
                continue
            encontrados[h["nsuid"]] = (JUEGO_OTROS, h)
            nuevos += 1
        log(f"  eShop: {nuevos} juegos en oferta descubiertos fuera de tu lista ({relleno_pre} de relleno descartados)")
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
    ccfg = cfg_canal()
    basura = 0
    for nsuid, (juego, h) in sorted(encontrados.items(), key=lambda x: x[1][1]["titulo"]):
        regs = porreg.get(nsuid)
        if not regs:
            continue
        base = regs.get(pais) or next(iter(regs.values()))
        # juegos descubiertos que son relleno: ni se guardan ni se avisan (tu lista no se toca)
        if juego.get("auto") and ccfg and es_basura_descubierta(ccfg, h, base["regular_usd"]):
            basura += 1
            items.pop(nsuid, None)
            hist_e.pop(nsuid, None)
            (hist.get("eshop_ofertas") or {}).pop(nsuid, None)
            continue
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
    if basura:
        log(f"  eShop: {basura} juegos descubiertos de relleno descartados (no se guardan)")


def _leer_una_tienda(tienda, juegos, state):
    cache = {}
    hallados = {}  # url -> (juego, producto)
    respondio = False
    vistos_terminos = set()
    for juego in juegos:
        if juego.get("fisico") is False:
            continue
        if minutos() > LIMITE_TIENDAS_MIN and not VOLCAR:
            cache.setdefault("diag", []).append(f"busqueda cortada a los {minutos():.0f} min")
            log(f"  {tienda['nombre']}: corto la busqueda a los {minutos():.0f} min (lenta)")
            break
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
    # tiendas Shopify: ya se bajo el catalogo completo, se aprovechan todos los juegos de Switch.
    # Las demas (Falabella, Lider, Paris): se recorre su catalogo de juegos de Switch.
    if state.get("_descubrir", True) and respondio and cache.get("metodo") != "shopify":
        cache["catalogo"] = recorrer_catalogo(tienda, cache)
    if state.get("_descubrir", True):
        for p in (cache.get("shopify") or []) + (cache.get("catalogo") or []):
            if p["url"] not in hallados and precio_valido(p.get("precio")) and es_juego_switch(p["titulo"]):
                hallados[p["url"]] = (JUEGO_OTROS, p)
    # memorias microSD (van aparte de los juegos: tienen su propio filtro)
    terminos_mem = state.get("_memorias") or []
    if terminos_mem and respondio:
        mem = {}
        fuentes = [cache.get("shopify")] if cache.get("metodo") == "shopify" else []
        if not fuentes:
            for t in terminos_mem:
                if minutos() > LIMITE_TIENDAS_MIN:
                    break
                prods = buscar_en_tienda(tienda, t, state, cache)
                fuentes.append(prods or [])
                time.sleep(PAUSA)
        for prods in fuentes:
            for p in prods or []:
                if precio_valido(p.get("precio")) and clasificar_memoria(p["titulo"], p["precio"]):
                    mem.setdefault(p["url"], p)
        cache["memorias"] = mem
    # categorias extra en seguimiento (accesorios, consolas): una vez al dia, con limite de tiempo
    for cat, terminos_cat in (state.get("_extras") or {}).items():
        if not terminos_cat or not respondio:
            continue
        encontrados = {}
        fuentes = [cache.get("shopify")] if cache.get("metodo") == "shopify" else [cache.get("catalogo")]
        if cache.get("metodo") != "shopify":
            for t in terminos_cat:
                if minutos() > LIMITE_TIENDAS_MIN:
                    break
                fuentes.append(buscar_en_tienda(tienda, t, state, cache) or [])
                time.sleep(PAUSA)
        for prods in fuentes:
            for p in prods or []:
                if precio_valido(p.get("precio")) and CLASIFICADORES[cat](p["titulo"], p["precio"], p.get("url")):
                    encontrados.setdefault(p["url"], p)
        cache["extra_" + cat] = encontrados
    return respondio, hallados, cache


# consolas y hardware que aparecen en las busquedas generales ("Nintendo Switch OLED 64GB")
HARDWARE_RE = re.compile(r"\b(oled|lite|\d+ ?gb|\d+ ?tb|reacondicionad[oa]s?|joy ?cons?|pro controller|"
                         r"mando|base de carga|bateria|pantalla|carcasa|mantencion|reparacion|analog caps|grips?|thumb)\b")


def es_juego_switch(titulo):
    w = palabras(titulo)
    return bool(w & PLATAFORMA) and not (w & EXCLUIR_SIEMPRE) and not HARDWARE_RE.search(norm(titulo))


def _leer_seguro(tienda, juegos, state):
    """Si una tienda revienta, se cuenta como fallo y las demas siguen."""
    t0 = time.time()
    try:
        r = _leer_una_tienda(tienda, juegos, state)
        r[2]["seg"] = time.time() - t0
        return r
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
    VIA_WORKER.clear()
    VIA_WORKER.update(dominio(t["url"]) for t in tiendas if t.get("via") == "worker")
    mem_cfg = cfg.get("memorias") or {}
    state["_memorias"] = mem_cfg.get("buscar", MEMORIAS_BUSCAR) if mem_cfg.get("activa", True) else []
    MEMORIAS_LISTADOS.clear()
    extras = {}
    for cat, (lista_cat, terminos_def) in EXTRAS.items():
        lista_cat.clear()
        if cat == "seguimientos":       # tus seguimientos: en cada revision, no una vez al dia
            SEGUIMIENTOS_CFG[:] = [x for x in cfg.get("seguimientos") or [] if x.get("activa", True) is not False]
            terms = [t for x in SEGUIMIENTOS_CFG for t in terminos(x)]
            if terms:
                extras[cat] = terms
            continue
        conf = cfg.get(cat) or {}
        if conf.get("activa", True) is not False and state.get(f"{cat}_fecha") != hoy_chile():
            extras[cat] = conf.get("buscar", terminos_def)
    state["_extras"] = extras
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, len(tiendas))) as ex:
        resultados = list(ex.map(lambda t: (t, *_leer_seguro(t, juegos, state)), tiendas))
    state.pop("_descubrir", None)
    state.pop("_memorias", None)
    state.pop("_extras", None)
    for tienda, respondio, _, cache in resultados:
        for p in (cache.get("memorias") or {}).values():
            p["externo"] = es_externo(p, tienda)
            MEMORIAS_LISTADOS.append((tienda["nombre"], p))
        for cat in extras:
            for p in (cache.get("extra_" + cat) or {}).values():
                p["externo"] = es_externo(p, tienda)
                EXTRAS[cat][0].append((tienda["nombre"], p))
    for cat in extras:
        if EXTRAS[cat][0] and cat != "seguimientos":
            state[f"{cat}_fecha"] = hoy_chile()
    log(f"  tiendas leidas en {time.time() - t0:.0f} s")
    for tienda, _, _, cache in resultados:
        if cache.get("seg"):
            log(f"    {tienda['nombre']}: {cache['seg']:.0f} s")
    if RELAY_USADO:
        log(f"  leidas por el Worker (bloqueaban a GitHub): {', '.join(sorted(RELAY_USADO))}")
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
        # y una tienda que cobra 2,5 veces o mas casi siempre vende otra edicion (coleccionista,
        # importada): compararla inventaria descuentos de 75 % que no existen
        otras = [a["precio"] for a in avisos[1:] if a["precio"] <= 2 * med_todos and a["precio"] <= 2.5 * precio]
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

        s["preventa"] = any(PREVENTA_RE.search(norm(a.get("titulo") or "")) for a in avisos)
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
        if negocio(h, s, hoy).get("escasez") and not s.get("avisado_escasez"):
            motivos.append("escasez")
            s["avisado_escasez"] = hoy
        elif not negocio(h, s, hoy).get("escasez"):
            s.pop("avisado_escasez", None)

        silencio = anterior is None and (not juego.get("auto") or not state.get("descubrimiento_listo"))
        # preventa de algo de tu lista o de una edicion especial: se avisa una vez (aunque sea nuevo)
        if s["preventa"] and not s.get("avisado_preventa") and (not juego.get("auto") or g["variante"]):
            motivos.append("preventa")
            s["avisado_preventa"] = hoy
        avisar = decidir(s, precio, motivos, nuevo=silencio)
        if ("escasez" in motivos and not silencio) or "preventa" in motivos:
            avisar = True                   # se avisa una vez aunque el precio no haya bajado
        if avisar and not silenciado:
            agregar_alerta(alertas, "fisico", juego["nombre"],
                           linea_producto(s, motivos, anterior, e),
                           puntaje(s.get("pct") or 0, motivos), ref=clave,
                           extra=extra_motivos(motivos, anterior, precio, clp))


def fechar_tiendas(state, hist, cfg):
    """Desde que dia el bot tiene datos de cada tienda (state['tiendas_desde']). El canal no
    publica bombazos de una tienda recien agregada: sin historial, todo le sale como novedad."""
    desde = state.setdefault("tiendas_desde", {})
    activas = [t["nombre"] for t in cfg.get("tiendas", []) if t.get("activa") is not False]
    faltan = [n for n in activas if n not in desde]
    if not faltan:
        return
    primera = {}
    for h in (hist.get("productos") or {}).values():
        for tienda, dias in (h.get("t") or {}).items():
            if tienda in faltan and dias:
                d = min(dias)
                if d < primera.get(tienda, "9999"):
                    primera[tienda] = d
    for n in faltan:
        desde[n] = primera.get(n, hoy_chile())


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
               "dock", "ssd", "disco", "reacondicionado", "usado", "usada", "seminuevo"}
# aparatos: "Camara GoPro con microSD 64GB" no es una memoria, pero "Memoria microSD 128GB para
# celular y camara" si. Se descarta solo si el aparato aparece ANTES de la palabra microSD.
APARATOS_MEM = {"camara", "dashcam", "celular", "telefono", "smartphone", "tablet", "gopro", "dron",
                "drone", "notebook", "parlante", "reloj", "smartwatch"}
NOMBRE_MARCA = {"sandisk": "SanDisk", "pny": "PNY", "adata": "ADATA", "kioxia": "KIOXIA",
                "teamgroup": "TeamGroup"}
# por debajo de esto por GB casi siempre es una tarjeta falsa (capacidad trucada)
MIN_CLP_POR_GB = {"normal": 25, "express": 80}


def motivo_no_memoria(titulo, precio=None):
    """Por que un aviso no cuenta como microSD suelta (para el diagnostico). None si cuenta."""
    t = norm(titulo)
    mm = MEM_RE.search(t)
    if not mm:
        return "no dice microSD"
    if set(t.split()) & EXCLUIR_MEM:
        return "accesorio/pack: " + ", ".join(sorted(set(t.split()) & EXCLUIR_MEM))
    if set(t[:mm.start()].split()) & APARATOS_MEM:
        return "es un aparato con memoria"
    if not clasificar_memoria(titulo, precio):
        return "sin capacidad clara o precio imposible por GB"
    return None


def clasificar_memoria(titulo, precio=None, url=None):
    """'Tarjeta microSD Express SanDisk 256GB para Switch 2' ->
    {'tipo': 'express', 'gb': 256, 'marca': 'SanDisk'}. None si no es una microSD suelta.
    La marca se busca tambien en el link (algunas tiendas la dejan fuera del titulo)."""
    t = norm(titulo)
    mm = MEM_RE.search(t)
    if not mm or set(t.split()) & EXCLUIR_MEM or set(t[:mm.start()].split()) & APARATOS_MEM:
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
    if not marca and url:
        u = norm(re.sub(r"[-_/.]+", " ", str(url)))
        marca = next((m for m in MARCAS_MEM if contiene_palabras(u, m)), None)
    return {"tipo": tipo, "gb": gb, "marca": NOMBRE_MARCA.get(marca, marca.title()) if marca else None}


def contiene_palabras(texto_norm, frase):
    return re.search(r"(^| )" + re.escape(frase) + r"( |$)", texto_norm) is not None


def nombre_memoria(m):
    cap = f"{m['gb'] // 1024} TB" if m["gb"] >= 1024 and m["gb"] % 1024 == 0 else \
        f"{m['gb'] / 1024:.1f} TB" if m["gb"] >= 1024 else f"{m['gb']} GB"
    return f"microSD{' Express' if m['tipo'] == 'express' else ''} {cap}" + \
        (f" · {m['marca']}" if m.get("marca") else "")


# busquedas de microSD en MercadoLibre: solo se usan avisos nuevos de vendedores seguros
# (tienda oficial o MercadoLider Gold/Platinum con reputacion verde, lo decide el Worker)
ML_MEMORIAS_BUSCAR = ["microsd express 256gb", "microsd express 512gb", "microsd express sandisk",
                      "microsd express samsung", "microsd express lexar", "microsd sandisk 256gb",
                      "microsd sandisk 512gb", "microsd samsung evo 256gb", "microsd samsung 512gb",
                      "microsd kingston 256gb", "microsd lexar 256gb"]


def leer_memorias_ml(cfg, state):
    """Suma a MEMORIAS_LISTADOS las microSD de vendedores seguros de MercadoLibre y guarda
    quienes son esos vendedores (state['proveedores_ml']) para elegir con quien afiliarse."""
    mem_cfg = cfg.get("memorias") or {}
    if not ml_disponible() or mem_cfg.get("mercadolibre") is False:
        return 0
    hoy = hoy_chile()
    if state.get("memorias_ml_fecha") == hoy:
        return 0                        # una vez al dia basta (los precios de memorias cambian lento)
    prov = state.setdefault("proveedores_ml", {})
    vistos, n = set(), 0
    for q in mem_cfg.get("mercadolibre_buscar") or ML_MEMORIAS_BUSCAR:
        if minutos() > LIMITE_ML_MIN:
            log(f"  memorias ML: corto a los {minutos():.0f} min")
            return n
        try:
            res = ml_buscar(q)
        except (requests.RequestException, ValueError) as e:
            log(f"  memorias ML: fallo '{q}' ({type(e).__name__})")
            break
        for it in res.get("items") or []:
            if it.get("condicion") == "used" or not it.get("seguro") or it.get("moneda") != "CLP" \
                    or not precio_valido(it.get("precio")) or it.get("url") in vistos:
                continue
            m = clasificar_memoria(it.get("titulo") or "", it["precio"], it.get("url"))
            if not m:
                continue
            vistos.add(it.get("url"))
            vend = it.get("vendedor") or "tienda oficial"
            MEMORIAS_LISTADOS.append(("MercadoLibre", {
                "titulo": f"{it['titulo']} ({vend})", "precio": it["precio"], "url": it.get("url"),
                "normal": None}))
            p = prov.setdefault(vend, {"marcas": [], "tipos": []})
            p.update({"nivel": it.get("nivel"), "visto": hoy})
            for campo, valor in (("marcas", m["marca"]), ("tipos", m["tipo"])):
                if valor and valor not in p[campo]:
                    p[campo].append(valor)
            p["avisos"] = p.get("avisos", 0) + 1 if p.get("fecha_conteo") == hoy else 1
            p["fecha_conteo"] = hoy
            n += 1
        time.sleep(0.5)
    state["memorias_ml_fecha"] = hoy
    log(f"  memorias ML: {n} avisos de vendedores seguros")
    return n


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
        m = clasificar_memoria(p["titulo"], p["precio"], p.get("url"))
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


# ------------------------------------------------------------- accesorios
#
# Seguimiento de accesorios (controles, Joy-Con, estuches, bases, micas...) de marcas conocidas.
# Por ahora solo informa a tu chat (informe diario + alerta si hay un remate confirmado) para
# decidir si vale la pena llevarlos al canal.

ACCESORIOS_BUSCAR = ["control switch", "joy-con", "pro controller", "estuche switch",
                     "base carga switch", "mica switch", "volante switch", "audifonos switch"]
ACCESORIOS_LISTADOS = []    # (tienda, producto) de esta corrida, los llena leer_tiendas
TIPOS_ACC = [               # el primero que calza manda
    ("protector", "Mica / protector", r"\b(mica|micas|protector(es)? de pantalla|vidrio templado|screen protector)\b"),
    ("estuche", "Estuche", r"\b(estuche|funda|bolso|carrying case|travel case)\b"),
    ("base", "Base / cargador", r"\b(dock|base de carga|cargador|estacion de carga|charging)\b"),
    ("volante", "Volante / grip", r"\b(volante|wheel|grip)\b"),
    ("audio", "Audífonos", r"\b(audifonos|headset|auriculares)\b"),
    ("joycon", "Joy-Con", r"\bjoy ?cons?\b"),
    ("control", "Control", r"\b(pro controller|controller|control|mando|gamepad|joystick)\b"),
]
MARCAS_ACC = ["nintendo", "powera", "hori", "8bitdo", "pdp", "turtle beach", "razer", "hyperx",
              "logitech", "gulikit", "belkin", "nyko", "snakebyte", "steelseries", "jbl", "otterbox"]
NOMBRE_MARCA_ACC = {"powera": "PowerA", "hori": "HORI", "8bitdo": "8BitDo", "pdp": "PDP",
                    "hyperx": "HyperX", "jbl": "JBL", "gulikit": "GuliKit", "steelseries": "SteelSeries"}
EXCLUIR_ACC = {"consola", "usado", "usada", "reacondicionado", "reacondicionada", "repuesto", "reparacion",
               "flex", "tornillo", "tornillos", "carcasa", "pin", "amiibo", "tarjeta", "microsd", "memoria",
               "juego", "videojuego", "edition", "edicion"}
ACC_PRECIO_MIN = 3000
REMATE_PCT = 35             # descuento confirmado para avisarte al momento


def clasificar_accesorio(titulo, precio=None, url=None):
    """'Control Pro PowerA Nintendo Switch Negro' -> {'tipo': 'control', 'marca': 'PowerA',
    'consola': 'Switch 1'}. None si no es un accesorio de Switch reconocible."""
    t = norm(titulo)
    w = set(t.split())
    if not (w & PLATAFORMA) or w & EXCLUIR_ACC or (precio is not None and precio < ACC_PRECIO_MIN):
        return None
    tipo = next((k for k, _, rx in TIPOS_ACC if re.search(rx, t)), None)
    if not tipo:
        return None
    # "Nintendo Switch" aparece en casi todo (tambien en los genericos): Nintendo cuenta como
    # marca solo si sigue apareciendo despues de sacar el nombre de la consola.
    sin_consola = re.sub(r"\bnintendo (switch|ns)\b", " ", t)
    marca = next((m for m in MARCAS_ACC if contiene_palabras(sin_consola, m)), None)
    if not marca and url:
        u = re.sub(r"\bnintendo (switch|ns)\b", " ", norm(re.sub(r"[-_/.]+", " ", str(url))))
        marca = next((m for m in MARCAS_ACC if contiene_palabras(u, m)), None)
    if not marca and tipo in ("control", "joycon") and re.search(r"\b(pro controller|joy ?cons?)\b", t) \
            and not re.search(r"\b(compatible|generico|alternativo|tipo|inalambrico para)\b", t):
        marca = "nintendo"              # "Pro Controller" y "Joy-Con" a secas son los oficiales
    plat = plataforma_de(titulo)
    return {"tipo": tipo, "marca": NOMBRE_MARCA_ACC.get(marca, marca.title()) if marca else None,
            "consola": "Switch 2" if plat == "Switch 2" else "Switch 1"}


def clave_accesorio(titulo, m):
    """Junta el mismo accesorio de distintas tiendas: tipo + marca + consola + palabras del modelo."""
    t = norm(limpiar_titulo(titulo))
    ruido = RUIDO | STOP | {"para", "compatible", "con", "oficial", "original", "color", "negro", "negra",
                            "blanco", "blanca", "unidad", "nuevo", "2", "lite", "oled",
                            "inalambrico", "inalambrica", "wireless", "bluetooth"}
    palabras_ = sorted({x for x in t.split() if x not in ruido and len(x) > 1})[:6]
    return f"{m['tipo']}|{norm(m['marca'] or 'otra')}|{m['consola']}|{' '.join(palabras_)}"


def revisar_accesorios(cfg, state, hist, alertas):
    conf = cfg.get("accesorios") or {}
    if conf.get("activa", True) is False or not ACCESORIOS_LISTADOS:
        return
    hoy = hoy_chile()
    grupos = {}
    for tienda, p in ACCESORIOS_LISTADOS:
        if p.get("externo"):
            continue
        m = clasificar_accesorio(p["titulo"], p["precio"], p.get("url"))
        if not m:
            continue
        clave = clave_accesorio(p["titulo"], m)
        g = grupos.setdefault(clave, {"m": m, "por_tienda": {}, "nombre": limpiar_titulo(p["titulo"])})
        prev = g["por_tienda"].get(tienda)
        if not prev or p["precio"] < prev["precio"]:
            g["por_tienda"][tienda] = {"tienda": tienda, "precio": p["precio"], "url": p["url"],
                                       "titulo": p["titulo"], "normal": p.get("normal")}
    items = state.setdefault("accesorios", {})
    hist_a = hist.setdefault("accesorios", {})
    avisados = 0
    for clave, g in grupos.items():
        m = g["m"]
        avisos = sorted(g["por_tienda"].values(), key=lambda a: a["precio"])
        mejor = avisos[0]
        precio = mejor["precio"]
        serie = hist_a.setdefault(clave, {})
        est = estadisticas(serie, hoy)
        registrar(serie, hoy, precio, mejor["tienda"])
        otras = [a["precio"] for a in avisos[1:]]
        if est.get("tipico"):
            ref, ref_tipo = est["tipico"], "tipico"
        elif otras and _mediana(otras) <= 3 * precio:
            ref, ref_tipo = _mediana(otras), "mercado"
        else:
            ref, ref_tipo = None, None
        pct = 100 * (1 - precio / ref) if ref else 0
        s = items.setdefault(clave, {})
        anterior = s.get("ultimo")
        s.update({"nombre": g["nombre"], "tipo": m["tipo"], "marca": m["marca"], "consola": m["consola"],
                  "precio": precio, "tienda": mejor["tienda"], "url": mejor["url"], "ref": ref,
                  "ref_tipo": ref_tipo, "pct": round(pct, 1), "normal_tienda": mejor.get("normal"),
                  "min_hist": est.get("min"), "dias": est["dias"], "ultimo": precio, "visto": ahora_iso(),
                  "avisos": [{k: a[k] for k in ("tienda", "precio", "url")} for a in avisos[:4]]})
        if not m["marca"] or not ref or pct < conf.get("remate_pct", REMATE_PCT) or avisados >= 3:
            continue
        if decidir(s, precio, ["descuento"], nuevo=anterior is None and ref_tipo != "mercado"):
            avisados += 1
            agregar_alerta(alertas, "accesorio", "🎮 Accesorios", linea_accesorio(s), puntaje(pct, ["descuento"]),
                           extra="compra")
    # limpia lo que no se ve hace 21 dias
    corte = _sumar_dias(hoy, -21)
    for k in [k for k, s in items.items() if (s.get("visto") or "")[:10] < corte]:
        del items[k]
    log(f"  accesorios: {len(ACCESORIOS_LISTADOS)} avisos, {len(grupos)} modelos, {avisados} remates")


def nombre_tipo_acc(tipo):
    return next((n for k, n, _ in TIPOS_ACC if k == tipo), tipo)


def linea_accesorio(s):
    contra = "su precio típico" if s["ref_tipo"] == "tipico" else "las otras tiendas"
    l = [f"🎮 <b>REMATE · {esc(nombre_tipo_acc(s['tipo']))}</b> · {esc(s.get('marca') or '')} · {s['consola']}",
         f"<a href=\"{esc_attr(s['url'])}\">{esc(s['nombre'])}</a>",
         f"💰 <b>{clp(s['precio'])}</b> en {esc(s['tienda'])} · −{round(s['pct'])}% frente a {contra} ({clp(s['ref'])})"]
    for a in s["avisos"][1:3]:
        l.append(f"   · {esc(a['tienda'])} {clp(a['precio'])}")
    l.append("🧪 En seguimiento: todavía no se publica en el canal")
    return "\n".join(l)


def lineas_accesorios_informe(state):
    """Seccion del informe diario: como va el seguimiento de accesorios."""
    acc = [a for a in (state.get("accesorios") or {}).values()
           if a.get("marca") and (a.get("visto") or "")[:10] >= _sumar_dias(hoy_chile(), -2)]
    if not acc:
        return []
    l = ["", f"🎮 <b>ACCESORIOS</b> (seguimiento · {len(acc)} de marca)"]
    rebajas = sorted((a for a in acc if a.get("ref") and a.get("pct", 0) >= 20), key=lambda a: -a["pct"])[:5]
    for a in rebajas:
        l.append(f"• <a href=\"{esc_attr(a['url'])}\">{esc(corto_txt(a['nombre'], 40))}</a> "
                 f"({esc(a.get('marca') or '')}, {a['consola']}) — {clp(a['precio'])} en {esc(a['tienda'])} · −{round(a['pct'])}%")
    if not rebajas:
        dias = max((a.get("dias") or 0) for a in acc)
        l.append(f"Sin rebajas confirmadas todavía · historial de {dias} día(s)")
    por_tipo = {}
    for a in acc:
        por_tipo[a["tipo"]] = por_tipo.get(a["tipo"], 0) + 1
    l.append("Siguiendo: " + " · ".join(f"{nombre_tipo_acc(k)} {n}" for k, n in sorted(por_tipo.items(), key=lambda kv: -kv[1])))
    return l


def corto_txt(t, n):
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


# --------------------------------------------------------------- consolas
#
# Seguimiento de consolas Nintendo (Switch 2, OLED, Switch, Lite; solas o con juego). Solo
# informa a tu chat: informe diario y alerta si una baja de verdad. Pensado para Black Friday.

CONSOLAS_BUSCAR = ["consola nintendo switch", "nintendo switch 2", "switch oled", "switch lite"]
CONSOLAS_LISTADOS = []
CONSOLA_PRECIO_MIN = 120000
EXCLUIR_CONSOLA = {"estuche", "funda", "mica", "micas", "vidrio", "protector", "carcasa", "skin", "sticker",
                   "repuesto", "reparacion", "usado", "usada", "reacondicionado", "reacondicionada",
                   "seminuevo", "seminueva", "caja", "soporte", "bolso", "cargador", "cable", "flex"}
CONSOLA_BAJA_PCT = 8        # descuento confirmado (o baja desde la revision anterior) para avisarte


def clasificar_consola(titulo, precio=None, url=None):
    """'Consola Nintendo Switch 2 + Mario Kart World' -> {'modelo': 'Switch 2', 'bundle': True}."""
    t = norm(titulo)
    w = set(t.split())
    if w & EXCLUIR_CONSOLA or (precio is not None and precio < CONSOLA_PRECIO_MIN):
        return None
    if not (w & PLATAFORMA):
        return None
    es_consola = "consola" in w or (not (w & {"juego", "videojuego"}) and
                                    re.search(r"^(nintendo )?switch( 2| oled| lite)?\b", t))
    if not es_consola:
        return None
    if PLAT2_RE.search(t):
        modelo = "Switch 2"
    elif "oled" in w:
        modelo = "Switch OLED"
    elif "lite" in w:
        modelo = "Switch Lite"
    else:
        modelo = "Switch"
    bundle = "+" in str(titulo) or bool(re.search(r"\b(bundle|pack|incluye|juego|edicion|edition)\b", t))
    return {"modelo": modelo, "bundle": bundle}


def clave_consola(titulo, m):
    t = norm(limpiar_titulo(titulo))
    ruido = RUIDO | STOP | {"consola", "oled", "lite", "2", "con", "incluye", "bundle", "pack", "color",
                            "edicion", "edition", "modelo", "version", "nueva", "nuevo", "set", "y"}
    extra = sorted({x for x in t.split() if x not in ruido and len(x) > 1})[:6] if m["bundle"] else []
    return f"{m['modelo']}|{'bundle' if m['bundle'] else 'sola'}|{' '.join(extra)}"


def revisar_consolas(cfg, state, hist, alertas):
    conf = cfg.get("consolas") or {}
    if conf.get("activa", True) is False or not CONSOLAS_LISTADOS:
        return
    hoy = hoy_chile()
    grupos = {}
    for tienda, p in CONSOLAS_LISTADOS:
        if p.get("externo"):
            continue
        m = clasificar_consola(p["titulo"], p["precio"], p.get("url"))
        if not m:
            continue
        clave = clave_consola(p["titulo"], m)
        g = grupos.setdefault(clave, {"m": m, "por_tienda": {}, "nombre": re.sub(r"\s+", " ", p["titulo"]).strip()[:80]})
        prev = g["por_tienda"].get(tienda)
        if not prev or p["precio"] < prev["precio"]:
            g["por_tienda"][tienda] = {"tienda": tienda, "precio": p["precio"], "url": p["url"],
                                       "titulo": p["titulo"], "normal": p.get("normal")}
    items = state.setdefault("consolas", {})
    hist_c = hist.setdefault("consolas", {})
    avisados = 0
    umbral = conf.get("baja_pct", CONSOLA_BAJA_PCT)
    for clave, g in grupos.items():
        m = g["m"]
        avisos = sorted(g["por_tienda"].values(), key=lambda a: a["precio"])
        mejor = avisos[0]
        precio = mejor["precio"]
        serie = hist_c.setdefault(clave, {})
        est = estadisticas(serie, hoy)
        registrar(serie, hoy, precio, mejor["tienda"])
        otras = [a["precio"] for a in avisos[1:] if a["precio"] <= 1.6 * precio]
        if est.get("tipico"):
            ref, ref_tipo = est["tipico"], "tipico"
        elif otras:
            ref, ref_tipo = _mediana(otras), "mercado"
        else:
            ref, ref_tipo = None, None
        pct = 100 * (1 - precio / ref) if ref else 0
        s = items.setdefault(clave, {})
        anterior = s.get("ultimo")
        s.update({"nombre": g["nombre"], "modelo": m["modelo"], "bundle": m["bundle"], "precio": precio,
                  "tienda": mejor["tienda"], "url": mejor["url"], "ref": ref, "ref_tipo": ref_tipo,
                  "pct": round(pct, 1), "min_hist": est.get("min"), "dias": est["dias"], "ultimo": precio,
                  "visto": ahora_iso(), "avisos": [{k: a[k] for k in ("tienda", "precio", "url")} for a in avisos[:4]]})
        motivos = []
        if ref and pct >= umbral:
            motivos.append("descuento")
        if anterior and 100 * (1 - precio / anterior) >= umbral:
            motivos.append("baja")
        if motivos and avisados < 3 and decidir(s, precio, motivos, nuevo=anterior is None and ref_tipo != "mercado"):
            avisados += 1
            agregar_alerta(alertas, "consola", "🕹️ Consolas", linea_consola(s, motivos, anterior),
                           puntaje(max(pct, 10), motivos), extra="compra")
    corte = _sumar_dias(hoy, -21)
    for k in [k for k, s in items.items() if (s.get("visto") or "")[:10] < corte]:
        del items[k]
    log(f"  consolas: {len(CONSOLAS_LISTADOS)} avisos, {len(grupos)} versiones, {avisados} alertas")


def linea_consola(s, motivos, anterior):
    l = [f"🕹️ <b>CONSOLA · {s['modelo']}</b>" + (" con juego" if s["bundle"] else ""),
         f"<a href=\"{esc_attr(s['url'])}\">{esc(s['nombre'])}</a>",
         f"💰 <b>{clp(s['precio'])}</b> en {esc(s['tienda'])}"
         + (f" · −{round(s['pct'])}% frente a {'su precio típico' if s['ref_tipo'] == 'tipico' else 'las otras tiendas'}"
            f" ({clp(s['ref'])})" if "descuento" in motivos else "")]
    if "baja" in motivos and anterior:
        l.append(f"📉 Bajó desde {clp(anterior)}")
    for a in s["avisos"][1:3]:
        l.append(f"   · {esc(a['tienda'])} {clp(a['precio'])}")
    l.append("🧪 En seguimiento: todavía no se publica en el canal")
    return "\n".join(l)


def lineas_consolas_informe(state):
    cons = [c for c in (state.get("consolas") or {}).values()
            if (c.get("visto") or "")[:10] >= _sumar_dias(hoy_chile(), -2)]
    if not cons:
        return []
    l = ["", "🕹️ <b>CONSOLAS</b> (la más barata de cada versión)"]
    for modelo in ("Switch 2", "Switch OLED", "Switch", "Switch Lite"):
        for bundle in (False, True):
            xs = sorted((c for c in cons if c["modelo"] == modelo and c["bundle"] == bundle), key=lambda c: c["precio"])
            if not xs:
                continue
            c = xs[0]
            otra = next((a for a in c["avisos"][1:]), None)
            l.append(f"• {modelo}{' con juego' if bundle else ''}: <a href=\"{esc_attr(c['url'])}\">{clp(c['precio'])}</a>"
                     f" en {esc(c['tienda'])}" + (f" · −{round(c['pct'])}%" if c.get("pct", 0) >= 3 else "")
                     + (f" (sigue {esc(otra['tienda'])} {clp(otra['precio'])})" if otra else ""))
    dias = max((c.get("dias") or 0) for c in cons)
    l.append(f"Historial: {dias} día(s) · {len(cons)} versiones en seguimiento")
    return l


# ------------------------------------------------------------ audifonos gamer
#
# Audifonos gamer de marcas conocidas (todas las plataformas). Solo para tu chat: informe
# diario y alerta si uno baja de verdad.

AUDIFONOS_BUSCAR = ["audifonos gamer", "headset gamer", "audifonos gamer inalambricos"]
AUDIFONOS_ML = ["audifonos gamer hyperx", "audifonos gamer logitech g", "audifonos gamer razer",
                "audifonos gamer steelseries", "audifonos gamer corsair", "audifonos redragon zeus"]
AUDIFONOS_LISTADOS = []
MARCAS_AUD = {"hyperx": "HyperX", "logitech": "Logitech G", "razer": "Razer", "steelseries": "SteelSeries",
              "corsair": "Corsair", "turtle beach": "Turtle Beach", "astro": "Astro", "redragon": "Redragon",
              "jbl": "JBL", "sony": "Sony", "asus": "ASUS ROG", "cooler master": "Cooler Master",
              "audio technica": "Audio-Technica", "epos": "EPOS", "trust": "Trust"}
EXCLUIR_AUD = {"almohadillas", "almohadilla", "repuesto", "cable", "soporte", "adaptador", "usado", "usada",
               "reacondicionado", "reacondicionada", "funda", "estuche", "stand", "atril", "mouse", "teclado",
               "combo", "kit", "parlante", "parlantes"}
AUD_RE = re.compile(r"\b(audifonos?|headsets?|auriculares?|headphones?|cascos?)\b")
AUD_PRECIO_MIN = 12000
AUD_BAJA_PCT = 25


def clasificar_audifono(titulo, precio=None, url=None):
    """'Audifonos Gamer HyperX Cloud II 7.1' -> {'marca': 'HyperX', 'inalambrico': False}."""
    t = norm(titulo)
    w = set(t.split())
    if not AUD_RE.search(t) or w & EXCLUIR_AUD or (precio is not None and precio < AUD_PRECIO_MIN):
        return None
    marca = next((m for m in MARCAS_AUD if contiene_palabras(t, m)), None)
    if not marca and url:
        u = norm(re.sub(r"[-_/.]+", " ", str(url)))
        marca = next((m for m in MARCAS_AUD if contiene_palabras(u, m)), None)
    if not marca:
        return None                      # sin marca conocida no se sigue (genericos)
    if marca == "jbl" and "quantum" not in w:
        return None                      # JBL: solo la linea gamer
    if marca == "sony" and "inzone" not in w:
        return None
    if marca == "asus" and "rog" not in w:
        return None
    return {"marca": MARCAS_AUD[marca],
            "inalambrico": bool(w & {"inalambrico", "inalambricos", "wireless", "bluetooth", "lightspeed"})}


def clave_audifono(titulo, m):
    t = norm(titulo)
    ruido = STOP | {"audifonos", "audifono", "headset", "headsets", "auriculares", "auricular", "gamer",
                    "gaming", "para", "pc", "ps4", "ps5", "xbox", "switch", "nintendo", "con", "microfono",
                    "inalambrico", "inalambricos", "wireless", "negro", "negra", "blanco", "blanca", "color",
                    "rojo", "azul", "rosado", "nuevo", "original", "sonido", "envolvente", "surround", "y"}
    ruido |= set(norm(m["marca"]).split()) | set(MARCAS_AUD)
    modelo = [x for x in t.split() if x not in ruido and len(x) > 1][:4]
    return f"{norm(m['marca'])}|{'inal' if m['inalambrico'] else 'cable'}|{' '.join(sorted(modelo))}"


# ------------------------------------------------------------ tus seguimientos
#
# Productos que tu sigues para ti (juegos.json -> seguimientos), ej. los Redragon Zeus.
# Se buscan en cada revision, en las tiendas y en MercadoLibre (solo vendedores seguros).
# Nunca van al canal.

SEGUIMIENTOS_CFG = []
SEGUIMIENTOS_LISTADOS = []
PALABRAS_VARIANTE = {"x", "pro", "2", "3", "elite", "max", "plus", "lite", "mini", "wireless",
                     "inalambrico", "inalambricos"}


def item_seguimiento(titulo):
    """El seguimiento de juegos.json al que corresponde el titulo (o None)."""
    for x in SEGUIMIENTOS_CFG:
        if coincide_simple(titulo, x):
            return x
    return None


def coincide_simple(titulo, x):
    w = palabras(titulo)
    if w & {norm(e) for e in x.get("excluir") or []}:
        return False
    return any(palabras(t) <= w for t in terminos(x))


def clasificar_seguimiento(titulo, precio=None, url=None):
    if precio is not None and precio < 3000:
        return None
    if USADO_RE.search(norm(titulo)):
        return None
    x = item_seguimiento(titulo)
    return {"item": x["nombre"]} if x else None


def variante_seguimiento(titulo, x):
    w = norm(titulo).split()
    base = palabras(terminos(x)[0])     # el primer termino es el nombre base (ej. "redragon zeus")
    v = [p for p in w if p in PALABRAS_VARIANTE and p not in base]
    if "inalambrico" in v or "inalambricos" in v:
        v = [p for p in v if p not in ("inalambrico", "inalambricos")] + ["wireless"]
    return " ".join(sorted(set(v))) or "base"


def leer_ml_extra(state, cat, consultas, lista, clasificador):
    """Suma a la lista los avisos nuevos de vendedores seguros de MercadoLibre."""
    if not ml_disponible() or not consultas:
        return 0
    n, vistos = 0, set()
    for q in consultas:
        if minutos() > LIMITE_ML_MIN:
            break
        try:
            res = ml_buscar(q)
        except (requests.RequestException, ValueError):
            break
        for it in res.get("items") or []:
            if it.get("condicion") == "used" or not it.get("seguro") or it.get("moneda") != "CLP" \
                    or not precio_valido(it.get("precio")) or it.get("url") in vistos:
                continue
            titulo = it.get("titulo") or ""
            if not clasificador(titulo, it["precio"], it.get("url")):
                continue
            vistos.add(it.get("url"))
            vend = it.get("vendedor") or "tienda oficial"
            lista.append(("MercadoLibre", {"titulo": f"{titulo} ({vend})", "precio": it["precio"],
                                           "url": it.get("url"), "normal": None}))
            n += 1
        time.sleep(0.5)
    if n:
        log(f"  {cat} ML: {n} avisos de vendedores seguros")
    return n


def _agrupar_simple(lista, clave_fn):
    grupos = {}
    for tienda, p in lista:
        if p.get("externo"):
            continue
        r = clave_fn(p)
        if not r:
            continue
        clave, nombre = r
        g = grupos.setdefault(clave, {"nombre": nombre, "por_tienda": {}})
        prev = g["por_tienda"].get(tienda)
        if not prev or p["precio"] < prev["precio"]:
            g["por_tienda"][tienda] = {"tienda": tienda, "precio": p["precio"], "url": p["url"], "titulo": p["titulo"]}
    return grupos


def revisar_audifonos(cfg, state, hist, alertas):
    conf = cfg.get("audifonos") or {}
    if conf.get("activa", True) is False:
        return
    if AUDIFONOS_LISTADOS or state.get("audifonos_ml_fecha") != hoy_chile():
        if leer_ml_extra(state, "audifonos", conf.get("mercadolibre_buscar", AUDIFONOS_ML),
                         AUDIFONOS_LISTADOS, clasificar_audifono) is not None:
            state["audifonos_ml_fecha"] = hoy_chile()
    if not AUDIFONOS_LISTADOS:
        return
    def clave(p):
        m = clasificar_audifono(p["titulo"], p["precio"], p.get("url"))
        return (clave_audifono(p["titulo"], m), re.sub(r"\s+", " ", p["titulo"]).strip()[:70]) if m else None
    grupos = _agrupar_simple(AUDIFONOS_LISTADOS, clave)
    items = state.setdefault("audifonos", {})
    hist_a = hist.setdefault("audifonos", {})
    hoy, avisados = hoy_chile(), 0
    for k, g in grupos.items():
        avisos = sorted(g["por_tienda"].values(), key=lambda a: a["precio"])
        precio, mejor = avisos[0]["precio"], avisos[0]
        serie = hist_a.setdefault(k, {})
        est = estadisticas(serie, hoy)
        registrar(serie, hoy, precio, mejor["tienda"])
        otras = [a["precio"] for a in avisos[1:] if a["precio"] <= 2 * precio]
        ref, ref_tipo = (est["tipico"], "tipico") if est.get("tipico") else \
            ((_mediana(otras), "mercado") if otras else (None, None))
        pct = 100 * (1 - precio / ref) if ref else 0
        s = items.setdefault(k, {})
        anterior = s.get("ultimo")
        s.update({"nombre": g["nombre"], "marca": k.split("|")[0], "precio": precio, "tienda": mejor["tienda"],
                  "url": mejor["url"], "ref": ref, "ref_tipo": ref_tipo, "pct": round(pct, 1),
                  "min_hist": est.get("min"), "dias": est["dias"], "ultimo": precio, "visto": ahora_iso(),
                  "avisos": [{x: a[x] for x in ("tienda", "precio", "url")} for a in avisos[:4]]})
        if ref and pct >= conf.get("baja_pct", AUD_BAJA_PCT) and avisados < 3 and \
                decidir(s, precio, ["descuento"], nuevo=anterior is None and ref_tipo != "mercado"):
            avisados += 1
            otras_txt = "\n".join(f"   · {esc(a['tienda'])} {clp(a['precio'])}" for a in s["avisos"][1:3])
            agregar_alerta(alertas, "consola", "🎧 Audífonos", (
                f"🎧 <b>AUDÍFONOS GAMER</b>\n<a href=\"{esc_attr(s['url'])}\">{esc(s['nombre'])}</a>\n"
                f"💰 <b>{clp(precio)}</b> en {esc(s['tienda'])} · −{round(pct)}% frente a "
                f"{'su precio típico' if ref_tipo == 'tipico' else 'las otras tiendas'} ({clp(ref)})"
                + (f"\n{otras_txt}" if otras_txt else "")), puntaje(pct, ["descuento"]), extra="compra")
    corte = _sumar_dias(hoy, -21)
    for k in [k for k, s in items.items() if (s.get("visto") or "")[:10] < corte]:
        del items[k]
    log(f"  audífonos: {len(AUDIFONOS_LISTADOS)} avisos, {len(grupos)} modelos, {avisados} alertas")


def lineas_audifonos_informe(state):
    aud = [a for a in (state.get("audifonos") or {}).values()
           if (a.get("visto") or "")[:10] >= _sumar_dias(hoy_chile(), -2)]
    if not aud:
        return []
    l = ["", f"🎧 <b>AUDÍFONOS GAMER</b> ({len(aud)} modelos de marca)"]
    rebajas = sorted((a for a in aud if a.get("ref") and a.get("pct", 0) >= 15), key=lambda a: -a["pct"])[:5]
    for a in rebajas:
        l.append(f"• <a href=\"{esc_attr(a['url'])}\">{esc(corto_txt(a['nombre'], 42))}</a> — "
                 f"{clp(a['precio'])} en {esc(a['tienda'])} · −{round(a['pct'])}%")
    if not rebajas:
        l.append(f"Sin rebajas confirmadas todavía · historial de {max((a.get('dias') or 0) for a in aud)} día(s)")
    return l


def revisar_seguimientos(cfg, state, hist, alertas):
    if not SEGUIMIENTOS_CFG:
        return
    consultas = [t for x in SEGUIMIENTOS_CFG if x.get("mercadolibre", True) is not False for t in terminos(x)]
    leer_ml_extra(state, "seguimientos", consultas, SEGUIMIENTOS_LISTADOS, clasificar_seguimiento)
    def clave(p):
        x = item_seguimiento(p["titulo"])
        if not x or not clasificar_seguimiento(p["titulo"], p["precio"]):
            return None
        return f"{x['nombre']}|{variante_seguimiento(p['titulo'], x)}", re.sub(r"\s+", " ", p["titulo"]).strip()[:70]
    grupos = _agrupar_simple(SEGUIMIENTOS_LISTADOS, clave)
    items = state.setdefault("seguimientos", {})
    hist_s = hist.setdefault("seguimientos", {})
    hoy = hoy_chile()
    objetivos = {x["nombre"]: x.get("objetivo_clp") for x in SEGUIMIENTOS_CFG}
    for k, g in grupos.items():
        avisos = sorted(g["por_tienda"].values(), key=lambda a: a["precio"])
        precio, mejor = avisos[0]["precio"], avisos[0]
        serie = hist_s.setdefault(k, {})
        est = estadisticas(serie, hoy)
        registrar(serie, hoy, precio, mejor["tienda"])
        s = items.setdefault(k, {})
        anterior = s.get("ultimo")
        item, variante = k.split("|", 1)
        s.update({"item": item, "variante": variante, "nombre": g["nombre"], "precio": precio,
                  "tienda": mejor["tienda"], "url": mejor["url"], "min_hist": est.get("min"),
                  "min_fecha": est.get("min_fecha"), "dias": est["dias"], "ultimo": precio, "visto": ahora_iso(),
                  "avisos": [{x: a[x] for x in ("tienda", "precio", "url")} for a in avisos[:4]]})
        motivos = []
        obj = objetivos.get(item)
        if obj and precio <= obj:
            motivos.append("objetivo")
        if anterior and precio < anterior * 0.97:
            motivos.append("baja")
        if est.get("min") and est["dias"] >= 2 and precio < est["min"] * 0.99:
            motivos.append("minimo")
        if motivos and decidir(s, precio, motivos, nuevo=False):
            l = [f"👀 <b>TU SEGUIMIENTO · {esc(item)}</b>" + (f" · {esc(nombre_variante(variante))}" if variante != "base" else ""),
                 f"<a href=\"{esc_attr(s['url'])}\">{esc(s['nombre'])}</a>",
                 f"💰 <b>{clp(precio)}</b> en {esc(s['tienda'])}"]
            if "objetivo" in motivos:
                l.append(f"🎯 Llegó a tu objetivo ({clp(obj)})")
            if "minimo" in motivos:
                l.append("🏆 El precio más bajo que he visto")
            elif "baja" in motivos and anterior:
                l.append(f"📉 Bajó desde {clp(anterior)}")
            for a in s["avisos"][1:3]:
                l.append(f"   · {esc(a['tienda'])} {clp(a['precio'])}")
            agregar_alerta(alertas, "consola", "👀 Seguimientos", "\n".join(l), 90, extra="compra")
    log(f"  seguimientos: {len(SEGUIMIENTOS_LISTADOS)} avisos, {len(grupos)} versiones")


def nombre_variante(v):
    if v == "base":
        return "modelo base"
    return " ".join(w.upper() if len(w) == 1 else ("inalámbrico" if w == "wireless" else w.capitalize())
                    for w in v.split())


def lineas_seguimientos_informe(state):
    segs = [s for s in (state.get("seguimientos") or {}).values()
            if (s.get("visto") or "")[:10] >= _sumar_dias(hoy_chile(), -2)]
    if not segs:
        return []
    l = ["", "👀 <b>TUS SEGUIMIENTOS</b>"]
    for item in sorted({s["item"] for s in segs}):
        l.append(f"<b>{esc(item)}</b>")
        for s in sorted((x for x in segs if x["item"] == item), key=lambda x: x["precio"])[:5]:
            otra = s["avisos"][1] if len(s["avisos"]) > 1 else None
            l.append(f"• {esc(nombre_variante(s['variante']))}: "
                     f"<a href=\"{esc_attr(s['url'])}\">{clp(s['precio'])}</a> en {esc(s['tienda'])}"
                     + (f" (sigue {esc(otra['tienda'])} {clp(otra['precio'])})" if otra else "")
                     + (f" · mín {clp(s['min_hist'])}" if s.get("min_hist") and s["min_hist"] < s["precio"] else ""))
    return l


# categorias extra que se buscan una vez al dia en todas las tiendas
EXTRAS = {"accesorios": (ACCESORIOS_LISTADOS, ACCESORIOS_BUSCAR),
          "consolas": (CONSOLAS_LISTADOS, CONSOLAS_BUSCAR),
          "audifonos": (AUDIFONOS_LISTADOS, AUDIFONOS_BUSCAR),
          "seguimientos": (SEGUIMIENTOS_LISTADOS, [])}
CLASIFICADORES = {"accesorios": clasificar_accesorio, "consolas": clasificar_consola,
                  "audifonos": clasificar_audifono, "seguimientos": clasificar_seguimiento}


# ----------------------------------------------------------- mercadolibre

ML_POR_CORRIDA = 40         # consultas a MercadoLibre por revision (rota entre los juegos)
ML_DIAS_VIGENCIA = 3        # cada juego se vuelve a consultar cada 3 dias


def ml_disponible():
    return bool(os.environ.get("WORKER_URL") and os.environ.get("PROXY_KEY"))


def ml_buscar(q):
    r = requests.get(os.environ["WORKER_URL"].rstrip("/") + "/ml/buscar", params={"q": q},
                     headers={"X-Proxy-Key": os.environ["PROXY_KEY"]}, timeout=TIMEOUT + 15)
    return r.json()


def ml_calza(titulo, nombre, plataforma):
    """El aviso de MercadoLibre es el mismo juego (y la misma consola) y no un accesorio."""
    w = palabras(titulo)
    if w & EXCLUIR_SIEMPRE:
        return False
    if (plataforma_de(titulo) == "Switch 2") != (plataforma == "Switch 2"):
        return False
    t_nombre = set(tokens_de(nombre))
    t_aviso = set(tokens_de(titulo))
    if not t_nombre:
        return False
    # los vendedores abrevian ("Zelda Tears of the Kingdom"): basta el 75 % de las palabras,
    # pero el subtitulo (lo que va despues de ":") tiene que estar completo
    if len(t_nombre) <= 2 and not t_nombre <= t_aviso:
        return False
    if len(t_nombre & t_aviso) < 0.75 * len(t_nombre):
        return False
    if ":" in nombre and not set(tokens_de(nombre.split(":", 1)[1])) <= t_aviso:
        return False
    extra = (w & VARIANTES) - palabras(nombre) - {"usado"}
    return not extra                 # coleccionista, steelbook, bundles: otro producto


def revisar_mercadolibre(cfg, state, hoy):
    """Precio de reventa: a cuanto se vende cada fisico nuevo y usado en MercadoLibre Chile."""
    if not ml_disponible() or (cfg.get("mercadolibre") or {}).get("activa") is False:
        return
    ml = state.setdefault("mercadolibre", {})
    corte_visto = _sumar_dias(hoy, -1)
    vence = _sumar_dias(hoy, -ML_DIAS_VIGENCIA)
    cands = []
    for clave, s in (state.get("productos") or {}).items():
        if (s.get("visto") or "")[:10] < corte_visto or s.get("externo") or s.get("variante"):
            continue
        if USADO_RE.search(norm(s.get("nombre") or "")):
            continue
        previo = ml.get(clave) or {}
        if (previo.get("fecha") or "") > vence:
            continue
        de_tu_lista = norm(re.sub(r"^\W+", "", s.get("juego") or "")) != "otros juegos"
        prioridad = (0 if de_tu_lista else 1, previo.get("fecha") or "")
        cands.append((prioridad, clave, s))
    cands.sort(key=lambda x: x[0])
    hechos = errores = 0
    for _, clave, s in cands[:(cfg.get("mercadolibre") or {}).get("por_corrida", ML_POR_CORRIDA)]:
        if minutos() > LIMITE_ML_MIN:
            log(f"  mercadolibre: corto a los {minutos():.0f} min para no pasarme del tiempo")
            break
        q = f"{s['nombre']} {'switch 2' if s.get('plataforma') == 'Switch 2' else 'nintendo switch'}"
        try:
            res = ml_buscar(q)
            calzan = [it for it in res.get("items") or []
                      if ml_calza(it.get("titulo") or "", s["nombre"], s.get("plataforma"))]
            if res.get("ok") and not calzan:
                res = ml_buscar(s["nombre"])     # el catalogo a veces prioriza otra version: sin la consola
        except (requests.RequestException, ValueError) as e:
            log(f"  mercadolibre: fallo la consulta ({type(e).__name__}); sigo en la proxima revision")
            break
        if not res.get("ok"):
            errores += 1
            log(f"  mercadolibre: {res.get('error')} ({res.get('status')})")
            if errores >= 2:
                break
            continue
        nuevos, usados = [], []
        for it in res.get("items") or []:
            if it.get("moneda") != "CLP" or not precio_valido(it.get("precio")):
                continue
            if not ml_calza(it.get("titulo") or "", s["nombre"], s.get("plataforma")):
                continue
            (usados if it.get("condicion") == "used" else nuevos).append(it)
        def med(xs):
            return _mediana([x["precio"] for x in xs]) if len(xs) >= 2 else None
        seguros = sorted((x for x in nuevos if x.get("seguro")), key=lambda x: x["precio"])
        seguro = ({"precio": seguros[0]["precio"], "url": seguros[0].get("url"),
                   "vendedor": seguros[0].get("vendedor"), "nivel": seguros[0].get("nivel")}
                  if seguros else None)
        ml[clave] = {"fecha": hoy, "nombre": s["nombre"], "seguro": seguro,
                     "nuevo": med(nuevos), "n_nuevo": len(nuevos),
                     "nuevo_min": min((x["precio"] for x in nuevos), default=None),
                     "usado": med(usados), "n_usado": len(usados),
                     "vendidos": sum(x.get("vendidos") or 0 for x in nuevos + usados) or None,
                     "busqueda": "https://listado.mercadolibre.cl/" + norm(q).replace(" ", "-")}
        hechos += 1
        time.sleep(0.5)
    if hechos:
        log(f"  mercadolibre: {hechos} juegos consultados")


def lineas_mercadolibre(m, precio):
    if not m or not (m.get("nuevo") or m.get("usado")):
        return []
    partes = []
    if m.get("nuevo"):
        partes.append(f"nuevo ~{clp(m['nuevo'])}")
    if m.get("usado"):
        partes.append(f"usado ~{clp(m['usado'])}")
    l = [f"🛒 <a href=\"{esc_attr(m['busqueda'])}\">MercadoLibre</a>: " + " · ".join(partes)
         + f" ({m['n_nuevo'] + m['n_usado']} avisos)"]
    ref = m.get("nuevo")
    if ref and ref >= precio * 1.12:
        l.append(f"💰 <b>Reventa</b>: lo compras a {clp(precio)} y nuevo se vende a ~{clp(ref)} "
                 f"(+{clp(ref - precio)} antes de comisiones)")
    return l


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


def ids_contundentes(state, cfg):
    """ids de juegos que pasan el filtro de calidad (destacados o de tu lista). None si no se pudo."""
    if cfg.get("ajustes", {}).get("solo_contundentes", True) is False:
        return None
    try:
        import canal
        ccfg = load_json(canal.CANAL_FILE, None)
        if not ccfg:
            return None
        return {o["id"] for o in canal.candidatos(ccfg, state)
                if o.get("saga") or o.get("nivel") in ("destacado", "bombazo")}
    except Exception as e:  # noqa: BLE001
        log(f"  filtro de calidad: error {type(e).__name__}: {e}")
        return None


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
        if a["tipo"] == "fisico" and a.get("extra") in ("📦 se está agotando", "📅 preventa abierta"):
            out.append(a)               # dato de negocio: siempre
            continue
        if a["tipo"] == "digital" and cfg.get("ajustes", {}).get("alertas_digitales", "seguidos") != "todas":
            fuera += 1                  # los digitales para disfrutar los publica el canal
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
    if "escasez" in motivos:
        return "📦 se está agotando"
    if "preventa" in motivos:
        return "📅 preventa abierta"
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
    l += lineas_evento(clave, s, hist)
    l += lineas_negocio((hist.get("productos") or {}).get(clave) or {}, s)
    l += lineas_mercadolibre((state.get("mercadolibre") or {}).get(clave), s["precio"])
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
        nivel = a["extra"] if a["tipo"] in ("memoria", "accesorio", "consola") and a.get("extra") in NIVELES else "bueno"
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

    # /ofertas y /precios muestran solo lo que vale la pena (el resto queda guardado igual)
    buenos = ids_contundentes(state, cfg)

    def vale(x, tipo):
        if buenos is None or x.get("seguido") or x.get("objetivo") is not None or x.get("id") in buenos:
            return True
        de_tu_lista = norm(re.sub(r"^\W+", "", x.get("juego") or "")) != "otros juegos"
        return tipo == "fisico" and de_tu_lista and not USADO_RE.search(norm(x.get("nombre") or ""))

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
        if not vale(s, "fisico"):
            continue
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
        if not vale(e, "digital"):
            continue
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
                                           "evento": evento_activo(cfg) if cfg else None,
                                           "proximo": proximo_evento(cfg) if cfg else None,
                                           "pendientes": len(state.get("pendientes") or []),
                                           "fisicos": sum(1 for x in ofertas), "total": sum(len(v) for v in sagas.values())})
    save_json(VISTAS_DIR / "ids.json", ids)
    log(f"  vistas: {len(ofertas)} ofertas, {len(indice)} sagas")


# ------------------------------------------------------------------- agenda
#
# El workflow corre cada hora. El bot decide en cada pasada:
#   - si toca revisar precios (cada 6 h, o cada 1 h en modo evento)
#   - si toca mandar el resumen diario (a la hora que elijas)
# Lo urgente (🎯 objetivo, 🟢 compra ya, ⭐ seguidos, 🏆 minimos) llega al tiro;
# lo demas espera al resumen. En modo evento llega todo al tiro.

EVENTO = None          # evento activo en esta pasada

EVENTOS_BASE = [
    {"nombre": "Black Friday eShop", "desde": "2026-11-19", "hasta": "2026-12-03",
     "nota": "fechas estimadas según 2025"},
    {"nombre": "Black Friday Chile", "desde": "2026-11-27", "hasta": "2026-11-30"},
    {"nombre": "Ofertas de fin de año eShop", "desde": "2026-12-17", "hasta": "2027-01-06",
     "nota": "fechas estimadas"},
]


def ahora_chile():
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Santiago"))
    except Exception:
        from datetime import datetime, timedelta
        return datetime.utcnow() - timedelta(hours=3)


def _dias_entre(a, b):
    from datetime import date
    y1, m1, d1 = (int(x) for x in a[:10].split("-"))
    y2, m2, d2 = (int(x) for x in b[:10].split("-"))
    return (date(y2, m2, d2) - date(y1, m1, d1)).days


def _sumar_dias(ymd, n):
    from datetime import date, timedelta
    y, m, d = (int(x) for x in ymd[:10].split("-"))
    return (date(y, m, d) + timedelta(days=n)).isoformat()


def lista_eventos(cfg):
    aj = cfg.get("ajustes", {})
    evs = [e for e in EVENTOS_BASE if e["nombre"] not in (aj.get("eventos_desactivados") or [])]
    evs += aj.get("eventos") or []
    return sorted(evs, key=lambda e: e["desde"])


def evento_activo(cfg):
    aj = cfg.get("ajustes", {})
    man = aj.get("evento_manual")
    if man and man.get("hasta") and man["hasta"] > ahora_iso():
        return {"nombre": man.get("nombre") or "Modo evento manual", "desde": man.get("desde", hoy_chile()),
                "hasta": man["hasta"][:10], "manual": True}
    hoy = hoy_chile()
    for e in lista_eventos(cfg):
        if e["desde"] <= hoy <= e["hasta"]:
            return e
    return None


def proximo_evento(cfg):
    hoy = hoy_chile()
    futuros = [e for e in lista_eventos(cfg) if e["desde"] > hoy]
    return futuros[0] if futuros else None


def horas_desde(iso):
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600
    except (ValueError, AttributeError):
        return 999


# ---------------------------------------------------------------- pre-evento

def lineas_evento(clave, s, hist):
    """Durante un evento: compara contra el precio de las semanas anteriores y
    detecta tiendas que subieron el precio justo antes para inflar el descuento."""
    if not EVENTO or EVENTO.get("manual"):
        return []
    h = (hist.get("productos") or {}).get(clave) or {}
    desde = EVENTO["desde"]
    base_ini, base_fin = _sumar_dias(desde, -21), _sumar_dias(desde, -7)
    serie = {d: (v[0] if isinstance(v, list) else v) for d, v in (h.get("f") or {}).items()}
    antes = [v for d, v in serie.items() if base_ini <= d <= base_fin]
    l = []
    if antes:
        ref = _mediana(antes)
        dif = 100 * (1 - s["precio"] / ref)
        if dif >= 1:
            l.append(f"📅 Antes de {esc(EVENTO['nombre'])} estaba a {clp(ref)}: hoy está {round(dif)}% más barato de verdad")
        elif dif <= -1:
            l.append(f"📅 Antes de {esc(EVENTO['nombre'])} estaba a {clp(ref)}: ¡hoy está MÁS CARO!")
        else:
            l.append(f"📅 Mismo precio que antes de {esc(EVENTO['nombre'])} ({clp(ref)}): el descuento no es real")
    # tienda que inflo el precio la semana previa
    t = (h.get("t") or {}).get(s.get("tienda")) or {}
    val = {d: (v[0] if isinstance(v, list) else v) for d, v in t.items()}
    previo = [v for d, v in val.items() if base_ini <= d <= base_fin]
    semana = [v for d, v in val.items() if _sumar_dias(desde, -6) <= d < desde]
    if previo and semana and max(semana) > 1.1 * min(previo):
        l.append(f"⚠️ {esc(s['tienda'])} subió este juego de {clp(min(previo))} a {clp(max(semana))} "
                 f"la semana antes del evento: ojo con el «descuento»")
    return l


# ---------------------------------------------------------------- clasificar

def es_urgente(it):
    if it["tipo"] in ("memoria", "accesorio", "consola") or it.get("extra") in ("📦 se está agotando", "📅 preventa abierta"):
        return True                     # no se guardan para el resumen: van al momento
    return it["nivel"] in ("objetivo", "compra") or any(
        x in (it.get("extra") or "") for x in ("⭐", "🏆"))


def render_alertas(alertas, state, hist):
    """Convierte las alertas en tarjetas [{nivel, texto, score, boton, ...}] y avisos."""
    items, avisos = [], []
    for a in alertas:
        if a["tipo"] == "aviso":
            avisos.append(a["linea"])
            continue
        if a["tipo"] in ("memoria", "accesorio", "consola"):
            nivel = a["extra"] if a.get("extra") in NIVELES else "bueno"
            items.append({"nivel": nivel, "texto": a["linea"], "score": a["score"], "tipo": a["tipo"],
                          "extra": None, "ref": None, "boton": None})
            continue
        grupo = "productos" if a["tipo"] == "fisico" else "eshop"
        s = (state.get(grupo) or {}).get(a.get("ref"))
        if not s:
            continue
        if a["tipo"] == "fisico":
            nivel, texto = tarjeta_fisico(a["ref"], s, state, hist, a.get("extra"))
        else:
            nivel, texto = tarjeta_digital(a["ref"], s, state, hist, a.get("extra"))
        items.append({"nivel": nivel, "texto": texto, "score": a["score"], "s": s, "tipo": a["tipo"],
                      "extra": a.get("extra"), "ref": a["ref"],
                      "boton": boton_de(s, a["tipo"]) if s.get("id") else None})
    items.sort(key=lambda it: (NIVELES[it["nivel"]][0], -it["score"]))
    return items, avisos


def mensaje_alerta(items, avisos, titulo, sub_extra=""):
    if not items and not avisos:
        return []
    sub = f"{fecha_hora_chile()} · {len(items)} novedad{'es' if len(items) != 1 else ''}{sub_extra}"
    res = ["📋 <b>RESUMEN</b>"] + resumen_niveles(items)
    if items and items[0].get("s"):
        s = items[0]["s"]
        nombre = s.get("nombre") or s.get("titulo")
        precio = clp(s["precio"]) if items[0]["tipo"] == "fisico" else usd(s["ultimo"])
        donde = s.get("tienda") or pais_txt(s.get("pais", "US"))
        res.append(f"⭐ Mejor oportunidad: <b>{esc(nombre)}</b> a {precio} en {esc(donde)}")
    bloques = [cabecera(titulo, sub) + "\n" + "\n".join(res)]
    bloques += bloques_por_nivel(items)
    if avisos:
        bloques.append("⚠️ <b>AVISOS DEL BOT</b>\n" + "\n".join(f"• {x}" for x in avisos))
    bloques.append(f"{LINEA}\n{LEYENDA}\n\nVer todo lo vigente: /ofertas · Por saga: /precios · Tu lista: /seguidos")
    return empaquetar(bloques, titulo)


# ---------------------------------------------------------------- resumen

# ---------------------------------------------------------------- informe de negocio

def oportunidades_fisicas(state, hist, hoy):
    """Fisicos vigentes con su lectura de negocio, sin usados ni ediciones especiales."""
    hp = hist.get("productos") or {}
    corte = _sumar_dias(hoy, -1)
    out = []
    for clave, s in (state.get("productos") or {}).items():
        if (s.get("visto") or "")[:10] < corte or s.get("externo") or s.get("variante"):
            continue
        if USADO_RE.search(norm(s.get("nombre") or "")):
            continue
        out.append((clave, s, negocio(hp.get(clave) or {}, s, hoy)))
    return out


def estado_canal():
    """Lo que paso hoy en el canal (canal_estado.json + canal.json + Telegram)."""
    est = load_json(BASE / "canal_estado.json", {}) or {}
    ccfg = load_json(BASE / "canal.json", {}) or {}
    aj = ccfg.get("ajustes") or {}
    hoy = hoy_chile()
    dia = (est.get("dias") or {}).get(hoy) or {}
    miembros = None
    canal = os.environ.get("TELEGRAM_CHANNEL_ID")
    token = os.environ.get("TELEGRAM_TOKEN")
    if canal and token and not os.environ.get("DRY_RUN"):
        try:
            r = requests.get(f"https://api.telegram.org/bot{token}/getChatMemberCount",
                             params={"chat_id": canal}, timeout=TIMEOUT)
            if r.status_code == 200:
                miembros = r.json().get("result")
        except requests.RequestException:
            pass
    return {
        "miembros": miembros, "posts_hoy": dia.get("posts", 0), "bombazos_hoy": dia.get("bombazos", 0),
        "slots_hoy": dia.get("slots") or [], "en_revision": len(est.get("cola") or {}),
        "sospechosos_hoy": sum(1 for v in (est.get("sospechosos") or {}).values() if v == hoy),
        "pausado": bool(aj.get("pausado")), "revision_previa": bool(aj.get("revision_previa")),
        "ultimo_post": est.get("ultimo_post"),
    }


def lineas_canal(state, ec):
    l = []
    hist_m = state.setdefault("miembros_canal", {})
    if ec["miembros"] is not None:
        hoy = hoy_chile()
        previo = [v for d, v in sorted(hist_m.items()) if d < hoy]
        hist_m[hoy] = ec["miembros"]
        for d in sorted(hist_m)[:-60]:
            del hist_m[d]
        delta = f" ({ec['miembros'] - previo[-1]:+d} vs ayer" if previo else ""
        semana = [v for d, v in sorted(hist_m.items()) if d <= _sumar_dias(hoy, -7)]
        if delta:
            delta += (f", {ec['miembros'] - semana[-1]:+d} en 7 días)" if semana else ")")
        l.append(f"👥 {ec['miembros']} miembros{delta}")
    estado = "⏸️ PAUSADO (/canal seguir)" if ec["pausado"] else "▶️ publicando"
    estado += " · 👀 revisión previa" if ec["revision_previa"] else " · sin revisión previa"
    l.append(estado)
    l.append(f"📝 {ec['posts_hoy']} post{'s' if ec['posts_hoy'] != 1 else ''} hoy"
             + (f" ({', '.join(ec['slots_hoy'])})" if ec["slots_hoy"] else "")
             + (f" · 💣 {ec['bombazos_hoy']}" if ec["bombazos_hoy"] else ""))
    if ec["en_revision"]:
        l.append(f"⏳ {ec['en_revision']} esperando tu revisión")
    if ec["sospechosos_hoy"]:
        l.append(f"🚨 {ec['sospechosos_hoy']} precio(s) sospechoso(s) frenados hoy")
    l += lineas_metricas(ec.get("miembros"))
    return l


def metricas_worker(dias):
    if not ml_disponible():
        return None
    try:
        r = requests.get(os.environ["WORKER_URL"].rstrip("/") + "/metricas/datos", params={"dias": dias},
                         headers={"X-Proxy-Key": os.environ["PROXY_KEY"]}, timeout=20)
        d = r.json()
        return d if d.get("ok") else None
    except (requests.RequestException, ValueError):
        return None


def lineas_metricas(miembros):
    """Clics del canal (contados por el Worker) y comisiones anotadas con /comision."""
    m7, m1 = metricas_worker(7), metricas_worker(1)
    if not m7:
        return []
    def clics(m):
        return sum(x["n"] for x in m.get("tipos", []) if x.get("tipo") != "whatsapp")
    def shares(m):
        return sum(x["n"] for x in m.get("tipos", []) if x.get("tipo") == "whatsapp")
    l = [f"🖱️ Clics: {clics(m1) if m1 else '?'} hoy · {clics(m7)} en 7 días"
         + (f" ({clics(m7) / miembros:.1f} por miembro)" if miembros else "")
         + (f" · 📲 {shares(m7)} compartidos" if shares(m7) else "")]
    if m7.get("items"):
        l.append("🔥 Más clickeados: " + " · ".join(f"{esc(x['item'][:28])} ({x['n']})" for x in m7["items"][:3]))
    if m7.get("slots"):
        mejor = max(m7["slots"], key=lambda x: x["n"] / max(1, x["posts"]))
        l.append(f"📰 Mejor post: {esc(mejor['slot'])} ({mejor['n'] / max(1, mejor['posts']):.1f} clics por post)")
    if m7.get("tiendas"):
        l.append("🏬 " + " · ".join(f"{esc(x['tienda'])} {x['n']}" for x in m7["tiendas"][:4]))
    com = m7.get("comisiones_mes") or {}
    l.append(f"💵 Comisiones del mes: {clp(com.get('total') or 0)}" + (f" ({com['n']})" if com.get("n") else "")
             + " · anótalas con /comision")
    return l


def lineas_salud(state, cfg=None):
    l = []
    activas = {t["nombre"] for t in ((cfg or load_json(CONFIG_FILE, {}) or {}).get("tiendas") or [])
               if t.get("activa") is not False}
    rev = state.get("ultima_revision")
    l.append(f"🔄 Última revisión: hace {horas_desde(rev):.0f} h" if rev else "🔄 Sin revisiones aún")
    fallos = {k: v for k, v in (state.get("fallos") or {}).items() if v >= 3 and k in activas}
    if fallos:
        l.append("🏬 Tiendas sin responder: " + ", ".join(f"{k} ({v})" for k, v in sorted(fallos.items())))
    else:
        l.append("🏬 Todas las tiendas respondiendo")
    if state.get("telegram_bloqueado_hasta"):
        l.append(f"⛔ Telegram bloqueó al bot hasta {state['telegram_bloqueado_hasta']}")
    l.append(f"👀 Vigilando {len(state.get('productos') or {})} físicos, "
             f"{len(state.get('eshop') or {})} digitales y {len(state.get('memorias') or {})} memorias")
    return l


def guardar_estado_vista(state, cfg):
    """Resumen chico para /estado del Worker (sin leer los archivos grandes)."""
    try:
        VISTAS_DIR.mkdir(exist_ok=True)
        ev, prox = evento_activo(cfg), proximo_evento(cfg)
        save_json(VISTAS_DIR / "estado.json", {
            "generado": ahora_iso(), "ultima_revision": state.get("ultima_revision"),
            "salud": lineas_salud(state, cfg),
            "evento": ev["nombre"] if ev else None,
            "proximo": f"{prox['nombre']} ({fecha_corta(prox['desde'])})" if prox else None,
            "revisiones_hoy": (state.get("revisiones_dia") or {}).get(hoy_chile(), 0),
            "alertas_hoy": (state.get("urgentes_dia") or {}).get(hoy_chile(), 0),
        })
    except Exception as e:  # noqa: BLE001
        log(f"  estado: error {type(e).__name__}: {e}")


def armar_resumen(state, hist, cfg, revisiones_hoy):
    """Informe diario: tu negocio (mercado fisico), el canal y la salud del bot."""
    hoy = hoy_chile()
    pend = {}
    for p in state.get("pendientes") or []:
        if p["tipo"] != "digital":
            pend[(p["tipo"], p["ref"])] = p
    alertas = [{"tipo": t, "ref": r, "score": p.get("score", 0), "extra": p.get("extra"), "linea": ""}
               for (t, r), p in pend.items()]
    items, _ = render_alertas(alertas, state, hist)
    corte = _sumar_dias(hoy, -1)
    items = [it for it in items if it["nivel"] != "normal" and (it["s"].get("visto") or "")[:10] >= corte
             and not it["s"].get("externo")]

    titulo = "🗞️ <b>INFORME DIARIO</b>"
    sub = f"{fecha_hora_chile()} · {revisiones_hoy} revisi{'ones' if revisiones_hoy != 1 else 'ón'} hoy"
    l = []

    # mercado fisico
    ops = oportunidades_fisicas(state, hist, hoy)
    margen = sorted((x for x in ops if (x[2].get("margen_pct") or 0) >= 15), key=lambda x: -x[2]["margen"])
    agotando = [x for x in ops if x[2].get("escasez")]
    subiendo = sorted((x for x in ops if (x[2].get("tendencia_pct") or 0) >= 8),
                      key=lambda x: -x[2]["tendencia_pct"])
    l.append("💼 <b>MERCADO FÍSICO</b>")
    if margen:
        l.append("<i>Más baratos que en el resto de las tiendas:</i>")
        for clave, s, ng in margen[:6]:
            l.append(f"• <a href=\"{esc_attr(s['url'])}\">{esc(s['nombre'])}</a>{' (S2)' if s.get('plataforma') == 'Switch 2' else ''}"
                     f" — <b>{clp(s['precio'])}</b> en {esc(s['tienda'])} · resto {clp(ng['ref_tiendas'])}"
                     f" (−{round(ng['margen_pct'])}%)")
    else:
        l.append("• Sin diferencias grandes entre tiendas hoy")
    if agotando:
        l += ["", "📦 <i>Se están agotando:</i>"]
        for clave, s, ng in agotando[:5]:
            l.append(f"• {esc(s['nombre'])} — quedaba en {ng['tiendas_max']} tiendas, hoy en {ng['tiendas_hoy']}"
                     f" ({esc(s['tienda'])} {clp(s['precio'])})")
    if subiendo:
        l += ["", "📈 <i>Subiendo de precio (2 semanas):</i>"]
        for clave, s, ng in subiendo[:5]:
            l.append(f"• {esc(s['nombre'])} — {clp(s['precio'])} (+{round(ng['tendencia_pct'])}%)")

    # reventa en mercadolibre
    mlp = state.get("mercadolibre") or {}
    reventa = []
    for clave, s, ng in ops:
        m = mlp.get(clave) or {}
        if m.get("nuevo") and m["nuevo"] >= s["precio"] * 1.15:
            reventa.append((m["nuevo"] - s["precio"], s, m))
    if reventa:
        l += ["", "💰 <b>REVENTA</b> (precio nuevo en MercadoLibre)"]
        for dif, s, m in sorted(reventa, key=lambda x: -x[0])[:6]:
            l.append(f"• <a href=\"{esc_attr(s['url'])}\">{esc(s['nombre'])}</a> — {clp(s['precio'])} en "
                     f"{esc(s['tienda'])} · ML ~{clp(m['nuevo'])} (+{clp(dif)})")

    # memorias
    mem = [m for m in (state.get("memorias") or {}).values() if (m.get("visto") or "")[:10] >= corte and m.get("marca")]
    if mem:
        l += ["", "💾 <b>MEMORIAS</b> (más barato por GB)"]
        for tipo, nombre in (("express", "Switch 2 · Express"), ("normal", "Switch 1 · normal")):
            xs = sorted((m for m in mem if m["tipo"] == tipo), key=lambda m: m["por_gb"])[:2]
            for m in xs:
                l.append(f"• {nombre}: <a href=\"{esc_attr(m['url'])}\">{esc(m['nombre'])}</a> — "
                         f"{clp(m['precio'])} en {esc(m['tienda'])} ({clp(m['por_gb'])}/GB)")

    l += lineas_seguimientos_informe(state)
    l += lineas_consolas_informe(state)
    l += lineas_audifonos_informe(state)
    l += lineas_accesorios_informe(state)

    # objetivos
    por_id = {s.get("id"): s for s in list((state.get("productos") or {}).values()) +
              list((state.get("eshop") or {}).values()) if s.get("id")}
    objs = (SEG.get("objetivos") or {})
    if objs:
        l += ["", "🎯 <b>TUS OBJETIVOS</b>"]
        filas = []
        for id_, o in objs.items():
            s = por_id.get(id_)
            if not s:
                filas.append((9, f"• {esc(o.get('nombre', '?'))} — sin datos en la última revisión"))
                continue
            fis = "precio" in s and "nombre" in s
            act = s["precio"] if fis else s["ultimo"]
            din = clp if fis else usd
            if act <= o["precio"]:
                filas.append((0, f"• ✅ <b>{esc(o['nombre'])}</b> — {din(act)} · ¡llegó a tu objetivo de {din(o['precio'])}!"))
            else:
                falta = 100 * (act - o["precio"]) / act
                filas.append((falta, f"• {esc(o['nombre'])} — hoy {din(act)} · 🎯 {din(o['precio'])} · falta {round(falta)}%"))
        l += [f for _, f in sorted(filas, key=lambda x: x[0])]

    # canal y salud
    l += ["", "📣 <b>CANAL</b>"] + lineas_canal(state, estado_canal())
    l += ["", "🩺 <b>ESTADO DEL BOT</b>"] + lineas_salud(state, cfg)

    # eventos
    ev = EVENTO or None
    prox = proximo_evento(cfg)
    if ev:
        l += ["", f"🚨 <b>MODO EVENTO ACTIVO</b>: {esc(ev['nombre'])} hasta el {fecha_corta(ev['hasta'])}"]
    elif prox:
        dias = _dias_entre(hoy, prox["desde"])
        l += ["", "📅 <b>PRÓXIMO EVENTO</b>",
              f"• {esc(prox['nombre'])} en {dias} día{'s' if dias != 1 else ''} ({fecha_corta(prox['desde'])}"
              + (f", {prox['nota']}" if prox.get("nota") else "") + ")"]

    bloques = [cabecera(titulo, sub) + "\n" + "\n".join(l)]
    if items:
        bloques.append(f"{LINEA}\n🗂️ <b>OFERTAS FÍSICAS QUE ESPERABAN EL INFORME</b>")
        bloques += bloques_por_nivel(items)
    bloques.append(f"{LINEA}\nDigitales para jugar: en el canal · /estado · /canal · /ofertas fisico")
    return empaquetar(bloques, titulo)


# ---------------------------------------------------------------- ciclo

def ciclo(cfg, state, hist, forzar=False, solo_resumen=False):
    """Decide que hacer en esta pasada. Devuelve (mensajes, hubo_cambios)."""
    global EVENTO
    aj = cfg.get("ajustes", {})
    EVENTO = evento_activo(cfg)
    modo = aj.get("modo_alertas", "resumen")          # "resumen" o "inmediato"
    hora_res = int(aj.get("hora_resumen", 20))
    intervalo = 1 if EVENTO else float(aj.get("intervalo_horas", 6))
    ultima = state.get("ultima_revision")
    ahora = ahora_chile()
    hoy = hoy_chile()

    toca_revisar = (forzar or not ultima or horas_desde(ultima) >= intervalo - 0.25) and not solo_resumen
    toca_resumen = solo_resumen or (modo == "resumen" and ahora.hour >= hora_res
                                    and state.get("resumen_enviado") != hoy)
    if EVENTO:
        log(f"🚨 modo evento: {EVENTO['nombre']} (revisión cada hora)")
    log(f"revisar: {'sí' if toca_revisar else 'no'} · resumen: {'sí' if toca_resumen else 'no'}"
        f" (última revisión hace {horas_desde(ultima):.1f} h, intervalo {intervalo:g} h)")
    if not toca_revisar and not toca_resumen:
        return [], False

    mensajes = []
    if toca_revisar:
        alertas = []
        log("Revisando eShop...")
        revisar_eshop(cfg, state, hist, alertas)
        log(f"Revisando tiendas... ({minutos():.1f} min)")
        listados = leer_tiendas(cfg, state, alertas)
        log(f"  tiendas listas a los {minutos():.1f} min")
        revisar_productos(cfg, state, hist, listados, alertas)
        try:
            fechar_tiendas(state, hist, cfg)
        except Exception as e:  # noqa: BLE001
            log(f"  tiendas_desde: error {type(e).__name__}: {e}")
        try:
            leer_memorias_ml(cfg, state)
        except Exception as e:  # noqa: BLE001
            log(f"  memorias ML: error {type(e).__name__}: {e}")
        try:
            revisar_memorias(cfg, state, hist, alertas)
        except Exception as e:  # noqa: BLE001 - las memorias nunca deben botar la revision
            log(f"  memorias: error {type(e).__name__}: {e}")
        for nombre_f, fn in (("seguimientos", revisar_seguimientos), ("audífonos", revisar_audifonos)):
            try:
                fn(cfg, state, hist, alertas)
            except Exception as e:  # noqa: BLE001
                log(f"  {nombre_f}: error {type(e).__name__}: {e}")
        try:
            revisar_consolas(cfg, state, hist, alertas)
        except Exception as e:  # noqa: BLE001
            log(f"  consolas: error {type(e).__name__}: {e}")
        try:
            revisar_accesorios(cfg, state, hist, alertas)
        except Exception as e:  # noqa: BLE001
            log(f"  accesorios: error {type(e).__name__}: {e}")
        try:
            revisar_mercadolibre(cfg, state, hoy)
        except Exception as e:  # noqa: BLE001 - MercadoLibre nunca debe botar la revision
            log(f"  mercadolibre: error {type(e).__name__}: {e}")
        log(f"  revisión completa en {minutos():.1f} min")
        state.pop("tiendas", None)
        for viejo in ("digitales", "fisicos", "nsuids"):
            state.pop(viejo, None)
        state["ultima_revision"] = ahora_iso()
        state["descubrimiento_listo"] = True
        rev = state.setdefault("revisiones_dia", {})
        rev[hoy] = rev.get(hoy, 0) + 1
        for d in sorted(rev)[:-10]:
            del rev[d]

        nuevos_p = [s for s in state.get("productos", {}).values() if s.get("visto_desde") is None]
        nuevos_e = [s for s in state.get("eshop", {}).values() if s.get("visto_desde") is None]
        for s in nuevos_p + nuevos_e:
            s["visto_desde"] = hoy
        if len(nuevos_p) + len(nuevos_e) >= 5:
            con_oferta = sum(1 for s in nuevos_p if (s.get("pct") or 0) >= 15) + \
                sum(1 for s in nuevos_e if s.get("pct_oferta"))
            mensajes.append("\n".join([
                "🆕 <b>BASE DE DATOS AMPLIADA</b>", f"<i>{fecha_hora_chile()}</i>", LINEA,
                f"📦 {len(nuevos_p)} productos físicos nuevos",
                f"🎮 {len(nuevos_e)} juegos de la eShop nuevos",
                f"🔥 {con_oferta} ya están con descuento", "",
                "Los registré en silencio para no llenarte de alertas.",
                "Desde ahora te aviso solo cuando bajen de precio.", LINEA, "Ver las ofertas de hoy: /ofertas"]))

        if not EVENTO:                  # en un evento llega todo lo que encuentre
            alertas = filtrar_alertas(alertas, state, cfg)
        items, avisos = render_alertas(alertas, state, hist)
        if EVENTO:
            titulo = f"🚨 <b>ALERTA · {esc(EVENTO['nombre'].upper())}</b>"
            mensajes += mensaje_alerta(items, avisos, titulo, " · modo evento")
        elif modo == "resumen":
            urg = [it for it in items if es_urgente(it)]
            resto = [it for it in items if not es_urgente(it)]
            mensajes += mensaje_alerta(urg, avisos, "🔔 <b>ALERTA URGENTE</b>",
                                       f" · {len(resto)} más para el resumen de las {hora_res}:00" if resto else "")
            pend = state.setdefault("pendientes", [])
            for it in resto:
                pend.append({"tipo": it["tipo"], "ref": it["ref"], "score": it["score"],
                             "extra": it.get("extra"), "fecha": hoy})
            ud = state.setdefault("urgentes_dia", {})
            ud[hoy] = ud.get(hoy, 0) + len(urg)
            for d in sorted(ud)[:-10]:
                del ud[d]
        else:
            mensajes += mensaje_alerta(items, avisos, "🔔 <b>ALERTA DE OFERTAS</b>")

    if toca_resumen:
        mensajes += armar_resumen(state, hist, cfg, (state.get("revisiones_dia") or {}).get(hoy, 0))
        state["pendientes"] = []
        state["resumen_enviado"] = hoy
    return mensajes, True


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
    if ml_disponible():
        try:
            r = requests.get(os.environ["WORKER_URL"].rstrip("/") + "/ml/estado",
                             headers={"X-Proxy-Key": os.environ["PROXY_KEY"]}, timeout=40)
            log(f"\nMercadoLibre: {r.text[:400]}")
            res = ml_buscar("The Legend of Zelda: Tears of the Kingdom nintendo switch")
            ok = [x for x in res.get("items") or [] if ml_calza(x["titulo"], "The Legend of Zelda: Tears of the Kingdom", "Switch")]
            if not ok:
                res = ml_buscar("The Legend of Zelda: Tears of the Kingdom")
                ok = [x for x in res.get("items") or [] if ml_calza(x["titulo"], "The Legend of Zelda: Tears of the Kingdom", "Switch")]
            log(f"  prueba Zelda TOTK: {len(res.get('items') or [])} avisos, {len(ok)} calzan")
            for x in (res.get("items") or [])[:8]:
                marca = "✓" if x in ok else "✗"
                log(f"    {marca} {x['titulo']} | {clp(x['precio'])} | {x['condicion']}")
            log("\n  microSD en MercadoLibre (✓ = vendedor seguro, el que puede ir al canal):")
            vend = {}
            for q in ("microsd express 256gb", "microsd sandisk 256gb", "microsd samsung evo 256gb"):
                for x in ml_buscar(q).get("items") or []:
                    m = clasificar_memoria(x.get("titulo") or "", x.get("precio"))
                    if not m or x.get("condicion") == "used":
                        continue
                    v = vend.setdefault(x.get("vendedor") or "(sin datos)", {"seguro": x.get("seguro"),
                                                                            "nivel": x.get("nivel"), "n": 0, "min": None})
                    v["n"] += 1
                    v["min"] = min(v["min"] or x["precio"], x["precio"])
                time.sleep(0.5)
            for nombre, v in sorted(vend.items(), key=lambda kv: (not kv[1]["seguro"], kv[1]["min"] or 0)):
                log(f"    {'✓' if v['seguro'] else '✗'} {nombre} · {v['nivel'] or 'sin reputación destacada'}"
                    f" · {v['n']} avisos desde {clp(v['min'])}")
        except (requests.RequestException, ValueError) as e:
            log(f"\nMercadoLibre: ERROR {e}")
    log("\nRelay por el Worker: " + ("activo" if os.environ.get("WORKER_URL") and os.environ.get("PROXY_KEY")
                                      else "NO configurado (faltan WORKER_URL y PROXY_KEY en GitHub)"))
    VIA_WORKER.update(dominio(t["url"]) for t in cfg.get("tiendas", []) if t.get("via") == "worker")
    for tienda in cfg.get("tiendas", []):
        log(f"\n{tienda['nombre']} ({tienda['url']})" + (" [inactiva]" if tienda.get("activa") is False else "") + ":")
        cache = {}
        prods = buscar_en_tienda(tienda, termino, {}, cache)
        for d in cache.get("diag", []):
            log(f"  · {d}")
        if prods is None:
            log("  => NO FUNCIONA")
            continue
        ok = [p for p in prods if precio_valido(p.get("precio")) and coincide(p["titulo"], juego, True)]
        if cache.get("metodo") != "shopify" and tienda.get("activa") is not False:
            cat = recorrer_catalogo(tienda, cache, paginas=3)
            juegos_cat = [p for p in cat if precio_valido(p.get("precio")) and es_juego_switch(p["titulo"])]
            log(f"  catálogo (3 páginas de prueba): {len(cat)} productos, {len(juegos_cat)} juegos de Switch")
            for p in juegos_cat[:3]:
                log(f"    · {p['titulo']} | {clp(p['precio'])}")
        log(f"  => metodo {cache.get('metodo')}: {len(prods)} productos leidos, {len(ok)} calzan"
            + (" · vía Worker" if dominio(tienda["url"]) in RELAY_USADO | VIA_WORKER else ""))
        for p in ok[:6]:
            log(f"  - {p['titulo']} | {clp(p['precio'])}"
                + (f" (normal {clp(p['normal'])})" if p.get("normal") else "") + f" | {p['url']}")
        if not ok:
            for p in prods[:3]:
                log(f"  ? {p.get('titulo')} | {p.get('precio')} | {p.get('url')}")
        if tienda.get("activa") is not False:
            diagnostico_memorias(cfg, tienda, cache)


def diagnostico_memorias(cfg, tienda, cache):
    """microSD de la tienda: cuantas encuentra, cuales cuentan y por que se descartan las otras."""
    terminos_mem = (cfg.get("memorias") or {}).get("buscar", MEMORIAS_BUSCAR)
    if cache.get("metodo") == "shopify":
        fuentes = cache.get("shopify") or []
    else:
        fuentes = []
        for t in terminos_mem:
            fuentes += buscar_en_tienda(tienda, t, {}, cache) or []
            time.sleep(PAUSA)
    vistos, buenas, malas = set(), [], {}
    for p in fuentes:
        if p.get("url") in vistos or not precio_valido(p.get("precio")) or not MEM_RE.search(norm(p["titulo"])):
            continue
        vistos.add(p.get("url"))
        m = clasificar_memoria(p["titulo"], p["precio"], p.get("url"))
        if m:
            buenas.append((m, p))
        else:
            malas.setdefault(motivo_no_memoria(p["titulo"], p["precio"]) or "?", []).append(p)
    log(f"  microSD: {len(vistos)} avisos, {len(buenas)} cuentan"
        + (f", {sum(1 for m, _ in buenas if not m['marca'])} sin marca reconocida (no avisan)" if buenas else ""))
    for m, p in sorted(buenas, key=lambda x: (x[0]["tipo"], x[0]["gb"], x[1]["precio"]))[:8]:
        log(f"    ✓ {nombre_memoria(m)} | {clp(p['precio'])} | {p['titulo'][:70]}")
    for motivo, ps in malas.items():
        log(f"    ✗ {motivo}: {len(ps)} · ej. {ps[0]['titulo'][:70]}")


# ---------------------------------------------------------------------- main

def main():
    # si algo se cuelga, a los 33 min se imprime en el log donde esta cada hilo (para encontrarlo)
    import faulthandler
    faulthandler.dump_traceback_later(33 * 60, exit=False)
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
    global SEG
    SEG = load_json(SEG_FILE, {})
    hist = load_json(HIST_FILE, {})
    mensajes, cambios = ciclo(cfg, state, hist, forzar="--forzar" in sys.argv,
                              solo_resumen="--resumen" in sys.argv)
    if not cambios:
        log("Nada que hacer en esta pasada.")
        return
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
    guardar_estado_vista(state, cfg)
    log(f"Listo: {enviados}/{len(mensajes)} mensajes enviados.")

if __name__ == "__main__":
    main()
