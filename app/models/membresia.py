from typing import Optional
from pydantic import BaseModel


class MembresiaCreate(BaseModel):
    id_socio: int
    id_plan_membresia: int
    fecha_inicio: Optional[str] = None
    dias_adicionales: Optional[int] = 0
    estado: Optional[str] = "activo"
    precio: Optional[float] = None


class MembresiaUpdate(BaseModel):
    estado: Optional[str] = None
    fecha_inicio: Optional[str] = None
    fecha_vencimiento: Optional[str] = None
    dias_extension: Optional[int] = None
    precio: Optional[float] = None
