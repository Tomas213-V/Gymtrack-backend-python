import re
from typing import Optional
from pydantic import BaseModel, Field, field_validator

EMAIL_REGEX = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _validate_optional_email(val: Optional[str]) -> Optional[str]:
    if val is None:
        return None
    clean = str(val).strip().lower()
    if not clean:
        return None
    if not EMAIL_REGEX.match(clean):
        raise ValueError("El formato del email ingresado no es válido.")
    return clean


class GimnasioCreate(BaseModel):
    """
    Contrato de creación de un gimnasio (`public.gimnasio`).
    - Pertenece al dueño autenticado (`id_dueno`).
    - La dirección física pertenece a `public.sucursal`, no a `public.gimnasio`.
    """
    nombre: str = Field(..., min_length=1, max_length=120)
    telefono: Optional[str] = Field(default=None, max_length=30)
    email: Optional[str] = None
    estado: bool = True
    # Si el cliente envía id_dueno, se valida estrictamente contra el dueño autenticado (anti-spoofing)
    id_dueno: Optional[int] = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        return _validate_optional_email(v)


class GimnasioUpdate(BaseModel):
    """
    Contrato de actualización de `public.gimnasio`.
    Respeta el column-level GRANT de PostgreSQL: `update (nombre, telefono, email, estado)`.
    """
    nombre: Optional[str] = Field(default=None, max_length=120)
    telefono: Optional[str] = Field(default=None, max_length=30)
    email: Optional[str] = None
    estado: Optional[bool] = None
    id_dueno: Optional[int] = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        return _validate_optional_email(v)


class GimnasioEstadoUpdate(BaseModel):
    estado: bool
