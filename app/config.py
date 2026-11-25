import os
from pathlib import Path
from typing import Optional, List, Mapping, Tuple
import httpx
import jwt
from dotenv import load_dotenv
from supabase import create_client, Client, ClientOptions

# Cargar variables de entorno desde el archivo .env en la raíz
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Configuración del servidor
PORT = int(os.getenv("PORT", "3000"))
HOST = os.getenv("HOST", "0.0.0.0")

PUBLIC_KEY_ENV_VARS: Tuple[str, ...] = (
    "SUPABASE_ANON_KEY",
    "SUPABASE_PUBLISHABLE_KEY",
    "VITE_SUPABASE_PUBLISHABLE_KEY",
    "VITE_SUPABASE_ANON_KEY",
    "SUPABASE_KEY",
)


def is_service_role_key(key: Optional[str]) -> bool:
    """
    Detecta si una clave o token corresponde a una credencial privilegiada
    de Supabase (sb_secret_* o JWT con claim role='service_role').
    Nunca expone ni imprime el valor de la clave.
    """
    if not key or not isinstance(key, str):
        return False
    clean = key.strip()
    if not clean:
        return False
    if clean.startswith("sb_secret_"):
        return True
    if clean.count(".") == 2 and clean.startswith("eyJ"):
        try:
            payload = jwt.decode(
                clean,
                options={
                    "verify_signature": False,
                    "verify_exp": False,
                    "verify_aud": False,
                    "verify_iss": False,
                },
            )
            if isinstance(payload, dict) and str(payload.get("role") or "").strip().lower() == "service_role":
                return True
        except Exception:
            return False
    return False


def is_valid_public_supabase_key(key: Optional[str]) -> bool:
    """
    Valida que una clave pública de Supabase sea exclusivamente `anon` (JWT con role='anon')
    o una clave publishable (`sb_publishable_*`), rechazando cualquier clave secreta/service_role.
    """
    if not key or not isinstance(key, str):
        return False
    clean = key.strip()
    if not clean or is_service_role_key(clean):
        return False
    if clean.startswith("sb_publishable_"):
        return True
    if clean.count(".") == 2 and clean.startswith("eyJ"):
        try:
            payload = jwt.decode(
                clean,
                options={
                    "verify_signature": False,
                    "verify_exp": False,
                    "verify_aud": False,
                    "verify_iss": False,
                },
            )
            return isinstance(payload, dict) and str(payload.get("role") or "").strip().lower() == "anon"
        except Exception:
            return False
    return False


def resolve_supabase_anon_key(
    env: Optional[Mapping[str, str]] = None,
    *,
    raise_on_secret: bool = True,
) -> Tuple[str, List[str]]:
    """
    Resuelve la clave pública de Supabase (`SUPABASE_ANON_KEY`) SIN ningún fallback hacia claves
    secretas (`sb_secret_*` / `service_role`).
    - Detecta claves secretas ubicadas indebidamente en `SUPABASE_ANON_KEY` o variables públicas/`VITE_*`.
    - Nunca incluye el valor de ninguna clave en los mensajes de error.
    """
    source = env if env is not None else os.environ
    secret_violations: List[str] = []

    for env_name in PUBLIC_KEY_ENV_VARS:
        raw_val = (source.get(env_name) or "").strip()
        if raw_val and is_service_role_key(raw_val):
            secret_violations.append(env_name)

    if secret_violations and raise_on_secret:
        vars_list = ", ".join(secret_violations)
        raise RuntimeError(
            f"Configuración de seguridad inválida: se detectó una clave secreta/service_role en "
            f"{vars_list}. Configure SUPABASE_ANON_KEY exclusivamente con la clave pública "
            "(anon o sb_publishable_*) y elimine claves secretas de variables públicas o VITE_*."
        )

    for env_name in PUBLIC_KEY_ENV_VARS:
        raw_val = (source.get(env_name) or "").strip()
        if raw_val and not is_service_role_key(raw_val):
            return raw_val, secret_violations

    return "", secret_violations


# 1. URL y Claves de Supabase (CERO fallback hacia claves secretas/service_role)
SUPABASE_URL = (os.getenv("SUPABASE_URL") or os.getenv("VITE_SUPABASE_URL") or "").strip().rstrip("/")

