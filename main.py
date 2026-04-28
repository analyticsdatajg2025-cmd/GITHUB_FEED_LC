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

# Configura los datos de tu nuevo REPOSITORIO aquí
GITHUB_USER = "analyticsdatajg2025-cmd" 
REPO_NAME = "CURACAO_FEED_PPL" # Cambia esto por el nombre de tu nuevo repo
BASE_URL_IMG = f"https://{GITHUB_USER}.github.io/{REPO_NAME}/{OUTPUT_DIR}/"

# Datos de La Curacao
FEED_URL = "https://www.lacuracao.pe/media/feed/google_curacao.txt"
SHEET_ID = "1vFSUCzMYO5-uh_Fs5OZlubjF2iMIyxqpDnFat9nKjg0" # ¡RECUERDA PONER EL ID DE TU NUEVA HOJA!
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

BATCH_SIZE = 5000 
MAX_THREADS = 40  

# Recursos Gráficos Específicos LC
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

# --- 2. FUNCIONES AUXILIARES ---
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
        # A. DATOS
        val_sale_price = get_clean_price_val(row['sale_price'])
        price_tag = f"{val_sale_price:.2f}".replace('.', '_')
        file_name = f"{row['id']}_{price_tag}.jpg"
        target_path = os.path.join(OUTPUT_DIR, file_name)
        final_url = f"{BASE_URL_IMG}{file_name}"

        if os.path.exists(target_path):
            return final_url, False

        # Limpiar versiones viejas del mismo ID
        for f in glob.glob(os.path.join(OUTPUT_DIR, f"{row['id']}_*.jpg")):
            os.remove(f)

        # B. DESCARGA PRODUCTO
        raw_url = str(row['image_link']).strip()
        clean_url = quote(raw_url, safe="%/:=&?~#+!$,;'@()*[]") 
        res_prod = requests.get(clean_url, headers=HEADERS, timeout=15)
        if res_prod.status_code != 200: return raw_url, False
        prod_img = Image.open(BytesIO(res_prod.content)).convert("RGBA")

        # C. DISEÑO SOBRE PLANTILLA LC
        # Cargamos la plantilla de 1000x1000
        canvas = Image.open(TEMPLATE_PATH).convert("RGB")
        draw = ImageDraw.Draw(canvas)

        # Pegar producto (ajustado al centro de la plantilla)
        prod_img.thumbnail((650, 550), Image.Resampling.LANCZOS)
        canvas.paste(prod_img, ((1000 - prod_img.width)//2, 120 + (500 - prod_img.height)//2), prod_img)

        # D. TEXTOS (Blanco)
        color_blanco = (255, 255, 255)
        
        # Marca (Bold)
        f_brand = load_font(F_BOLD_PATH, 28)
        brand_txt = str(row['brand']).upper().strip()
        draw.text((70, 830), brand_txt, font=f_brand, fill=color_blanco)

        # Título (Regular - 30 pt)
        f_title = load_font(F_REG_PATH, 30)
        lines = textwrap.wrap(str(row['title']).strip(), width=35)[:3]
        y_text = 870
        for line in lines:
            draw.text((70, y_text), line, font=f_title, fill=color_blanco)
            y_text += 35

        # Precio (Bold)
        p_str = f"{val_sale_price:.2f}"
        f_symbol = load_font(F_BOLD_PATH, 60) # S/ a 60pt
        f_price = load_font(F_BOLD_PATH, 27)  # Monto a 27pt
        
        # Alineación a la derecha
        w_price = draw.textlength(p_str, font=f_price)
        w_sym = draw.textlength("S/", font=f_symbol)
        x_price_start = 930 - (w_sym + 10 + w_price)
        
        draw.text((x_price_start, 875), "S/", font=f_symbol, fill=color_blanco)
        draw.text((x_price_start + w_sym + 10, 905), p_str, font=f_price, fill=color_blanco)

        # E. OPTIMIZACIÓN FINAL (600x600 para no saturar)
        canvas = canvas.resize((600, 600), Image.Resampling.LANCZOS)
        canvas.save(target_path, "JPEG", optimize=True, quality=75)
        return final_url, True

    except Exception as e:
        print(f"Error en ID {row.get('id', '?')}: {e}")
        return row['image_link'], False

# --- 4. MAIN ---
def main():
    print(">>> [1/4] Descargando Feed LC...")
    df = pd.read_csv(FEED_URL, sep='\t', on_bad_lines='skip', low_memory=False)
    df.columns = [c.replace('g:', '').strip() for c in df.columns]
    
    df = df[df['availability'] == 'in stock'].copy()
    df.drop_duplicates(subset=['id'], keep='first', inplace=True)
    
    # RECOLECTOR DE BASURA
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