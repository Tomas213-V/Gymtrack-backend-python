from typing import Optional
from pydantic import BaseModel, Field


class SucursalCreate(BaseModel):
    """
    Contrato de creación de una sucursal (`public.sucursal`).
    La ubicación física (`direccion`, `telefono`, `descripcion`) pertenece a la sucursal.
    """
    direccion: str = Field(..., min_length=1, max_length=220)
    telefono: Optional[str] = Field(default=None, max_length=30)
    descripcion: Optional[str] = None
    estado: bool = True
    id_gimnasio: Optional[int] = None


class SucursalUpdate(BaseModel):
    """
    Contrato de actualización de una sucursal (`public.sucursal`).
    `id_gimnasio` es inmutable una vez creada la sucursal (`trg_proteger_sucursal_gimnasio_cambio`).
    """
    direccion: Optional[str] = Field(default=None, max_length=220)
    telefono: Optional[str] = Field(default=None, max_length=30)
    descripcion: Optional[str] = None
    estado: Optional[bool] = None
    id_gimnasio: Optional[int] = None


class SucursalEstadoUpdate(BaseModel):
    estado: bool