# Si SUPABASE_ANON_KEY directamente contiene una clave secreta, detener inmediatamente la inicialización.
_direct_anon_raw = (os.getenv("SUPABASE_ANON_KEY") or "").strip()
if _direct_anon_raw and is_service_role_key(_direct_anon_raw):
    raise RuntimeError(
        "Configuración de seguridad inválida: SUPABASE_ANON_KEY contiene una clave privilegiada "
        "(service_role / sb_secret_*). Configure SUPABASE_ANON_KEY exclusivamente con una clave pública "
        "(anon o sb_publishable_*)."
    )

SUPABASE_ANON_KEY, SUPABASE_PUBLIC_KEY_SECRET_VIOLATIONS = resolve_supabase_anon_key(
    os.environ, raise_on_secret=False
)
SUPABASE_ANON_KEY_IS_PRIVILEGED: bool = False  # Garantizado: jamás se asigna una clave secreta

if SUPABASE_PUBLIC_KEY_SECRET_VIOLATIONS:
    _viol_names = ", ".join(SUPABASE_PUBLIC_KEY_SECRET_VIOLATIONS)
    print(
        f"[ERROR DE SEGURIDAD] Se detectó una clave secreta/service_role en {_viol_names}. "
        "Fue rechazada automáticamente (sin fallback). Configure SUPABASE_ANON_KEY con la clave "
        "pública (anon / sb_publishable_*) y elimine claves secretas de variables públicas o VITE_*."
    )

# Clave administrativa aislada (SOLO backend: operaciones Auth admin y tablas privadas sin permisos a anon/authenticated como `public.integracion_pago_gimnasio`)
_raw_service_key = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or os.getenv("SUPABASE_SECRET_KEY")
    or ""
).strip()
SUPABASE_SERVICE_ROLE_KEY_INVALID: bool = bool(_raw_service_key and not is_service_role_key(_raw_service_key))
SUPABASE_SERVICE_ROLE_KEY: str = _raw_service_key if is_service_role_key(_raw_service_key) else ""

if SUPABASE_SERVICE_ROLE_KEY_INVALID:
    print(
        "[ADVERTENCIA DE SEGURIDAD] SUPABASE_SERVICE_ROLE_KEY / SUPABASE_SECRET_KEY no tiene un "
        "formato administrativo válido (service_role JWT o sb_secret_*). El cliente administrativo "
        "no se inicializará."
    )

# Alias de compatibilidad (siempre clave pública o vacío; jamás una clave secreta)
SUPABASE_KEY = SUPABASE_ANON_KEY

if not SUPABASE_URL or not SUPABASE_ANON_KEY:
    print(
        "[ADVERTENCIA] Falta configuración pública válida de Supabase "
        "(SUPABASE_URL y SUPABASE_ANON_KEY pública en .env)."
    )

# 2. Configuración de verificación criptográfica de JWT de Supabase Auth
SUPABASE_JWT_ISSUER: str = (
    os.getenv("SUPABASE_JWT_ISSUER")
    or (f"{SUPABASE_URL}/auth/v1" if SUPABASE_URL else "")
).strip().rstrip("/")

SUPABASE_JWT_AUDIENCE: str = (os.getenv("SUPABASE_JWT_AUDIENCE") or "authenticated").strip()

SUPABASE_JWKS_URL: str = (
    os.getenv("SUPABASE_JWKS_URL")
    or (f"{SUPABASE_JWT_ISSUER}/.well-known/jwks.json" if SUPABASE_JWT_ISSUER else "")
).strip()

SUPABASE_JWT_SECRET: str = (os.getenv("SUPABASE_JWT_SECRET") or "").strip()
SUPABASE_JWT_PUBLIC_KEY: str = (os.getenv("SUPABASE_JWT_PUBLIC_KEY") or "").strip()

_raw_algs = os.getenv("SUPABASE_JWT_ALGORITHMS", "ES256,RS256,HS256")
SUPABASE_JWT_ALGORITHMS: List[str] = [
    alg.strip().upper() for alg in _raw_algs.split(",") if alg.strip()
] or ["ES256", "RS256", "HS256"]

# 3. Clientes de Supabase con pool compartido de conexiones TCP/TLS
_SHARED_HTTP_LIMITS = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=30,
    keepalive_expiry=60.0,
)
_SHARED_HTTP_TRANSPORT = httpx.HTTPTransport(limits=_SHARED_HTTP_LIMITS, retries=1)


