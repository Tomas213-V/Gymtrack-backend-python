from typing import Optional, Any
from pydantic import BaseModel


class SocioCreate(BaseModel):
    nombre: str
    apellido: Optional[str] = ""
    dni: Any  # puede ser int o str
    telefono: Optional[str] = None
    email: Optional[str] = None
    contrasena: Optional[str] = None
    password: Optional[str] = None
    plan: Optional[str] = None
    precio: Optional[float] = None
    estado: Optional[str] = "activo"
    fecha_vencimiento: Optional[str] = None
    id_plan_membresia: Optional[int] = None


class SocioUpdate(BaseModel):
    nombre: Optional[str] = None
    apellido: Optional[str] = None
    dni: Optional[Any] = None
    telefono: Optional[str] = None
    email: Optional[str] = None
    estado: Optional[str] = None
    id_plan_membresia: Optional[int] = None
    plan: Optional[str] = None
    contrasena: Optional[str] = None
    password: Optional[str] = None


class SocioChangeEstado(BaseModel):
    estado: str


class SocioUpdatePerfil(BaseModel):
    id_socio: Optional[int] = None
    nombre: Optional[str] = None
    apellido: Optional[str] = None
    telefono: Optional[str] = None
    email: Optional[str] = None
    dni: Optional[Any] = None
    contrasena_actual: Optional[str] = None
    nueva_contrasena: Optional[str] = None
    contrasena: Optional[str] = None
    password: Optional[str] = None
