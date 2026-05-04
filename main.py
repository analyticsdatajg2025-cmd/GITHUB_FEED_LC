import pandas as pd
import requests
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
import os
import textwrap
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
import glob
import time
import subprocess
import re
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm
from urllib.parse import quote

# --- 1. CONFIGURACIÓN ---
OUTPUT_DIR = "images"
ASSETS_DIR = "assets"
os.makedirs(OUTPUT_DIR, exist_ok=True)

GITHUB_USER = "analyticsdatajg2025-cmd" 
REPO_NAME = "GITHUB_FEED_LC" 
BASE_URL_IMG = f"https://{GITHUB_USER}.github.io/{REPO_NAME}/{OUTPUT_DIR}/"

FEED_URL = "https://www.lacuracao.pe/media/feed/feed_fb_lc.csv"
SHEET_ID = "1vFSUCzMYO5-uh_Fs5OZlubjF2iMIyxqpDnFat9nKjg0" 
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

BATCH_SIZE = 5000 
MAX_THREADS = 40 

# Recursos Gráficos
TEMPLATE_PATH = os.path.join(ASSETS_DIR, "LC - PLANTILLA OFERTAS FEEDOM_PPL_.jpg")
F_BOLD_PATH = "GlacialIndifference-Bold.otf"
F_REG_PATH = "GlacialIndifference-Regular.otf"

# Cargar Credenciales
credentials_json = os.environ.get('GCP_CREDENTIALS')
if credentials_json:
    creds_dict = json.loads(credentials_json)
else:
    try:
        with open('service_account.json') as f:
            creds_dict = json.load(f)
    except:
        print("Error: Credenciales no encontradas.")
        exit(1)

# --- 2. FUNCIONES AUXILIARES ---

def limpiar_nombre_archivo(nombre):
    """ Sanitización agresiva para evitar errores 404 en GitHub Pages """
    # Eliminamos cualquier carácter que no sea letra, número, guion o guion bajo
    s = str(nombre).strip()
    s = re.sub(r'[\\/*?:"<>|]', '', s)
    return s.replace(' ', '_')

def es_link_funcional(url):
    try:
        if not url or pd.isna(url): return False
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code != 200: return False
        if "no hemos encontrado resultados" in res.text.lower(): return False
        return True
    except: return False

def load_font(filename, size):
    path = os.path.join(ASSETS_DIR, filename)
    try: return ImageFont.truetype(path, size)
    except: return ImageFont.load_default()

def get_clean_price_val(val_str):
    if pd.isna(val_str): return 0.0
    s = str(val_str).upper().replace(' PEN', '').replace('PEN', '').replace(',', '').strip()
    try: return float(s)
    except: return 0.0

def get_width_spaced(text, font, draw_obj, spacing):
    if not text: return 0
    return sum(draw_obj.textlength(c, font=font) for c in text) + (spacing * (len(text) - 1))

def git_autosave(batch_index):
    try:
        subprocess.run(["git", "add", "images/"], check=False)
        msg = f"Auto-save LC: Bloque {batch_index}"
        subprocess.run(["git", "commit", "-m", msg], check=False)
        subprocess.run(["git", "push"], check=False)
    except: pass

