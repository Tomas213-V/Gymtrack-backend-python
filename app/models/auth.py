from typing import Optional, Any
from pydantic import BaseModel, EmailStr, Field


class RegisterOwnerRequest(BaseModel):
    nombreGimnasio: Optional[str] = None
    direccion: Optional[str] = None
    telefono: Optional[str] = None
    emailGimnasio: Optional[str] = None
    nombre: str
    apellido: str
    email: EmailStr
    contrasena: str = Field(..., min_length=1)


class LoginRequest(BaseModel):
    email: Optional[str] = None
    contrasena: Optional[str] = None
    password: Optional[str] = None


class LoginSocioRequest(BaseModel):
    email: Optional[str] = None
    dni: Optional[Any] = None
    contrasena: Optional[str] = None
    password: Optional[str] = None


class UpdateOwnerProfileRequest(BaseModel):
    nombre: Optional[str] = None
    apellido: Optional[str] = None
    email: Optional[str] = None
    contrasena_actual: Optional[str] = None
    nueva_contrasena: Optional[str] = None
