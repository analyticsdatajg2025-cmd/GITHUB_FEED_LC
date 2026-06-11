import argparse
import os
import re
import io
import glob
import time
import json
import random
import hashlib
import textwrap
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from PIL import Image, ImageDraw, ImageFont

# ===================== CONFIGURACIÓN =====================
OUTPUT_DIR  = "images"        # imágenes publicadas (commit a main, servidas por GitHub Pages)
STAGING_DIR = "staging_new"   # imágenes NUEVAS de este run (se suben como artifact)
ASSETS_DIR  = "assets"
FEED_CLEAN  = "feed_clean.csv"
MANIFEST_PATH = "images_manifest.txt"   # lista de imágenes ya existentes (cache liviano)

GITHUB_USER  = "analyticsdatajg2025-cmd"
REPO_NAME    = "GITHUB_FEED_LC"
BASE_URL_IMG = f"https://{GITHUB_USER}.github.io/{REPO_NAME}/{OUTPUT_DIR}/"

FEED_URL = os.environ.get("FEED_URL", "https://www.lacuracao.pe/media/feed/feed_fb_lc.csv")
SHEET_ID = "1vFSUCzMYO5-uh_Fs5OZlubjF2iMIyxqpDnFat9nKjg0"

TEMPLATE_PATH = os.path.join(ASSETS_DIR, "GENERICO_LC.png")
F_BOLD_PATH   = "GlacialIndifference-Bold.otf"
F_REG_PATH    = "GlacialIndifference-Regular.otf"

# >>> Sube este número SOLO si cambias la LÓGICA de layout en el código (no la imagen).
#     Si cambias la imagen de plantilla o las fuentes, el hash lo detecta solo.
DESIGN_REV = "1"

MAX_THREADS = 48
SHEET_CHUNK = 20000  # filas por escritura a Sheets
VALIDAR_LINK = True  # ponlo en False para ~duplicar la velocidad (confía en availability del feed)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Headers del feed: minimos y "limpios" (los elaborados gatillan el WAF)
FEED_HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "es-PE,es;q=0.9,en;q=0.8",
}

SCOPES = ["https://spreadsheets.google.com/feeds",
          "https://www.googleapis.com/auth/drive"]

os.makedirs(OUTPUT_DIR, exist_ok=True)

EXISTING_FILES = set()  # se llena en build desde el manifest


# ===================== SESIÓN HTTP (reutilizada + retry) =====================
def build_session():
    s = requests.Session()
    retry = Retry(
        total=3, connect=3, read=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retry,
                          pool_connections=MAX_THREADS,
                          pool_maxsize=MAX_THREADS)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": UA})
    return s


SESSION = build_session()


# ===================== VERSIÓN DE DISEÑO (clave del caché) =====================
@lru_cache(maxsize=1)
def design_version():
    """Hash corto de plantilla + fuentes + DESIGN_REV.
    Si cambia cualquiera, cambia el nombre de archivo y todo se regenera."""
    h = hashlib.md5()
    for p in (TEMPLATE_PATH,
              os.path.join(ASSETS_DIR, F_BOLD_PATH),
              os.path.join(ASSETS_DIR, F_REG_PATH)):
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except FileNotFoundError:
            pass
    h.update(DESIGN_REV.encode())
    return h.hexdigest()[:8]


# ===================== RECURSOS CACHEADOS =====================
@lru_cache(maxsize=64)
def load_font(filename, size):
    try:
        return ImageFont.truetype(os.path.join(ASSETS_DIR, filename), size)
    except Exception:
        return ImageFont.load_default()


@lru_cache(maxsize=1)
def base_canvas():
    """Plantilla cargada UNA vez. Por fila se hace .copy()."""
    return Image.open(TEMPLATE_PATH).convert("RGB").resize(
        (1080, 1080), Image.Resampling.LANCZOS)


# ===================== HELPERS =====================
def limpiar_nombre_archivo(nombre):
    s = str(nombre).strip()
    s = re.sub(r'[\\/*?:"<>|]', '', s)
    return s.replace(' ', '_')


def get_clean_price_val(val_str):
    if pd.isna(val_str):
        return 0.0
    s = str(val_str).upper().replace(' PEN', '').replace('PEN', '').replace(',', '').strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def get_width_spaced(text, font, draw_obj, spacing):
    if not text:
        return 0
    return sum(draw_obj.textlength(c, font=font) for c in text) + (spacing * (len(text) - 1))


