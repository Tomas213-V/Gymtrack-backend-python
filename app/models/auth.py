from typing import Optional, Any, Literal
from pydantic import BaseModel, EmailStr, Field, model_validator


class RegisterOwnerRequest(BaseModel):
    nombreGimnasio: Optional[str] = None
    direccion: Optional[str] = None
    telefono: Optional[str] = None
    emailGimnasio: Optional[str] = None
    nombre: str = Field(..., min_length=1)
    apellido: str = Field(..., min_length=1)
    email: EmailStr
    contrasena: Optional[str] = None
    password: Optional[str] = None

    @model_validator(mode="after")
    def validate_password_present(self) -> "RegisterOwnerRequest":
        pwd = self.contrasena or self.password
        if not pwd or not str(pwd).strip():
            raise ValueError("La contraseña es obligatoria.")
        return self


class RegisterSocioRequest(BaseModel):
    nombre: str = Field(..., min_length=1)
    apellido: str = Field(..., min_length=1)
    email: EmailStr
    dni: Optional[str] = None
    telefono: Optional[str] = None
    contrasena: Optional[str] = None
    password: Optional[str] = None

    @model_validator(mode="after")
    def validate_password_present(self) -> "RegisterSocioRequest":
        pwd = self.contrasena or self.password
        if not pwd or not str(pwd).strip():
            raise ValueError("La contraseña es obligatoria.")
        return self


class LoginRequest(BaseModel):
    email: Optional[str] = None
    contrasena: Optional[str] = None
    password: Optional[str] = None
    rol_esperado: Optional[Literal["dueño", "dueno", "administrador", "empleado", "socio"]] = None


class LoginSocioRequest(BaseModel):
    email: Optional[str] = None
    dni: Optional[Any] = None
    contrasena: Optional[str] = None
    password: Optional[str] = None


class CambiarContrasenaRequest(BaseModel):
    contrasena_actual: str = Field(..., min_length=1)
    nueva_contrasena: str = Field(..., min_length=6)
    confirmar_contrasena: Optional[str] = None


class RecuperarContrasenaRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)


class RestablecerContrasenaRequest(BaseModel):
    access_token: Optional[str] = None
    token_hash: Optional[str] = None
    type: Optional[str] = "recovery"
    nueva_contrasena: str = Field(..., min_length=6)
    confirmar_contrasena: Optional[str] = None


class VerificarIdentidadRequest(BaseModel):
    tipo_perfil: Literal["socio", "empleado", "administrador"]
    dni: Optional[str] = Field(default=None, max_length=20)
    email: Optional[str] = Field(default=None, max_length=254)
    id_gimnasio: Optional[int] = Field(default=None, gt=0)
    id_sucursal: Optional[int] = Field(default=None, gt=0)


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


class VincularPerfilRequest(BaseModel):
    perfil_tipo: Literal["socio", "administrador", "empleado", "dueno", "dueño"]


class UpdateOwnerProfileRequest(BaseModel):
    nombre: Optional[str] = None
    apellido: Optional[str] = None
    email: Optional[str] = None
    telefono: Optional[str] = None
    dni: Optional[str] = None
    contrasena_actual: Optional[str] = None
    nueva_contrasena: Optional[str] = None