class _PooledHTTPTransport(httpx.BaseTransport):
    """Wrapper sobre el transporte HTTP compartido que evita cerrar el pool global."""

    def __init__(self, transport: httpx.BaseTransport):
        self._transport = transport

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._transport.handle_request(request)

    def close(self) -> None:
        # Conserva el pool keep-alive compartido vivo entre requests.
        return None


def _build_pooled_httpx_client() -> httpx.Client:
    return httpx.Client(
        transport=_PooledHTTPTransport(_SHARED_HTTP_TRANSPORT),
        timeout=httpx.Timeout(15.0, connect=5.0),
    )


def _build_common_client_options() -> ClientOptions:
    return ClientOptions(
        auto_refresh_token=False,
        persist_session=False,
        httpx_client=_build_pooled_httpx_client(),
    )


supabase_anon: Optional[Client] = None
if SUPABASE_URL and SUPABASE_ANON_KEY and not is_service_role_key(SUPABASE_ANON_KEY):
    try:
        supabase_anon = create_client(
            SUPABASE_URL,
            SUPABASE_ANON_KEY,
            options=_build_common_client_options(),
        )
    except Exception as e:
        print(f"[ERROR] No se pudo inicializar el cliente público de Supabase: {e}")

# Cliente administrativo estrictamente aislado (no se expone en consultas de negocio ni en auth público)
supabase_admin: Optional[Client] = None
if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    try:
        supabase_admin = create_client(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            options=_build_common_client_options(),
        )
    except Exception as e:
        print(f"[ERROR] No se pudo inicializar el cliente administrativo de Supabase: {e}")

# Alias conservado únicamente para /api/health (siempre cliente público o None)
supabase: Optional[Client] = supabase_anon


def validate_supabase_configuration() -> None:
    """
    Valida la configuración pública de Supabase al inicializar el servidor o crear sesiones.
    Detiene la inicialización con RuntimeError explícito si existe alguna clave secreta en
    variables públicas/VITE_* o si falta una clave pública válida.
    """
    if SUPABASE_PUBLIC_KEY_SECRET_VIOLATIONS:
        vars_list = ", ".join(SUPABASE_PUBLIC_KEY_SECRET_VIOLATIONS)
        raise RuntimeError(
            f"Inicialización detenida por seguridad: se encontró una clave secreta/service_role en "
            f"{vars_list}. Configure SUPABASE_ANON_KEY con la clave pública (anon / sb_publishable_*) "
            "y retire claves secretas de variables públicas o VITE_*."
        )
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise RuntimeError(
            "Configuración incompleta de Supabase: se requieren SUPABASE_URL y una SUPABASE_ANON_KEY pública válida."
        )
    if is_service_role_key(SUPABASE_ANON_KEY):
        raise RuntimeError(
            "Inicialización detenida por seguridad: SUPABASE_ANON_KEY no puede contener una clave secreta/service_role."
        )


def has_service_role() -> bool:
    """Indica si el cliente administrativo aislado está configurado."""
    return supabase_admin is not None


def create_supabase_anon_client() -> Client:
    """
    Crea un cliente público (`anon`) aislado para una operación puntual de Supabase Auth
    (`sign_up`, `sign_in_with_password`, `refresh_session`, `reset_password_for_email`).
    Evita reutilizar el singleton `supabase_anon`, ya que `supabase-py` muta
    `options.headers["Authorization"]` y `auth._headers["Authorization"]` al recibir
    eventos `SIGNED_IN`, lo que podría contaminar llamadas concurrentes o posteriores.
    """
    validate_supabase_configuration()
    return create_client(
        SUPABASE_URL,
        SUPABASE_ANON_KEY,
        options=_build_common_client_options(),
    )


def get_supabase_admin_client() -> Client:
    """
    Retorna el cliente administrativo de Supabase (`service_role` / `sb_secret_*`)
    exclusivo para operaciones privadas de servidor (p. ej. `public.integracion_pago_gimnasio`).
    Nunca realiza fallback al cliente público ni expone valores de claves.
    """
    if supabase_admin is not None:
        return supabase_admin
    if SUPABASE_SERVICE_ROLE_KEY_INVALID:
        raise RuntimeError(
            "La variable SUPABASE_SERVICE_ROLE_KEY del servidor no contiene una clave "
            "administrativa válida (service_role JWT o sb_secret_*)."
        )
    raise RuntimeError(
        "Falta configurar la variable privada SUPABASE_SERVICE_ROLE_KEY en el servidor "
        "para gestionar integraciones privadas de pago."
    )


