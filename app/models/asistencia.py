from typing import Optional, Any
from pydantic import BaseModel


class AsistenciaCreate(BaseModel):
    id_socio: Optional[int] = None
    dni: Optional[Any] = None
    hora_ingreso: Optional[str] = None
    estado: Optional[str] = "presente"
    id_gimnasio: Optional[int] = None


class AsistenciaCheckout(BaseModel):
    estado: Optional[str] = "completado"
    hora_salida: Optional[str] = None
