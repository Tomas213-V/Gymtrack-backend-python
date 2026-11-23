from typing import Optional, Any
from pydantic import BaseModel, ConfigDict, Field


class EjercicioGimnasioCreate(BaseModel):
    """Contrato para crear un ejercicio organizacional en `public.ejercicio_gimnasio`."""
    model_config = ConfigDict(extra="forbid")

    nombre: str = Field(..., min_length=1, max_length=120)
    descripcion: Optional[str] = None
    grupo_muscular: Optional[str] = Field(default=None, max_length=60)
    dificultad: Optional[str] = Field(default=None, max_length=30)
    estado: bool = True
    id_gimnasio: Optional[int] = Field(default=None, gt=0)


class EjercicioGimnasioUpdate(BaseModel):
    """Contrato para actualizar un ejercicio de `public.ejercicio_gimnasio` sin permitir transferencia de gimnasio."""
    model_config = ConfigDict(extra="forbid")

    nombre: Optional[str] = Field(default=None, min_length=1, max_length=120)
    descripcion: Optional[str] = None
    grupo_muscular: Optional[str] = Field(default=None, max_length=60)
    dificultad: Optional[str] = Field(default=None, max_length=30)
    estado: Optional[bool] = None
    # Campo protegido capturado explícitamente para rechazo 403 si se intenta transferir gimnasio
    id_gimnasio: Optional[Any] = None


class EjercicioSocioCreate(BaseModel):
    """Contrato para crear un ejercicio personal en `public.ejercicio_socio`."""
    model_config = ConfigDict(extra="forbid")

    nombre: str = Field(..., min_length=1, max_length=120)
    descripcion: Optional[str] = None
    grupo_muscular: Optional[str] = Field(default=None, max_length=60)
    dificultad: Optional[str] = Field(default=None, max_length=30)
    estado: bool = True
    # Campo protegido: un socio no puede crear ejercicios personales para otro socio
    id_socio: Optional[Any] = None


class EjercicioSocioUpdate(BaseModel):
    """Contrato para actualizar un ejercicio personal de `public.ejercicio_socio`."""
    model_config = ConfigDict(extra="forbid")

    nombre: Optional[str] = Field(default=None, min_length=1, max_length=120)
    descripcion: Optional[str] = None
    grupo_muscular: Optional[str] = Field(default=None, max_length=60)
    dificultad: Optional[str] = Field(default=None, max_length=30)
    estado: Optional[bool] = None
    # Campo protegido capturado para rechazo 403
    id_socio: Optional[Any] = None


# Alias compatibles para importaciones internas existentes
EjercicioCreate = EjercicioGimnasioCreate
EjercicioPersonalizadoCreate = EjercicioSocioCreate
EjercicioUpdate = EjercicioGimnasioUpdate