# --- 3. PROCESAMIENTO DE IMAGEN ---
def procesar_fila(row):
    try:
        val_sale_price = get_clean_price_val(row.get('sale_price', 0))
        if val_sale_price <= 0: return None, False

        prod_link = str(row.get('link', '')).strip()
        if not es_link_funcional(prod_link): return None, False

        val_price = get_clean_price_val(row.get('price', 0))
        clean_id = limpiar_nombre_archivo(row['id'])
        price_tag = f"{val_sale_price:.2f}".replace('.', '_')
        file_name = f"{clean_id}_{price_tag}.jpg"
        
        target_path = os.path.join(OUTPUT_DIR, file_name)
        final_url = f"{BASE_URL_IMG}{file_name}"

        if os.path.exists(target_path): return final_url, False

        for f in glob.glob(os.path.join(OUTPUT_DIR, f"{clean_id}_*.jpg")):
            try: os.remove(f)
            except: pass

        raw_img_url = str(row.get('image_link', '')).strip()
        clean_img_url = quote(raw_img_url, safe="%/:=&?~#+!$,;'@()*[]") 
        res_img = requests.get(clean_img_url, headers=HEADERS, timeout=15)
        if res_img.status_code != 200: return None, False
        prod_img = Image.open(BytesIO(res_img.content)).convert("RGBA")

        # --- DISEÑO MAESTRO LC ---
        canvas = Image.open(TEMPLATE_PATH).convert("RGB")
        canvas = canvas.resize((1080, 1080), Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(canvas)

        # 1. Pegar Producto
        prod_img.thumbnail((680, 520), Image.Resampling.LANCZOS)
        canvas.paste(prod_img, ((1080 - prod_img.width)//2, 140 + (580 - prod_img.height)//2), prod_img)

        color_blanco = (255, 255, 255)
        MARGIN_RIGHT, MARGIN_LEFT = 1010, 70

        # 2. SALE PRICE (Con ajuste de tamaño inteligente)
        p_sale_str = f"{val_sale_price:.2f}"
        size_sale = 135
        f_sale = load_font(F_BOLD_PATH, size_sale)
        f_symbol = load_font(F_BOLD_PATH, int(size_sale * 0.5))
        
        while size_sale > 60:
            w_total = draw.textlength("S/", font=f_symbol) + 12 + get_width_spaced(p_sale_str, f_sale, draw, -4)
            if w_total <= 420: break
            size_sale -= 5
            f_sale = load_font(F_BOLD_PATH, size_sale)
            f_symbol = load_font(F_BOLD_PATH, int(size_sale * 0.5))

        w_monto = get_width_spaced(p_sale_str, f_sale, draw, -4)
        draw.text((MARGIN_RIGHT - w_monto, 920), p_sale_str, font=f_sale, fill=color_blanco)
        draw.text((MARGIN_RIGHT - w_monto - 15 - draw.textlength("S/", font=f_symbol), 935), "S/", font=f_symbol, fill=color_blanco)

        # 3. PRECIO REGULAR
        p_reg_str = f"PRECIO REGULAR: S/{val_price:.2f}"
        f_reg_ui = load_font(F_REG_PATH, 30)
        draw.text((MARGIN_RIGHT - draw.textlength(p_reg_str, font=f_reg_ui), 865), p_reg_str, font=f_reg_ui, fill=color_blanco)

        # 4. MARCA (Ajuste inteligente)
        brand_txt = str(row.get('brand', '')).upper().strip()
        size_br = 35
        f_br = load_font(F_BOLD_PATH, size_br)
        while size_br > 20 and draw.textlength(brand_txt, font=f_br) > 500:
            size_br -= 2
            f_br = load_font(F_BOLD_PATH, size_br)
        draw.text((MARGIN_LEFT, 860), brand_txt, font=f_br, fill=color_blanco)

        # 5. TÍTULO (Multilínea inteligente)
        title_txt = str(row.get('title', '')).strip()
        f_ti = load_font(F_REG_PATH, 42)
        lines = textwrap.wrap(title_txt, width=28)[:2] # Máximo 2 líneas para no chocar
        y_ti = 910
        for line in lines:
            draw.text((MARGIN_LEFT, y_ti), line, font=f_ti, fill=color_blanco)
            y_ti += 48

        canvas = canvas.resize((600, 600), Image.Resampling.LANCZOS)
        canvas.save(target_path, "JPEG", optimize=True, quality=80)
        return final_url, True
    except:
        return None, False

# --- 4. MAIN ---
def main():
    print(">>> [1/4] Purificando Feed La Curacao...")
    res_feed = requests.get(FEED_URL, headers=HEADERS, timeout=60)
    df = pd.read_csv(BytesIO(res_feed.content), sep=',', skiprows=2, on_bad_lines='skip', low_memory=False, encoding='utf-8')
    df.columns = [c.replace('g:', '').strip() for c in df.columns]
    
    df = df[df['availability'].astype(str).str.lower().str.contains('in stock')].copy()
    df = df.dropna(subset=['id', 'link', 'image_link'])
    
    # Filtro de unicidad visual (Juntoz style)
    df.drop_duplicates(subset=['title'], keep='first', inplace=True)
    df.drop_duplicates(subset=['id'], keep='first', inplace=True)
    
    rows_to_process = df.to_dict('records')
    print(f">>> Productos a procesar: {len(rows_to_process)}")

    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive'])
    sheet = gspread.authorize(creds).open_by_key(SHEET_ID).sheet1
    sheet.clear()
    sheet.append_row(list(df.columns))

    print(">>> [3/4] Generando Diseños...")
    for i in range(0, len(rows_to_process), BATCH_SIZE):
        batch = rows_to_process[i : i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            results = list(tqdm(executor.map(procesar_fila, batch), total=len(batch), leave=False))
        
        valid_data = []
        any_new = False
        for idx, res in enumerate(results):
            if res and res[0]:
                row = batch[idx]
                row['image_link'] = res[0]
                row['sale_price'] = f"{get_clean_price_val(row['sale_price']):.2f} PEN"
                row['price'] = f"{get_clean_price_val(row['price']):.2f} PEN"
                valid_data.append(row)
                if res[1]: any_new = True
        
        if valid_data:
            if any_new: git_autosave(i // BATCH_SIZE + 1)
            sheet.append_rows(pd.DataFrame(valid_data).astype(str).values.tolist(), value_input_option='RAW')

    print("\n>>> 🏁 ¡PROCESO LC COMPLETADO!")

if __name__ == "__main__":
    main()