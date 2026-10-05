from typing import Optional
from pydantic import BaseModel, Field


class RutinaCreate(BaseModel):
    nombre: str = Field(..., min_length=1)
    descripcion: Optional[str] = None
    id_socio: Optional[int] = None
    fecha_inicio: Optional[str] = None
    fecha_fin: Optional[str] = None
    estado: Optional[str] = "asignada"


class RutinaUpdate(BaseModel):
    nombre: Optional[str] = None
    descripcion: Optional[str] = None
    id_socio: Optional[int] = None
    fecha_inicio: Optional[str] = None
    fecha_fin: Optional[str] = None
    estado: Optional[str] = None


class RutinaEjercicioAdd(BaseModel):
    id_ejercicio: int
    series: Optional[int] = None
    serie: Optional[int] = None
    repeticiones: Optional[int] = 12
    peso: Optional[float] = 0.0
    descanso: Optional[int] = 60
    orden: Optional[int] = 1


class RutinaEjercicioUpdate(BaseModel):
    series: Optional[int] = None
    serie: Optional[int] = None
    repeticiones: Optional[int] = None
    peso: Optional[float] = None
    descanso: Optional[int] = None
    orden: Optional[int] = None
