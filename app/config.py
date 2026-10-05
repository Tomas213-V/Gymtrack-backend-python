import os
from pathlib import Path
from dotenv import load_dotenv
from supabase import create_client, Client

# Cargar variables de entorno desde el archivo .env en la raíz
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Configuración del servidor
PORT = int(os.getenv("PORT", "3000"))
HOST = os.getenv("HOST", "0.0.0.0")

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("VITE_SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_ANON_KEY") or os.getenv("VITE_SUPABASE_PUBLISHABLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("[ADVERTENCIA] Faltan las variables de entorno de Supabase (SUPABASE_URL y SUPABASE_KEY en .env).")

# Cliente Supabase singleton
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"[ERROR] No se pudo inicializar el cliente de Supabase: {e}")

# JWT
JWT_SECRET = os.getenv("JWT_SECRET", "gymtrack_jwt_secret_key_2026")
JWT_EXPIRES_IN = os.getenv("JWT_EXPIRES_IN", "24h")
JWT_ALGORITHM = "HS256"

# Pasarelas de Pago
MERCADOPAGO_ACCESS_TOKEN = (
    os.getenv("MERCADOPAGO_ACCESS_TOKEN")
    or os.getenv("Token_Mer")
    or os.getenv("Api_MER")
    or ""
)
UALA_CLIENT_ID = os.getenv("UALA_CLIENT_ID", "")
UALA_CLIENT_SECRET = os.getenv("UALA_CLIENT_SECRET", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
