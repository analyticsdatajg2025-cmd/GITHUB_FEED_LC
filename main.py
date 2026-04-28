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

# Nuevo Feed CSV de La Curacao
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
        print(f"    💾 [Git] Progreso guardado (Bloque {batch_index}).")
    except Exception as e:
        print(f"    ⚠️ Error Git: {e}")

# --- 2. PROCESAMIENTO DE IMAGEN ---
def procesar_fila(row):
    try:
        val_sale_price = get_clean_price_val(row.get('sale_price', 0))
        val_price = get_clean_price_val(row.get('price', 0))

        price_tag = f"{val_sale_price:.2f}".replace('.', '_')
        file_name = f"{row['id']}_{price_tag}.jpg"
        target_path = os.path.join(OUTPUT_DIR, file_name)
        final_url = f"{BASE_URL_IMG}{file_name}"

        if os.path.exists(target_path):
            return final_url, False

        for f in glob.glob(os.path.join(OUTPUT_DIR, f"{row['id']}_*.jpg")):
            os.remove(f)

        raw_url = str(row.get('image_link', '')).strip()
        clean_url = quote(raw_url, safe="%/:=&?~#+!$,;'@()*[]") 
        res_prod = requests.get(clean_url, headers=HEADERS, timeout=15)
        if res_prod.status_code != 200: return raw_url, False
        prod_img = Image.open(BytesIO(res_prod.content)).convert("RGBA")

        # Lienzo Maestro 1080x1080
        canvas = Image.open(TEMPLATE_PATH).convert("RGB")
        canvas = canvas.resize((1080, 1080), Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(canvas)

        prod_img.thumbnail((680, 520), Image.Resampling.LANCZOS)
        canvas.paste(prod_img, ((1080 - prod_img.width)//2, 140 + (580 - prod_img.height)//2), prod_img)

        color_blanco = (255, 255, 255)
        MARGIN_RIGHT, MARGIN_LEFT = 1010, 70
        WIDTH_PRICE_MAX = 400 

        # 1. SALE PRICE (Tracking compacto y Baseline alineado)
        p_sale_str = f"{val_sale_price:.2f}"
        size_sale = 135
        f_sale = load_font(F_BOLD_PATH, size_sale)
        f_symbol = load_font(F_BOLD_PATH, int(size_sale * 0.5)) 
        MIN_SPACING = 8
        LETTER_SPACING = -4

        while size_sale > 50:
            w_sale = get_width_spaced(p_sale_str, f_sale, draw, LETTER_SPACING)
            w_sym = draw.textlength("S/", font=f_symbol)
            if (w_sym + MIN_SPACING + w_sale) <= WIDTH_PRICE_MAX: break
            size_sale -= 4
            f_sale = load_font(F_BOLD_PATH, size_sale)
            f_symbol = load_font(F_BOLD_PATH, int(size_sale * 0.5))

        w_final_sale = get_width_spaced(p_sale_str, f_sale, draw, LETTER_SPACING)
        x_final_sale_monto = MARGIN_RIGHT - w_final_sale
        x_final_sym = x_final_sale_monto - MIN_SPACING - draw.textlength("S/", font=f_symbol)
        
        TARGET_BASELINE_Y = 1000 
        y_final_sale_monto = TARGET_BASELINE_Y - f_sale.getmetrics()[0]
        y_final_sym = TARGET_BASELINE_Y - f_symbol.getmetrics()[0]

        draw.text((x_final_sym, y_final_sym), "S/", font=f_symbol, fill=color_blanco)
        x_cursor = x_final_sale_monto
        for char in p_sale_str:
            draw.text((x_cursor, y_final_sale_monto), char, font=f_sale, fill=color_blanco)
            x_cursor += draw.textlength(char, font=f_sale) + LETTER_SPACING

        # 2. PRECIO REGULAR (Mayúsculas)
        p_reg_str = f"PRECIO REGULAR: S/{val_price:.2f}"
        f_reg = load_font(F_REG_PATH, 30) 
        w_reg = draw.textlength(p_reg_str, font=f_reg)
        draw.text((MARGIN_RIGHT - w_reg, 865), p_reg_str, font=f_reg, fill=color_blanco)

        # 3. MARCA
        brand_txt = str(row.get('brand', '')).upper().strip() 
        size_brand = 35 
        f_brand = load_font(F_BOLD_PATH, size_brand)
        while size_brand > 18:
            if draw.textlength(brand_txt, font=f_brand) < 540: break
            size_brand -= 2
            f_brand = load_font(F_BOLD_PATH, size_brand)
        draw.text((MARGIN_LEFT, 860), brand_txt, font=f_brand, fill=color_blanco)

        # 4. TÍTULO ADAPTABLE
        title_txt = str(row.get('title', '')).strip()
        size_title = 45 
        f_title = load_font(F_REG_PATH, size_title) 
        lines = []
        while size_title > 18:
            avg_char_w = draw.textlength("a", font=f_title)
            chars_per_line = max(int(540 / (avg_char_w or 10)), 1)
            temp_lines = textwrap.wrap(title_txt, width=chars_per_line)
            if len(temp_lines) <= 3 and all(draw.textlength(l, font=f_title) <= 540 for l in temp_lines):
                lines = temp_lines
                break
            size_title -= 2
            f_title = load_font(F_REG_PATH, size_title)

        y_pos = 910
        for line in lines:
            draw.text((MARGIN_LEFT, y_pos), line, font=f_title, fill=color_blanco)
            y_pos += (size_title + 4)

        canvas = canvas.resize((600, 600), Image.Resampling.LANCZOS)
        canvas.save(target_path, "JPEG", optimize=True, quality=80)
        return final_url, True

    except Exception as e:
        print(f"Error en ID {row.get('id', '?')}: {e}")
        return str(row.get('image_link', '')), False

# --- 3. MAIN ---
def main():
    print(">>> [1/4] Descargando Feed LC y Bypassing Firewall...")
    res_feed = requests.get(FEED_URL, headers=HEADERS, timeout=60)
    if res_feed.status_code != 200:
        print(f"❌ Error al descargar: {res_feed.status_code}")
        exit(1)
        
    df = pd.read_csv(BytesIO(res_feed.content), sep=',', skiprows=2, on_bad_lines='skip', low_memory=False, encoding='utf-8')
    df.columns = [c.replace('g:', '').strip() for c in df.columns]
    
    if 'availability' in df.columns:
        df['availability'] = df['availability'].astype(str).str.lower().str.replace('_', ' ').str.strip()
        df = df[df['availability'] == 'in stock'].copy()

    if 'image_link' in df.columns:
        df = df[df['image_link'].notna()]

    df.drop_duplicates(subset=['id'], keep='first', inplace=True)
    
    print(">>> Limpiando imágenes obsoletas...")
    ids_validos = set(df['id'].astype(str).tolist())
    for ruta in glob.glob(os.path.join(OUTPUT_DIR, "*.jpg")):
        if os.path.basename(ruta).split('_')[0] not in ids_validos:
            os.remove(ruta)

    rows_to_process = df.to_dict('records')
    print(f">>> Total productos ÚNICOS: {len(rows_to_process)}")

    print(">>> [2/4] Conectando Sheets...")
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    
    for attempt in range(5):
        try:
            sheet = client.open_by_key(SHEET_ID).sheet1
            sheet.clear()
            sheet.append_row(list(df.columns))
            break
        except: time.sleep(10)

    print(">>> [3/4] Procesando...")
    for i in range(0, len(rows_to_process), BATCH_SIZE):
        batch = rows_to_process[i : i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            results = list(tqdm(executor.map(procesar_fila, batch), total=len(batch), leave=False))
        
        batch_urls = [r[0] for r in results]
        any_new = any(r[1] for r in results)
        
        batch_df = pd.DataFrame(batch)
        batch_df['image_link'] = batch_urls 
        
        if any_new: git_autosave(i // BATCH_SIZE + 1)
        
        try:
            sheet.append_rows(batch_df.astype(str).values.tolist(), value_input_option='RAW')
            time.sleep(2)
        except: pass

    print("\n>>> 🏁 ¡PROCESO COMPLETADO!")

if __name__ == "__main__":
    main()