from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional, Dict, Any, Sequence
import uuid
import jwt
from jwt import PyJWKClient
from fastapi import HTTPException, status

from app.config import (
    SUPABASE_JWT_ISSUER,
    SUPABASE_JWT_AUDIENCE,
    SUPABASE_JWKS_URL,
    SUPABASE_JWT_SECRET,
    SUPABASE_JWT_PUBLIC_KEY,
    SUPABASE_JWT_ALGORITHMS,
    is_service_role_key,
)

ALLOWED_ASYMMETRIC_ALGS = {"ES256", "RS256", "EdDSA"}
ALLOWED_SYMMETRIC_ALGS = {"HS256"}


@lru_cache(maxsize=4)
def get_jwk_client(jwks_url: str) -> PyJWKClient:
    """Devuelve una instancia cacheada de PyJWKClient para consultar las claves públicas de Supabase Auth."""
    return PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    """Convierte un valor ISO 8601 o timestamp a datetime UTC con zona horaria."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw or raw.startswith("0001-01-01"):
            return None
        try:
            clean = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def is_auth_user_disabled(
    claims: Optional[Dict[str, Any]] = None,
    auth_user: Optional[Any] = None,
) -> bool:
    """
    Determina si un usuario de Supabase Auth se encuentra deshabilitado, baneado o eliminado,
    evaluando tanto los claims del token como el objeto de usuario de Supabase Auth.
    """
    now = datetime.now(timezone.utc)
    claims = claims or {}

    app_meta = claims.get("app_metadata") if isinstance(claims.get("app_metadata"), dict) else {}
    user_meta = claims.get("user_metadata") if isinstance(claims.get("user_metadata"), dict) else {}

    # 1. Flags explícitos en claims o metadata
    if (
        claims.get("disabled") is True
        or claims.get("banned") is True
        or app_meta.get("disabled") is True
        or app_meta.get("banned") is True
        or str(app_meta.get("status") or "").lower() in ("disabled", "banned", "suspended", "inactivo")
        or user_meta.get("disabled") is True
    ):
        return True

    # 2. Fechas de baneo o eliminación en claims
    banned_until_claim = _parse_iso_datetime(claims.get("banned_until") or app_meta.get("banned_until"))
    if banned_until_claim and banned_until_claim > now:
        return True

    deleted_at_claim = _parse_iso_datetime(claims.get("deleted_at") or app_meta.get("deleted_at"))
    if deleted_at_claim:
        return True

    # 3. Evaluación del objeto user de Supabase Auth (si fue consultado)
    if auth_user is not None:
        user_dict = auth_user if isinstance(auth_user, dict) else getattr(auth_user, "__dict__", {})
        u_app_meta = (
            user_dict.get("app_metadata")
            if isinstance(user_dict.get("app_metadata"), dict)
            else getattr(auth_user, "app_metadata", {}) or {}
        )
        u_user_meta = (
            user_dict.get("user_metadata")
            if isinstance(user_dict.get("user_metadata"), dict)
            else getattr(auth_user, "user_metadata", {}) or {}
        )

        if (
            user_dict.get("disabled") is True
            or u_app_meta.get("disabled") is True
            or u_app_meta.get("banned") is True
            or str(u_app_meta.get("status") or "").lower() in ("disabled", "banned", "suspended", "inactivo")
            or u_user_meta.get("disabled") is True
        ):
            return True

        banned_until_user = _parse_iso_datetime(
            user_dict.get("banned_until") or getattr(auth_user, "banned_until", None)
        )
        if banned_until_user and banned_until_user > now:
            return True

        deleted_at_user = _parse_iso_datetime(
            user_dict.get("deleted_at") or getattr(auth_user, "deleted_at", None)
        )
        if deleted_at_user:
            return True

    return False


def _resolve_signing_key(
    token: str,
    alg: str,
    verification_key: Optional[Any] = None,
    jwt_secret: Optional[str] = None,
    jwt_public_key: Optional[str] = None,
    jwks_url: Optional[str] = None,
    jwk_client: Optional[Any] = None,
) -> Any:
    """
    Resuelve la clave criptográfica real para verificar la firma del JWT de Supabase Auth.
    Nunca acepta tokens sin clave de verificación válida.
    """
    if verification_key is not None:
        return verification_key

    effective_public_key = jwt_public_key if jwt_public_key is not None else SUPABASE_JWT_PUBLIC_KEY
    effective_secret = jwt_secret if jwt_secret is not None else SUPABASE_JWT_SECRET
    effective_jwks_url = jwks_url if jwks_url is not None else SUPABASE_JWKS_URL

    if alg in ALLOWED_ASYMMETRIC_ALGS:
        if effective_public_key:
            return effective_public_key
        client = jwk_client or (get_jwk_client(effective_jwks_url) if effective_jwks_url else None)
        if client is not None:
            try:
                signing_key = client.get_signing_key_from_jwt(token)
                return signing_key.key
            except jwt.PyJWKClientError as exc:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Firma del token inválida o clave de firma no reconocida: {exc}",
                ) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="No fue posible validar la firma criptográfica del token contra JWKS.",
                ) from exc

    if alg in ALLOWED_SYMMETRIC_ALGS:
        if effective_secret:
            return effective_secret

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Configuración criptográfica no disponible para verificar el algoritmo del token.",
    )


def verify_supabase_access_token(
    token: str,
    *,
    verification_key: Optional[Any] = None,
    expected_issuer: Optional[str] = None,
    expected_audience: Optional[str] = None,
    allowed_algorithms: Optional[Sequence[str]] = None,
    jwt_secret: Optional[str] = None,
    jwt_public_key: Optional[str] = None,
    jwks_url: Optional[str] = None,
    jwk_client: Optional[Any] = None,
    auth_user: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Valida criptográficamente un access token emitido por Supabase Auth:
    1. Rechaza tokens vacíos, malformados, alg='none' o claves service_role.
    2. Verifica criptográficamente la firma según el algoritmo real (ES256/RS256 via JWKS/Public Key o HS256 via SUPABASE_JWT_SECRET).
    3. Valida expiración (exp), emisión (iat), issuer (iss), audience (aud) y claims obligatorios (sub, role).
    4. Verifica que `sub` sea un UUID válido y `role` sea `'authenticated'`.
    5. Rechaza usuarios deshabilitados o suspendidos (403 Forbidden).
    6. Devuelve la identidad Auth desacoplada del perfil de negocio.
    """
    if not token or not isinstance(token, str) or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Acceso denegado. No se proporcionó un token de autenticación válido.",
        )

    clean_token = token.strip()
    if clean_token.startswith("sb_secret_") or clean_token.startswith("sb_publishable_"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="El token proporcionado es una API key y no un access token de sesión de usuario.",
        )

    if clean_token.count(".") != 2:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token JWT malformado.",
        )

    try:
        header = jwt.get_unverified_header(clean_token)
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Cabecera del token JWT inválida o corrupta.",
        ) from exc

    alg = str(header.get("alg") or "").strip()
    effective_algs = [a.upper() for a in (allowed_algorithms or SUPABASE_JWT_ALGORITHMS)]

    if not alg or alg.lower() == "none" or alg.upper() not in effective_algs:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Algoritmo de firma del token no permitido o inválido.",
        )

    issuer = (expected_issuer if expected_issuer is not None else SUPABASE_JWT_ISSUER).strip().rstrip("/")
    audience = (expected_audience if expected_audience is not None else SUPABASE_JWT_AUDIENCE).strip()

    if not issuer:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Issuer de Supabase Auth no configurado para validar el token.",
        )

    signing_key = _resolve_signing_key(
        clean_token,
        alg=alg.upper(),
        verification_key=verification_key,
        jwt_secret=jwt_secret,
        jwt_public_key=jwt_public_key,
        jwks_url=jwks_url,
        jwk_client=jwk_client,
    )

    try:
        payload = jwt.decode(
            clean_token,
            signing_key,
            algorithms=[alg.upper()],
            audience=audience,
            issuer=issuer,
            options={
                "require": ["sub", "exp", "iat", "iss", "aud", "role"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_iss": True,
                "verify_aud": True,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="La sesión ha expirado. Por favor, inicia sesión nuevamente.",
        ) from exc
    except jwt.InvalidSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Firma del token inválida.",
        ) from exc
    except jwt.InvalidIssuerError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Issuer del token inválido o no autorizado.",
        ) from exc
    except jwt.InvalidAudienceError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Audience del token inválida.",
        ) from exc
    except jwt.MissingRequiredClaimError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token incompleto: falta el claim obligatorio '{exc.claim}'.",
        ) from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o corrupto.",
        ) from exc

    # Validar rol de Supabase Auth (debe ser estrictamente 'authenticated', nunca 'anon' ni 'service_role')
    token_role = str(payload.get("role") or "").strip().lower()
    if token_role != "authenticated" or is_service_role_key(clean_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="El token no pertenece a un usuario autenticado válido (rol de sesión inválido).",
        )

    # Validar que sub sea un UUID válido correspondiente a auth.users.id
    raw_sub = str(payload.get("sub") or "").strip()
    try:
        sub_uuid = str(uuid.UUID(raw_sub))
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Identificador de usuario (sub) inválido en el token.",
        ) from exc

    # Verificar si el usuario se encuentra deshabilitado / suspendido
    if is_auth_user_disabled(payload, auth_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tu cuenta de usuario se encuentra deshabilitada o suspendida.",
        )

    app_metadata = payload.get("app_metadata") if isinstance(payload.get("app_metadata"), dict) else {}
    user_metadata = payload.get("user_metadata") if isinstance(payload.get("user_metadata"), dict) else {}
    # No confiar jamás en `user_metadata` (editable por el cliente) como prueba de email o verificación
    email_raw = payload.get("email") or (getattr(auth_user, "email", None) if auth_user else None)
    email = str(email_raw).strip().lower() if email_raw else None

    raw_email_confirmed_at = (
        payload.get("email_confirmed_at")
        or (getattr(auth_user, "email_confirmed_at", None) if auth_user else None)
    )
    email_confirmed_at = (
        raw_email_confirmed_at.strip()
        if isinstance(raw_email_confirmed_at, str)
        else raw_email_confirmed_at
    ) or None
    email_verified = bool(email_confirmed_at)

    return {
        "auth_user_id": sub_uuid,
        "sub": sub_uuid,
        "email": email,
        "phone": payload.get("phone"),
        "role": token_role,
        "aal": payload.get("aal"),
        "session_id": payload.get("session_id"),
        "email_confirmed_at": email_confirmed_at,
        "email_verified": email_verified,
        "app_metadata": app_metadata,
        "user_metadata": user_metadata,
        "claims": payload,
        "access_token": clean_token,
    }


def decode_access_token(token: str) -> Dict[str, Any]:
    """
    Valida y decodifica un access token de Supabase Auth.
    Mantiene la firma de función por compatibilidad con dependencias existentes,
    delegando estrictamente en la validación criptográfica de Supabase Auth.
    """
    return verify_supabase_access_token(token)
