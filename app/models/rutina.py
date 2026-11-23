from datetime import date
from typing import Optional, Any, List
from pydantic import BaseModel, ConfigDict, Field, field_validator


def _validate_iso_date(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    clean = str(v).strip().split("T")[0]
    if not clean:
        return None
    date.fromisoformat(clean)
    return clean


# ==============================================================================
# RUTINAS PERSONALES DEL SOCIO (`public.rutina_socio` / `public.rutina_ejercicio_socio`)
# ==============================================================================

class RutinaSocioCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nombre: str = Field(..., min_length=1, max_length=120)
    descripcion: Optional[str] = None
    fecha_inicio: Optional[str] = None
    fecha_fin: Optional[str] = None
    estado: bool = True
    # Campo protegido para evitar crear rutinas personales en nombre de otro socio
    id_socio: Optional[Any] = None

    @field_validator("fecha_inicio", "fecha_fin")
    @classmethod
    def validate_dates(cls, v: Optional[str]) -> Optional[str]:
        return _validate_iso_date(v)


class RutinaSocioUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nombre: Optional[str] = Field(default=None, min_length=1, max_length=120)
    descripcion: Optional[str] = None
    fecha_inicio: Optional[str] = None
    fecha_fin: Optional[str] = None
    estado: Optional[bool] = None
    id_socio: Optional[Any] = None

    @field_validator("fecha_inicio", "fecha_fin")
    @classmethod
    def validate_dates(cls, v: Optional[str]) -> Optional[str]:
        return _validate_iso_date(v)


class RutinaEjercicioSocioAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id_ejercicio_socio: int = Field(..., gt=0)
    series: int = Field(default=4, gt=0)
    repeticiones: int = Field(default=12, gt=0)
    peso: Optional[float] = Field(default=0.0, ge=0)
    descanso: Optional[int] = Field(default=60, ge=0)
    orden: int = Field(..., gt=0)
    # Campo protegido para impedir mezclar ejercicio_gimnasio en rutina_socio
    id_ejercicio_gimnasio: Optional[Any] = None


class RutinaEjercicioUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series: Optional[int] = Field(default=None, gt=0)
    repeticiones: Optional[int] = Field(default=None, gt=0)
    peso: Optional[float] = Field(default=None, ge=0)
    descanso: Optional[int] = Field(default=None, ge=0)
    orden: Optional[int] = Field(default=None, gt=0)


# ==============================================================================
# RUTINAS DEL GIMNASIO (`public.rutina_gimnasio` / `public.rutina_ejercicio_gimnasio`)
# ==============================================================================

class RutinaGimnasioCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nombre: str = Field(..., min_length=1, max_length=120)
    descripcion: Optional[str] = None
    nivel: Optional[str] = Field(default=None, max_length=40)
    estado: bool = True
    id_gimnasio: Optional[int] = Field(default=None, gt=0)


class RutinaGimnasioUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nombre: Optional[str] = Field(default=None, min_length=1, max_length=120)
    descripcion: Optional[str] = None
    nivel: Optional[str] = Field(default=None, max_length=40)
    estado: Optional[bool] = None
    # Campo protegido para impedir transferir rutina a otro gimnasio
    id_gimnasio: Optional[Any] = None


class RutinaEjercicioGimnasioAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id_ejercicio_gimnasio: int = Field(..., gt=0)
    series: int = Field(default=4, gt=0)
    repeticiones: int = Field(default=12, gt=0)
    peso: Optional[float] = Field(default=0.0, ge=0)
    descanso: Optional[int] = Field(default=60, ge=0)
    orden: int = Field(..., gt=0)
    # Campo protegido para impedir mezclar ejercicio_socio en rutina_gimnasio
    id_ejercicio_socio: Optional[Any] = None


class RutinaReordenarRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids_ordenados: List[int] = Field(..., min_length=1)


# ==============================================================================
# ASIGNACIONES DE RUTINAS DEL GIMNASIO (`public.rutinas_asignadas_socio_gimnasio`)
# ==============================================================================

class RutinaAsignarRequest(BaseModel):
    """Invoca `public.rpc_asignar_rutina_gimnasio_socio`."""
    model_config = ConfigDict(extra="forbid")

    id_rutina_gimnasio: int = Field(..., gt=0)
    id_socio_gimnasio: int = Field(..., gt=0)
    fecha_finalizacion: Optional[str] = None
    observaciones: Optional[str] = None
    id_empleado_responsable: Optional[int] = Field(default=None, gt=0)

    @field_validator("fecha_finalizacion")
    @classmethod
    def validate_fecha_finalizacion(cls, v: Optional[str]) -> Optional[str]:
        return _validate_iso_date(v)


class RutinaReasignarResponsableRequest(BaseModel):
    """Invoca `public.rpc_reasignar_responsable_rutina_asignada`."""
    model_config = ConfigDict(extra="forbid")

    id_empleado_responsable: int = Field(..., gt=0)
    observaciones: Optional[str] = None


class RutinaFinalizarAsignacionRequest(BaseModel):
    """Invoca `public.rpc_finalizar_rutina_asignada` conservando el responsable histórico."""
    model_config = ConfigDict(extra="forbid")

    fecha_finalizacion: Optional[str] = None
    observaciones: Optional[str] = None

    @field_validator("fecha_finalizacion")
    @classmethod
    def validate_fecha_finalizacion(cls, v: Optional[str]) -> Optional[str]:
        return _validate_iso_date(v)
