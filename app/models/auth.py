from typing import Optional
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
    email: str
    contrasena: Optional[str] = None
    password: Optional[str] = None


class LoginSocioRequest(BaseModel):
    email: str
    dni: str
    contrasena: Optional[str] = None
    password: Optional[str] = None
