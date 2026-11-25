"""
Servicio de seguridad, cifrado y flujo OAuth oficial de Mercado Pago para gimnasios.

Reglas estrictas de seguridad:
- Cifrado simétrico autenticado (Fernet: AES-128-CBC + HMAC-SHA256) para `access_token`
  y `refresh_token` antes de persistir en `public.integracion_pago_gimnasio`.
- Estado OAuth (`state`) firmado con HMAC-SHA256 + nonce de un solo uso y TTL de 10 minutos
  vinculado al `auth_user_id` y al `id_gimnasio` autorizados (anti-CSRF y anti-replay).
- Jamás simula conexiones exitosas si las variables OAuth o la clave de cifrado no están configuradas.
- Jamás registra tokens ni códigos OAuth en logs ni los devuelve en respuestas HTTP.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status

import app.config as app_config

PROVEEDOR_MERCADOPAGO = "mercadopago"
_OAUTH_STATE_TTL_SECONDS = 600  # 10 minutos
_OAUTH_STATE_LOCK = threading.Lock()
_OAUTH_PENDING_STATES: Dict[str, Dict[str, Any]] = {}
_EPHEMERAL_DEMO_KEY: str = Fernet.generate_key().decode("utf-8")


def is_mercadopago_demo_mode() -> bool:
    """Indica si el modo simulación de vinculación (cuenta de prueba) está habilitado."""
    if is_mercadopago_oauth_configured():
        return False
    return bool(getattr(app_config, "MERCADOPAGO_OAUTH_DEMO_MODE", False))


def get_demo_account_public_info(gym_id: int) -> Dict[str, str]:
    """Devuelve información no sensible de la cuenta de prueba configurada para la demo."""
    user_id = str(getattr(app_config, "MP_VENDEDOR_USER_ID", "") or "").strip() or f"TEST-MP-GYM-{int(gym_id)}"
    alias = str(getattr(app_config, "MP_VENDEDOR_USUARIO", "") or "").strip() or "Cuenta de prueba Mercado Pago"
    return {
        "external_user_id": user_id,
        "alias": alias,
    }


def _get_demo_fernet_key_str() -> str:
    explicit = _get_encryption_key_str()
    if explicit:
        return explicit
    # Deriva una clave Fernet estable de 32 bytes para el entorno de presentación/demo
    seed = (
        str(getattr(app_config, "MERCADOPAGO_STATE_SECRET", "") or "")
        or str(getattr(app_config, "SUPABASE_ANON_KEY", "") or "")
        or _EPHEMERAL_DEMO_KEY
    )
    digest = hashlib.sha256(f"gymtrack:mp-demo-fernet:{seed}".encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8")


def reset_oauth_pending_states() -> None:
    """Limpia los estados OAuth en memoria (útil en tests)."""
    with _OAUTH_STATE_LOCK:
        _OAUTH_PENDING_STATES.clear()


def _get_encryption_key_str() -> str:
    return str(getattr(app_config, "MERCADOPAGO_TOKEN_ENCRYPTION_KEY", "") or "").strip()


def _get_fernet() -> Fernet:
    raw_key = _get_encryption_key_str()
    if not raw_key and is_mercadopago_demo_mode():
        raw_key = _get_demo_fernet_key_str()
    if not raw_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "La clave de cifrado de tokens de Mercado Pago (MERCADOPAGO_TOKEN_ENCRYPTION_KEY) "
                "no está configurada en el servidor."
            ),
        )
    try:
        return Fernet(raw_key.encode("utf-8"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "La variable MERCADOPAGO_TOKEN_ENCRYPTION_KEY no tiene un formato Fernet válido "
                "(32 bytes en base64 url-safe)."
            ),
        ) from exc


def _get_state_signing_secret() -> bytes:
    explicit = str(getattr(app_config, "MERCADOPAGO_STATE_SECRET", "") or "").strip()
    if explicit:
        return explicit.encode("utf-8")
    enc_key = _get_encryption_key_str()
    if not enc_key and is_mercadopago_demo_mode():
        enc_key = _get_demo_fernet_key_str()
    if not enc_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Falta configuración de seguridad OAuth (MERCADOPAGO_TOKEN_ENCRYPTION_KEY).",
        )
    # Derivación de dominio separada de la clave Fernet
    return hmac.new(enc_key.encode("utf-8"), b"gymtrack:mp-oauth-state:v1", hashlib.sha256).digest()


def _is_valid_redirect_uri(uri: str) -> bool:
    """Valida que `MERCADOPAGO_REDIRECT_URI` sea una URL HTTP(S) absoluta y sin espacios."""
    if not uri or any(ch.isspace() for ch in uri):
        return False
    parsed = urllib.parse.urlparse(uri)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def get_mercadopago_oauth_config_status() -> Dict[str, Any]:
    """
    Evalúa la configuración OAuth de Mercado Pago sin revelar jamás valores secretos.
    Devuelve exactamente qué variables faltan o tienen formato inválido.
    """
    client_id = str(getattr(app_config, "MERCADOPAGO_CLIENT_ID", "") or "").strip()
    client_secret = str(getattr(app_config, "MERCADOPAGO_CLIENT_SECRET", "") or "").strip()
    redirect_uri = str(getattr(app_config, "MERCADOPAGO_REDIRECT_URI", "") or "").strip()
    enc_key = _get_encryption_key_str()

    missing = []
    invalid = []

    if not client_id:
        missing.append("MERCADOPAGO_CLIENT_ID")
    if not client_secret:
        missing.append("MERCADOPAGO_CLIENT_SECRET")
    if not redirect_uri:
        missing.append("MERCADOPAGO_REDIRECT_URI")
    elif not _is_valid_redirect_uri(redirect_uri):
        invalid.append("MERCADOPAGO_REDIRECT_URI")

    if not enc_key:
        missing.append("MERCADOPAGO_TOKEN_ENCRYPTION_KEY")
    else:
        try:
            Fernet(enc_key.encode("utf-8"))
        except Exception:
            invalid.append("MERCADOPAGO_TOKEN_ENCRYPTION_KEY")

    mensajes = []
    if missing:
        mensajes.append(f"Faltan las variables privadas: {', '.join(missing)}.")
    if invalid:
        mensajes.append(f"Tienen formato inválido: {', '.join(invalid)}.")

    configurado = not missing and not invalid
    return {
        "configurado": configurado,
        "variables_faltantes": missing,
        "variables_invalidas": invalid,
        "mensaje": (
            f"La integración OAuth de Mercado Pago no está configurada correctamente en el servidor. {' '.join(mensajes)}"
            if mensajes
            else None
        ),
    }


def is_mercadopago_oauth_configured() -> bool:
    """Indica si todas las credenciales privadas necesarias para OAuth real están presentes y válidas."""
    return bool(get_mercadopago_oauth_config_status()["configurado"])


def require_mercadopago_oauth_configured() -> Dict[str, str]:
    """Valida que OAuth esté realmente configurado; jamás simula una conexión exitosa ni revela secretos."""
    cfg_status = get_mercadopago_oauth_config_status()
    if not cfg_status["configurado"]:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=cfg_status["mensaje"]
            or "La integración OAuth de Mercado Pago no está configurada en el servidor.",
        )

    client_id = str(getattr(app_config, "MERCADOPAGO_CLIENT_ID", "") or "").strip()
    client_secret = str(getattr(app_config, "MERCADOPAGO_CLIENT_SECRET", "") or "").strip()
    redirect_uri = str(getattr(app_config, "MERCADOPAGO_REDIRECT_URI", "") or "").strip()
    auth_url = str(getattr(app_config, "MERCADOPAGO_OAUTH_AUTH_URL", "https://auth.mercadopago.com/authorization") or "").strip()
    token_url = str(getattr(app_config, "MERCADOPAGO_OAUTH_TOKEN_URL", "https://api.mercadopago.com/oauth/token") or "").strip()

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "auth_url": auth_url,
        "token_url": token_url,
    }


def encrypt_oauth_token(plaintext_token: str) -> str:
    """Cifra un token OAuth antes de almacenarlo en base de datos."""
    clean = str(plaintext_token or "").strip()
    if not clean:
        raise ValueError("El token a cifrar no puede estar vacío.")
    fernet = _get_fernet()
    return fernet.encrypt(clean.encode("utf-8")).decode("utf-8")


def decrypt_oauth_token(ciphertext_token: str) -> str:
    """Descifra un token OAuth almacenado en base de datos (solo uso interno en servidor)."""
    clean = str(ciphertext_token or "").strip()
    if not clean:
        raise ValueError("El token cifrado no puede estar vacío.")
    fernet = _get_fernet()
    try:
        return fernet.decrypt(clean.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("No fue posible descifrar el token OAuth almacenado.") from exc


def _sign_state_payload(nonce: str, ts: int, gym_id: int, auth_user_id: str) -> str:
    secret = _get_state_signing_secret()
    msg = f"{nonce}.{ts}.{gym_id}.{auth_user_id}".encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def create_oauth_state(
    *,
    auth_user_id: str,
    id_gimnasio: int,
    rol: str,
    user_access_token: str,
    reconectar: bool = False,
) -> str:
    """
    Genera un `state` OAuth criptográfico firmado (HMAC-SHA256) con nonce de un solo uso
    vinculado a la sesión autenticada y al `id_gimnasio` autorizado.
    """
    nonce = secrets.token_urlsafe(24)
    ts = int(time.time())
    sig = _sign_state_payload(nonce, ts, int(id_gimnasio), str(auth_user_id))
    state_value = f"{nonce}.{ts}.{int(id_gimnasio)}.{sig}"

    now_mono = time.monotonic()
    with _OAUTH_STATE_LOCK:
        # Limpiar expirados para evitar acumulación en memoria
        expired_keys = [
            k for k, v in _OAUTH_PENDING_STATES.items()
            if now_mono >= v["expires_mono"]
        ]
        for k in expired_keys:
            _OAUTH_PENDING_STATES.pop(k, None)

        _OAUTH_PENDING_STATES[nonce] = {
            "auth_user_id": str(auth_user_id),
            "id_gimnasio": int(id_gimnasio),
            "rol": str(rol),
            "user_access_token": str(user_access_token),
            "reconectar": bool(reconectar),
            "ts": ts,
            "expires_mono": now_mono + _OAUTH_STATE_TTL_SECONDS,
        }

    return state_value


def consume_and_validate_oauth_state(state_value: Optional[str]) -> Dict[str, Any]:
    """
    Valida y consume (one-time use) el `state` devuelto por Mercado Pago.
    Rechaza cualquier `state` alterado, expirado, inexistente o ya utilizado (anti-CSRF / replay).
    """
    if not state_value or not isinstance(state_value, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Parámetro state OAuth faltante o inválido.",
        )

    parts = state_value.strip().split(".")
    if len(parts) != 4:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Estado OAuth inválido.",
        )

    nonce, ts_str, gym_str, provided_sig = parts
    try:
        ts = int(ts_str)
        gym_id = int(gym_str)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Estado OAuth inválido.",
        ) from exc

    now_mono = time.monotonic()
    with _OAUTH_STATE_LOCK:
        record = _OAUTH_PENDING_STATES.pop(nonce, None)

    if not record:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El estado OAuth no es válido, expiró o ya fue utilizado.",
        )

    if now_mono >= record["expires_mono"] or abs(int(time.time()) - ts) > _OAUTH_STATE_TTL_SECONDS + 30:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La sesión de autorización OAuth expiró. Iniciá la conexión nuevamente.",
        )

    if int(record["id_gimnasio"]) != gym_id or int(record["ts"]) != ts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El estado OAuth no coincide con el gimnasio autorizado.",
        )

    expected_sig = _sign_state_payload(nonce, ts, gym_id, record["auth_user_id"])
    if not hmac.compare_digest(expected_sig, provided_sig):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Firma de estado OAuth inválida.",
        )

    return record


def build_mercadopago_authorization_url(state_value: str) -> str:
    """Construye la URL oficial de autorización OAuth de Mercado Pago."""
    cfg = require_mercadopago_oauth_configured()
    params = urllib.parse.urlencode(
        {
            "client_id": cfg["client_id"],
            "response_type": "code",
            "platform_id": "mp",
            "state": state_value,
            "redirect_uri": cfg["redirect_uri"],
        }
    )
    return f"{cfg['auth_url']}?{params}"


async def exchange_mercadopago_oauth_code(code: str) -> Dict[str, Any]:
    """
    Intercambia el `code` de autorización por `access_token`, `refresh_token`, `user_id` y `expires_in`
    contra el endpoint oficial `POST https://api.mercadopago.com/oauth/token`.
    Nunca imprime ni incluye tokens ni códigos en mensajes de error.
    """
    clean_code = str(code or "").strip()
    if not clean_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Código de autorización OAuth faltante.",
        )

    cfg = require_mercadopago_oauth_configured()
    form_payload = {
        "grant_type": "authorization_code",
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "code": clean_code,
        "redirect_uri": cfg["redirect_uri"],
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                cfg["token_url"],
                data=form_payload,
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo establecer comunicación con Mercado Pago para completar la autorización.",
        ) from exc

    if resp.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Mercado Pago rechazó el código de autorización. Intentá conectar tu cuenta nuevamente.",
        )

    try:
        data = resp.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Respuesta inválida recibida desde Mercado Pago OAuth.",
        ) from exc

    access_token = str(data.get("access_token") or "").strip()
    refresh_token = str(data.get("refresh_token") or "").strip()
    external_user_id = str(data.get("user_id") or "").strip()
    expires_in = data.get("expires_in")

    if not access_token or not external_user_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Mercado Pago no devolvió los datos esperados de la cuenta autorizada.",
        )

    token_expires_at: Optional[str] = None
    if expires_in is not None:
        try:
            sec = int(expires_in)
            if sec > 0:
                token_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=sec)).isoformat()
        except (TypeError, ValueError):
            token_expires_at = None

    return {
        "access_token": access_token,
        "refresh_token": refresh_token or None,
        "external_user_id": external_user_id,
        "token_expires_at": token_expires_at,
    }