def es_link_funcional(url):
    """Valida que el PDP exista antes de pautar."""
    try:
        if not url or pd.isna(url):
            return False
        res = SESSION.get(url, timeout=12)
        if res.status_code != 200:
            return False
        if "no hemos encontrado resultados" in res.text.lower():
            return False
        return True
    except requests.RequestException:
        return False


def col(df, *candidatos):
    """Devuelve el nombre real de una columna (tolerante a mayúsculas/alias) o None."""
    lower = {c.lower(): c for c in df.columns}
    for cand in candidatos:
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


def read_feed(content):
    """Lee el feed detectando el encabezado, sin asumir un skiprows fijo."""
    for skip in (2, 0, 1, 3):
        try:
            df = pd.read_csv(io.BytesIO(content), sep=',', skiprows=skip,
                             on_bad_lines='skip', low_memory=False, encoding='utf-8')
        except Exception:
            continue
        cols = [c.replace('g:', '').strip().lower() for c in df.columns]
        if 'id' in cols and ('availability' in cols or 'image_link' in cols):
            df.columns = [c.replace('g:', '').strip() for c in df.columns]
            return df, skip
    raise RuntimeError("No pude detectar el encabezado del feed (¿cambió el formato?).")


def load_manifest(path=MANIFEST_PATH):
    """Lee la lista de imágenes ya existentes. Si no existe, set vacío (primer run)."""
    try:
        with open(path) as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


