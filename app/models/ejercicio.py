from typing import Optional
from pydantic import BaseModel


class EjercicioCreate(BaseModel):
    nombre: str
    grupo_muscular: Optional[str] = None
    descripcion: Optional[str] = None
    dificultad: Optional[str] = None
    id_socio: Optional[int] = None
    id_gimnasio: Optional[int] = None


class EjercicioPersonalizadoCreate(BaseModel):
    nombre: str
    grupo_muscular: Optional[str] = None
    descripcion: Optional[str] = None
    dificultad: Optional[str] = None
    id_socio: Optional[int] = None
    id_gimnasio: Optional[int] = None


class EjercicioUpdate(BaseModel):
    nombre: Optional[str] = None
    grupo_muscular: Optional[str] = None
    descripcion: Optional[str] = None
    dificultad: Optional[str] = None

