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

# Datos de tu repositorio
GITHUB_USER = "analyticsdatajg2025-cmd" 
REPO_NAME = "GITHUB_FEED_LC" # Ajusta si tu repo se llama distinto
BASE_URL_IMG = f"https://{GITHUB_USER}.github.io/{REPO_NAME}/{OUTPUT_DIR}/"

FEED_URL = "https://www.lacuracao.pe/media/feed/google_curacao.txt"
SHEET_ID = "1vFSUCzMYO5-uh_Fs5OZlubjF2iMIyxqpDnFat9nKjg0" # <-- PON AQUÍ TU ID DE GOOGLE SHEETS DE LC
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

BATCH_SIZE = 5000 
MAX_THREADS = 40  

# Recursos Gráficos LC
TEMPLATE_PATH = os.path.join(ASSETS_DIR, "LC - PLANTILLA OFERTAS FEEDOM_PPL_.jpg")
F_BOLD_PATH = "GlacialIndifference-Bold.otf"
F_REG_PATH = "GlacialIndifference-Regular.otf"

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

def git_autosave(batch_index):
    try:
        subprocess.run(["git", "add", "images/"], check=False)
        msg = f"Auto-save LC: Bloque {batch_index}"
        subprocess.run(["git", "commit", "-m", msg], check=False)
        subprocess.run(["git", "push"], check=False)
        print(f"   💾 [Git] Progreso guardado (Bloque {batch_index}).")
    except Exception as e:
        print(f"   ⚠️ Error Git: {e}")

# --- 3. PROCESAMIENTO ---
def procesar_fila(row):
    try:
        # A. DATOS (La Curacao usa 'price', ya no 'sale_price')
        val_price = get_clean_price_val(row.get('price', 0))
        price_tag = f"{val_price:.2f}".replace('.', '_')
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

        # C. DISEÑO SOBRE PLANTILLA LC
        canvas = Image.open(TEMPLATE_PATH).convert("RGB")
        draw = ImageDraw.Draw(canvas)

        prod_img.thumbnail((650, 550), Image.Resampling.LANCZOS)
        canvas.paste(prod_img, ((1000 - prod_img.width)//2, 120 + (500 - prod_img.height)//2), prod_img)

        # D. TEXTOS (Blanco)
        color_blanco = (255, 255, 255)
        
        f_brand = load_font(F_BOLD_PATH, 28)
        brand_txt = str(row.get('brand', '')).upper().strip()
        draw.text((70, 830), brand_txt, font=f_brand, fill=color_blanco)

        f_title = load_font(F_REG_PATH, 30)
        lines = textwrap.wrap(str(row.get('title', '')).strip(), width=35)[:3]
        y_text = 870
        for line in lines:
            draw.text((70, y_text), line, font=f_title, fill=color_blanco)
            y_text += 35

        p_str = f"{val_price:.2f}"
        f_symbol = load_font(F_BOLD_PATH, 60)
        f_price = load_font(F_BOLD_PATH, 27)
        
        w_price = draw.textlength(p_str, font=f_price)
        w_sym = draw.textlength("S/", font=f_symbol)
        x_price_start = 930 - (w_sym + 10 + w_price)
        
        draw.text((x_price_start, 875), "S/", font=f_symbol, fill=color_blanco)
        draw.text((x_price_start + w_sym + 10, 905), p_str, font=f_price, fill=color_blanco)

        canvas = canvas.resize((600, 600), Image.Resampling.LANCZOS)
        canvas.save(target_path, "JPEG", optimize=True, quality=75)
        return final_url, True

    except Exception as e:
        print(f"Error en ID {row.get('id', '?')}: {e}")
        return str(row.get('image_link', '')), False

# --- 4. MAIN ---
def main():
    print(">>> [1/4] Descargando Feed LC y Bypassing Firewall...")
    res_feed = requests.get(FEED_URL, headers=HEADERS, timeout=60)
    if res_feed.status_code != 200:
        print(f"❌ Error al descargar el feed: {res_feed.status_code}")
        exit(1)
        
    # 🔥 SOLUCIÓN DE ENCODING APLICADA AQUÍ (utf-8) para los caracteres raros
    df = pd.read_csv(BytesIO(res_feed.content), sep='\t', on_bad_lines='skip', low_memory=False, encoding='utf-8')
    df.columns = [c.replace('g:', '').strip() for c in df.columns]
    
    # 🔥 SOLUCIÓN DE DISPONIBILIDAD: Normalizar a in stock
    if 'availability' in df.columns:
        df['availability'] = df['availability'].astype(str).str.lower().str.replace('_', ' ').str.strip()
        df = df[df['availability'] == 'in stock'].copy()

    # Prevenir fallos si hay imágenes nulas
    if 'image_link' in df.columns:
        df = df[df['image_link'].notna()]

    df.drop_duplicates(subset=['id'], keep='first', inplace=True)
    
    print(">>> Limpiando imágenes obsoletas...")
    ids_validos = set(df['id'].astype(str).tolist())
    for ruta in glob.glob(os.path.join(OUTPUT_DIR, "*.jpg")):
        if os.path.basename(ruta).split('_')[0] not in ids_validos:
            os.remove(ruta)

    rows_to_process = df.to_dict('records')
    print(f">>> Total productos ÚNICOS a procesar: {len(rows_to_process)}")

    if len(rows_to_process) == 0:
        print("⚠️ No hay productos válidos para procesar. Abortando.")
        return

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