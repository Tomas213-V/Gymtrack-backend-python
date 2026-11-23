import re
from typing import Optional
from pydantic import BaseModel, Field, field_validator

EMAIL_REGEX = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class RegistrarAdministradorGimnasioRequest(BaseModel):
    """
    Contrato para registrar o vincular un administrador organizacional a un gimnasio
    mediante la RPC oficial `public.rpc_registrar_administrador_gimnasio`.
    """
    id_gimnasio: Optional[int] = None
    nombre: str = Field(..., min_length=1, max_length=80)
    apellido: str = Field(..., min_length=1, max_length=80)
    email: str = Field(..., min_length=3, max_length=254)
    dni: Optional[str] = Field(default=None, max_length=20)
    telefono: Optional[str] = Field(default=None, max_length=30)

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        clean = str(v or "").strip().lower()
        if not EMAIL_REGEX.match(clean):
            raise ValueError("El formato del email del administrador no es válido.")
        return clean


class AdministradorUpdate(BaseModel):
    """
    Contrato de actualización de datos personales en `public.administrador`.
    Respeta el column-level GRANT: `update (nombre, apellido, dni, telefono, email, estado)`.
    """
    nombre: Optional[str] = Field(default=None, max_length=80)
    apellido: Optional[str] = Field(default=None, max_length=80)
    dni: Optional[str] = Field(default=None, max_length=20)
    telefono: Optional[str] = Field(default=None, max_length=30)
    email: Optional[str] = Field(default=None, max_length=254)
    estado: Optional[bool] = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        clean = str(v).strip().lower()
        if not clean:
            raise ValueError("El email no puede estar vacío.")
        if not EMAIL_REGEX.match(clean):
            raise ValueError("El formato del email no es válido.")
        return clean


class AsignacionAdministradorGimnasioUpdate(BaseModel):
    """
    Contrato para activar o desactivar una relación `public.administrador_gimnasio`.
    Respeta `trg_proteger_admin_gimnasio_cambio` y `grant update (estado) on public.administrador_gimnasio`.
    """
    estado: bool
    id_administrador: Optional[int] = None
    id_gimnasio: Optional[int] = None