def create_supabase_client_for_token(access_token: str) -> Client:
    """
    Crea un cliente de Supabase acotado al access_token de Supabase Auth del usuario.
    Garantiza que PostgreSQL ejecute todas las consultas y RPCs bajo el rol `authenticated`
    y evalúe `auth.uid()` respetando estrictamente las políticas RLS.
    Rechaza cualquier intento de operar con claves secretas/service_role.
    """
    if not access_token or not isinstance(access_token, str) or not access_token.strip():
        raise ValueError("Se requiere un access_token de Supabase Auth válido para crear el cliente de sesión.")

    clean_token = access_token.strip()
    if is_service_role_key(clean_token):
        raise PermissionError(
            "Violación de seguridad: no se permite utilizar una clave service_role como token de sesión de usuario."
        )

    validate_supabase_configuration()

    options = ClientOptions(
        headers={"Authorization": f"Bearer {clean_token}"},
        auto_refresh_token=False,
        persist_session=False,
        httpx_client=_build_pooled_httpx_client(),
    )
    client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY, options=options)
    try:
        client.postgrest.auth(clean_token)
    except Exception:
        pass
    return client


# 4. Pasarelas de Pago y Frontend
MERCADOPAGO_ACCESS_TOKEN = (
    os.getenv("MERCADOPAGO_ACCESS_TOKEN")
    or os.getenv("Token_Mer")
    or os.getenv("Api_MER")
    or ""
)
# OAuth oficial de Mercado Pago (solo credenciales privadas de servidor)
MERCADOPAGO_CLIENT_ID = (
    os.getenv("MERCADOPAGO_CLIENT_ID")
    or os.getenv("MP_CLIENT_ID")
    or ""
).strip()
MERCADOPAGO_CLIENT_SECRET = (
    os.getenv("MERCADOPAGO_CLIENT_SECRET")
    or os.getenv("MP_CLIENT_SECRET")
    or ""
).strip()
MERCADOPAGO_REDIRECT_URI = (
    os.getenv("MERCADOPAGO_REDIRECT_URI")
    or os.getenv("MP_REDIRECT_URI")
    or ""
).strip()
MERCADOPAGO_OAUTH_AUTH_URL = (
    os.getenv("MERCADOPAGO_OAUTH_AUTH_URL")
    or "https://auth.mercadopago.com/authorization"
).strip().rstrip("/")
MERCADOPAGO_OAUTH_TOKEN_URL = (
    os.getenv("MERCADOPAGO_OAUTH_TOKEN_URL")
    or "https://api.mercadopago.com/oauth/token"
).strip()
MERCADOPAGO_TOKEN_ENCRYPTION_KEY = (
    os.getenv("MERCADOPAGO_TOKEN_ENCRYPTION_KEY")
    or os.getenv("MP_TOKEN_ENCRYPTION_KEY")
    or ""
).strip()
MERCADOPAGO_STATE_SECRET = (
    os.getenv("MERCADOPAGO_STATE_SECRET")
    or os.getenv("MP_STATE_SECRET")
    or ""
).strip()

# Datos no sensibles de cuenta de prueba y modo simulación para presentaciones/demos
MP_VENDEDOR_USER_ID = (os.getenv("MP_VENDEDOR_USER_ID") or "").strip()
MP_VENDEDOR_USUARIO = (os.getenv("MP_VENDEDOR_USUARIO") or "").strip()
_raw_mp_demo = os.getenv("MERCADOPAGO_OAUTH_DEMO_MODE")
if _raw_mp_demo is None:
    # Habilitado por defecto en desarrollo/presentación cuando hay cuenta de prueba configurada y no OAuth de producción
    MERCADOPAGO_OAUTH_DEMO_MODE = bool(
        (MERCADOPAGO_ACCESS_TOKEN or MP_VENDEDOR_USER_ID)
        and not (MERCADOPAGO_CLIENT_ID and MERCADOPAGO_CLIENT_SECRET)
    )
else:
    MERCADOPAGO_OAUTH_DEMO_MODE = _raw_mp_demo.strip().lower() in ("1", "true", "yes", "si", "on")

UALA_CLIENT_ID = os.getenv("UALA_CLIENT_ID", "")
UALA_CLIENT_SECRET = os.getenv("UALA_CLIENT_SECRET", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173").strip().rstrip("/")
