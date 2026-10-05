from typing import Optional, Dict, Any, List
from fastapi import Request, HTTPException, status, Depends
from app.core.security import decode_access_token


def get_token_from_header(request: Request) -> Optional[str]:
    """Extrae el token Bearer del encabezado Authorization."""
    auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not auth_header:
        return None
    parts = auth_header.split(" ")
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


async def get_current_user(request: Request) -> Dict[str, Any]:
    """Dependency que valida obligatoriamente el token JWT."""
    auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Acceso denegado. No se proporcionó un token de autenticación válido (Bearer token)."
        )

    token = auth_header.split(" ")[1] if len(auth_header.split(" ")) > 1 else None
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token no encontrado en la cabecera de autorización."
        )

    user = decode_access_token(token)
    return user


async def get_optional_user(request: Request) -> Optional[Dict[str, Any]]:
    """Dependency que extrae el usuario si se envía token, o retorna None si no se envía."""
    token = get_token_from_header(request)
    if not token:
        return None
    try:
        return decode_access_token(token)
    except Exception:
        return None


def require_roles(*roles_permitidos: str):
    """Factory de dependency para verificar roles (dueño, administrador, socio, etc.)."""
    async def role_checker(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
        user_role = current_user.get("rol", "").lower()
        allowed = [r.lower() for r in roles_permitidos]
        if user_role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes los permisos necesarios para realizar esta acción."
            )
        return current_user
    return role_checker
