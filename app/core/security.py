from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
import re
import bcrypt
import jwt
from fastapi import HTTPException, status
from app.config import JWT_SECRET, JWT_ALGORITHM, JWT_EXPIRES_IN


def parse_duration_string(duration: str) -> timedelta:
    """Parsea cadenas como '24h', '7d', '60m', '3600s' a timedelta."""
    match = re.match(r"^(\d+)([hdms])$", duration.strip().lower())
    if not match:
        return timedelta(hours=24)
    amount, unit = match.groups()
    amount = int(amount)
    if unit == "h":
        return timedelta(hours=amount)
    elif unit == "d":
        return timedelta(days=amount)
    elif unit == "m":
        return timedelta(minutes=amount)
    elif unit == "s":
        return timedelta(seconds=amount)
    return timedelta(hours=24)


def verify_password(plain_password: str, hashed_password: Optional[str]) -> bool:
    """Verifica si la contraseña coincide con el hash en formato bcrypt o fallback directo."""
    if not hashed_password:
        return False
    try:
        if bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8")):
            return True
    except Exception:
        pass
    # Fallback de contingencia idéntico al backend de Node.js
    return plain_password == hashed_password


def get_password_hash(password: str) -> str:
    """Genera hash bcrypt de la contraseña con salt rounds = 10."""
    salt = bcrypt.gensalt(rounds=10)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    """Genera un JWT firmado con los datos proporcionados."""
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + parse_duration_string(JWT_EXPIRES_IN)
    to_encode.update({"exp": expire, "iat": now})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> Dict[str, Any]:
    """Decodifica y valida el token JWT."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="La sesión ha expirado. Por favor, inicia sesión nuevamente."
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o corrupto."
        )