# ===================== RENDER (tu diseño original, intacto) =====================
def render_image(row, target_path):
    raw_url = str(row.get('image_link', '')).strip()
    clean_url = quote(raw_url, safe="%/:=&?~#+!$,;'@()*[]")
    res_prod = SESSION.get(clean_url, timeout=20)
    if res_prod.status_code != 200:
        return False
    prod_img = Image.open(io.BytesIO(res_prod.content)).convert("RGBA")

    canvas = base_canvas().copy()
    draw = ImageDraw.Draw(canvas)

    prod_img.thumbnail((680, 520), Image.Resampling.LANCZOS)
    canvas.paste(prod_img, ((1080 - prod_img.width) // 2, 140 + (580 - prod_img.height) // 2), prod_img)

    color_blanco = (0, 0, 0)
    MARGIN_RIGHT, MARGIN_LEFT = 1010, 70
    WIDTH_PRICE_MAX = 420

    val_sale_price = get_clean_price_val(row.get('sale_price', 0))
    val_price = get_clean_price_val(row.get('price', 0))

    # --- Precio oferta ---
    p_sale_str = f"{val_sale_price:.2f}"
    size_sale = 135
    f_sale = load_font(F_BOLD_PATH, size_sale)
    f_symbol = load_font(F_BOLD_PATH, int(size_sale * 0.5))
    LETTER_SPACING = -4

    while size_sale > 50:
        w_sale = get_width_spaced(p_sale_str, f_sale, draw, LETTER_SPACING)
        w_sym = draw.textlength("S/", font=f_symbol)
        if (w_sym + 8 + w_sale) <= WIDTH_PRICE_MAX:
            break
        size_sale -= 4
        f_sale = load_font(F_BOLD_PATH, size_sale)
        f_symbol = load_font(F_BOLD_PATH, int(size_sale * 0.5))

    w_final_sale = get_width_spaced(p_sale_str, f_sale, draw, LETTER_SPACING)
    x_monto = MARGIN_RIGHT - w_final_sale
    x_sym = x_monto - 8 - draw.textlength("S/", font=f_symbol)

    TARGET_BASELINE_Y = 1000
    draw.text((x_sym, TARGET_BASELINE_Y - f_symbol.getmetrics()[0]), "S/", font=f_symbol, fill=color_blanco)

    curr_x = x_monto
    for char in p_sale_str:
        draw.text((curr_x, TARGET_BASELINE_Y - f_sale.getmetrics()[0]), char, font=f_sale, fill=color_blanco)
        curr_x += draw.textlength(char, font=f_sale) + LETTER_SPACING

    # --- Precio regular ---
    p_reg_str = f"PRECIO REGULAR: S/{val_price:.2f}"
    f_reg = load_font(F_REG_PATH, 30)
    draw.text((MARGIN_RIGHT - draw.textlength(p_reg_str, font=f_reg), 865), p_reg_str, font=f_reg, fill=color_blanco)

    # --- Marca ---
    brand_txt = str(row.get('brand', '')).upper().strip()
    size_br = 35
    f_br = load_font(F_BOLD_PATH, size_br)
    while size_br > 18:
        if draw.textlength(brand_txt, font=f_br) < 540:
            break
        size_br -= 2
        f_br = load_font(F_BOLD_PATH, size_br)
    draw.text((MARGIN_LEFT, 860), brand_txt, font=f_br, fill=color_blanco)

    # --- Título ---
    title_txt = str(row.get('title', '')).strip()
    size_ti = 45
    f_ti = load_font(F_REG_PATH, size_ti)
    lines = []
    while size_ti > 18:
        avg_w = draw.textlength("a", font=f_ti)
        chars_line = max(int(540 / (avg_w or 10)), 1)
        temp = textwrap.wrap(title_txt, width=chars_line)
        if len(temp) <= 3 and all(draw.textlength(l, font=f_ti) <= 540 for l in temp):
            lines = temp
            break
        size_ti -= 2
        f_ti = load_font(F_REG_PATH, size_ti)

    y_ti = 910
    for line in lines:
        draw.text((MARGIN_LEFT, y_ti), line, font=f_ti, fill=color_blanco)
        y_ti += (size_ti + 4)

    canvas = canvas.resize((600, 600), Image.Resampling.LANCZOS)
    canvas.save(target_path, "JPEG", optimize=True, quality=80)
    return True


# ===================== PROCESAR UNA FILA =====================
def procesar_fila(args):
    """Devuelve un dict listo para el feed, o None si se descarta.
    Orden CLAVE: primero arma el nombre y revisa caché; la red solo si hace falta."""
    row, force = args
    try:
        val_sale = get_clean_price_val(row.get('sale_price', 0))
        if val_sale <= 0:
            return None

        val_price = get_clean_price_val(row.get('price', 0))
        clean_id = limpiar_nombre_archivo(row['id'])
        price_tag = f"{val_sale:.2f}".replace('.', '_')
        file_name = f"{clean_id}_{price_tag}_{design_version()}.jpg"
        final_url = f"{BASE_URL_IMG}{file_name}"

        is_new = False
        ya_existe = file_name in EXISTING_FILES

        if force or not ya_existe:
            # Solo aquí gastamos red: validar link (si hay) + descargar + render
            link = str(row.get('link', '')).strip()
            if VALIDAR_LINK and link and not es_link_funcional(link):
                return None
            staged = os.path.join(STAGING_DIR, file_name)
            if not render_image(row, staged):
                return None
            is_new = True

        out = dict(row)
        out['image_link'] = final_url
        out['sale_price'] = f"{val_sale:.2f} PEN"
        out['price'] = f"{val_price:.2f} PEN"
        out['__file'] = file_name
        out['__new'] = is_new
        return out
    except Exception:
        return None


# ===================== CREDENCIALES / SHEETS =====================
def load_creds():
    raw = os.environ.get('GCP_CREDENTIALS')
    if raw:
        return json.loads(raw)
    with open('service_account.json') as f:
        return json.load(f)


def open_sheet():
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    creds = ServiceAccountCredentials.from_json_keyfile_dict(load_creds(), SCOPES)
    return gspread.authorize(creds).open_by_key(SHEET_ID).sheet1


def with_retry(fn, *a, **k):
    """Reintenta llamadas a Sheets ante 429/500/502/503/504 con backoff exponencial."""
    from gspread.exceptions import APIError
    last = None
    for i in range(6):
        try:
            return fn(*a, **k)
        except APIError as e:
            last = e
            code = getattr(e.response, 'status_code', None)
            if code in (429, 500, 502, 503, 504):
                wait = min(2 ** i, 60) + random.random()
                print(f"   [sheets] {code} transitorio -> reintento {i+1}/6 en {wait:.1f}s")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Sheets: reintentos agotados ({last})")


def write_sheet(df):
    sheet = open_sheet()
    with_retry(sheet.clear)
    with_retry(sheet.append_row, list(df.columns), value_input_option='RAW')
    rows = df.values.tolist()
    for i in range(0, len(rows), SHEET_CHUNK):
        chunk = rows[i:i + SHEET_CHUNK]
        with_retry(sheet.append_rows, chunk, value_input_option='RAW')
        print(f"   [sheets] {min(i + SHEET_CHUNK, len(rows))}/{len(rows)} filas")


# ===================== MODOS =====================
def descargar_feed(intentos=6):
    """Descarga el feed aguantando 503 transitorios con backoff largo.
    Usa requests.get plano (sin el retry de estado de la sesión) para ver el código real."""
    last = None
    for i in range(intentos):
        try:
            r = requests.get(FEED_URL, headers=FEED_HEADERS, timeout=120)
            if r.status_code == 200 and r.content:
                return r.content
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = type(e).__name__
        if i < intentos - 1:
            wait = min(20 * (i + 1), 120)  # 20, 40, 60, ... hasta 120s
            print(f">>> [prepare] feed no disponible ({last}); reintento {i+1}/{intentos} en {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"No pude descargar el feed tras {intentos} intentos (último: {last})")


def cmd_prepare():
    print(">>> [prepare] Descargando feed LC...")
    content = descargar_feed()
    df, skip = read_feed(content)
    n0 = len(df)
    print(f">>> [prepare] Encabezado OK (skiprows={skip}). Columnas: {list(df.columns)}")

    # Resolver columnas reales (el feed a veces cambia mayúsculas / prefijos)
    mapa = {
        'id':           col(df, 'id'),
        'link':         col(df, 'link'),
        'image_link':   col(df, 'image_link', 'image link'),
        'availability': col(df, 'availability', 'disponibilidad'),
        'sale_price':   col(df, 'sale_price', 'sale price'),
        'price':        col(df, 'price'),
        'brand':        col(df, 'brand'),
        'title':        col(df, 'title'),
    }

    faltan = [k for k in ('id', 'image_link') if mapa[k] is None]
    if faltan:
        raise RuntimeError(f"Faltan columnas críticas: {faltan}. Detectadas: {list(df.columns)}")

    # Renombrar a los nombres que usa el resto del código
    df = df.rename(columns={real: logico for logico, real in mapa.items() if real})

    # --- FILTRO 1: solo "in stock" ---
    if 'availability' in df.columns:
        antes = len(df)
        df = df[df['availability'].astype(str).str.lower().str.contains('in stock', na=False)].copy()
        print(f">>> [prepare] Filtro stock: -{antes - len(df)}")
    else:
        print(">>> [prepare] Aviso: sin columna availability, no se filtra por stock.")

    def _no_vacio(serie):
        return serie.notna() & (serie.astype(str).str.strip() != '')

    # id obligatorio (es el nombre del archivo)
    df = df[_no_vacio(df['id'])]

    # --- FILTRO 2: debe tener URL en 'link' O en 'image_link' ---
    antes = len(df)
    tiene_link = _no_vacio(df['link']) if 'link' in df.columns else False
    tiene_img = _no_vacio(df['image_link']) if 'image_link' in df.columns else False
    df = df[tiene_link | tiene_img]
    print(f">>> [prepare] Filtro link/image_link: -{antes - len(df)}")

    # --- FILTRO 3: price y sale_price > 0 (fuera 0 o vacío) ---
    antes = len(df)
    if 'price' in df.columns:
        df = df[df['price'].apply(get_clean_price_val) > 0]
    if 'sale_price' in df.columns:
        df = df[df['sale_price'].apply(get_clean_price_val) > 0]
    print(f">>> [prepare] Filtro precios (price>0 y sale_price>0): -{antes - len(df)}")

    df.drop_duplicates(subset=['id'], keep='first', inplace=True)
    df['__order'] = range(len(df))
    df.to_csv(FEED_CLEAN, index=False)
    print(f">>> [prepare] Purificados: {len(df)} de {n0}  (design_version={design_version()})")


def cmd_build(shard, shards, force):
    global EXISTING_FILES
    os.makedirs(STAGING_DIR, exist_ok=True)
    EXISTING_FILES = load_manifest()
    print(f">>> [build] manifest: {len(EXISTING_FILES)} imágenes ya existentes | force={force}")
    df = pd.read_csv(FEED_CLEAN, low_memory=False)
    rows = df.to_dict('records')
    mine = rows[shard::shards]
    print(f">>> [build] shard {shard}/{shards} -> {len(mine)} productos | force={force}")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_THREADS) as ex:
        for r in ex.map(procesar_fila, ((row, force) for row in mine)):
            if r:
                results.append(r)

    out = pd.DataFrame(results)
    out.to_csv(f"valid_{shard}.csv", index=False)
    nuevos = sum(1 for r in results if r['__new'])
    print(f">>> [build] válidos={len(results)} nuevos={nuevos}")


def cmd_merge(valid_dir, expected_shards=None):
    parts = sorted(glob.glob(os.path.join(valid_dir, "valid_*.csv")))
    if not parts:
        parts = sorted(glob.glob("valid_*.csv"))
    if not parts:
        raise RuntimeError("No llegó ningún valid_*.csv. ¿Fallaron TODOS los shards?")

    completo = (expected_shards is None) or (len(parts) >= expected_shards)
    print(f">>> [merge] Uniendo {len(parts)} CSVs"
          + (f" de {expected_shards} esperados." if expected_shards else "."))
    if not completo:
        print("⚠️  RUN PARCIAL: faltan shards. Modo seguro -> actualizo Sheets con lo "
              "disponible y NO podo imágenes (para no borrar las buenas). "
              "Usa 'Re-run failed jobs' para cerrar.")

    dfs, vacios = [], 0
    for p in parts:
        try:
            d = pd.read_csv(p, low_memory=False)
        except pd.errors.EmptyDataError:
            vacios += 1
            continue
        if d.empty:
            vacios += 1
        else:
            dfs.append(d)
    if vacios:
        print(f">>> [merge] {vacios}/{len(parts)} CSV(s) vacíos (shards sin productos válidos), ignorados.")
    if not dfs:
        raise RuntimeError(
            "TODOS los CSVs vinieron vacíos: no se generó ningún producto. "
            "Lo más probable es que el WAF de lacuracao.pe esté bloqueando las IPs de GitHub "
            "(revisa el log de un job 'build': si dice 'válidos=0' es eso). "
            "Solución: mirror del feed/imágenes o whitelist de IPs.")
    full = pd.concat(dfs, ignore_index=True)
    if '__order' in full.columns:
        full = full.sort_values('__order')
    full = full.drop_duplicates(subset=['id'], keep='first').reset_index(drop=True)

    # --- Poda: SOLO en runs completos (en parcial borraría imágenes buenas) ---
    if completo:
        valid_files = set(full['__file'].astype(str))
        podadas = 0
        for f in glob.glob(os.path.join(OUTPUT_DIR, "*.jpg")):
            if os.path.basename(f) not in valid_files:
                try:
                    os.remove(f)
                    podadas += 1
                except OSError:
                    pass
        print(f">>> [merge] Imágenes podadas (precio viejo / diseño viejo / sin stock): {podadas}")
    else:
        print(">>> [merge] Poda OMITIDA por seguridad (run parcial).")

    # --- Manifest: refleja exactamente lo que queda en images/ ---
    actuales = sorted(os.path.basename(f) for f in glob.glob(os.path.join(OUTPUT_DIR, "*.jpg")))
    with open(MANIFEST_PATH, "w") as mf:
        mf.write("\n".join(actuales) + "\n")
    print(f">>> [merge] Manifest actualizado: {len(actuales)} imágenes")

    # --- Escribir Sheets (sin columnas internas __, y NaN como vacío) ---
    cols = [c for c in full.columns if not c.startswith('__')]
    salida = full[cols].fillna('').astype(str).replace({'nan': '', 'NaN': '', 'None': ''})
    write_sheet(salida)
    print(f">>> [merge] 🏁 Feed escrito: {len(full)} productos. "
          + ("COMPLETO." if completo else "PARCIAL (re-run para cerrar)."))


# ===================== ENTRYPOINT =====================
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("prepare")

    b = sub.add_parser("build")
    b.add_argument("--shard", type=int, required=True)
    b.add_argument("--shards", type=int, required=True)
    b.add_argument("--force", action="store_true")

    m = sub.add_parser("merge")
    m.add_argument("--valid-dir", default=".")
    m.add_argument("--shards", type=int, default=None)

    args = ap.parse_args()
    if args.cmd == "prepare":
        cmd_prepare()
    elif args.cmd == "build":
        cmd_build(args.shard, args.shards, args.force)
    elif args.cmd == "merge":
        cmd_merge(args.valid_dir, args.shards)


if __name__ == "__main__":
    main()
