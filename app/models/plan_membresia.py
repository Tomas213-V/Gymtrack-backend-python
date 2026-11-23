from typing import Optional, Any
from pydantic import BaseModel, ConfigDict, Field


class PlanMembresiaCreate(BaseModel):
    """
    Contrato de creación de un plan en `public.membresia_gimnasio`.
    - El gimnasio destino se valida estrictamente contra el contexto autorizado.
    - Rechaza campos desconocidos (`extra='forbid'`).
    """
    model_config = ConfigDict(extra="forbid")

    nombre: str = Field(..., min_length=1, max_length=100)
    descripcion: Optional[str] = None
    precio: float = Field(..., ge=0)
    duracion_dias: int = Field(..., gt=0)
    limite_asistencias_semanales: Optional[int] = Field(default=None, gt=0)
    limite_asistencias_totales: Optional[int] = Field(default=None, gt=0)
    estado: bool = True
    id_gimnasio: Optional[int] = Field(default=None, gt=0)


class PlanMembresiaUpdate(BaseModel):
    """
    Contrato de actualización de un plan en `public.membresia_gimnasio`.
    No permite reasignar el plan a otro gimnasio (`id_gimnasio` protegido).
    """
    model_config = ConfigDict(extra="forbid")

    nombre: Optional[str] = Field(default=None, min_length=1, max_length=100)
    descripcion: Optional[str] = None
    precio: Optional[float] = Field(default=None, ge=0)
    duracion_dias: Optional[int] = Field(default=None, gt=0)
    limite_asistencias_semanales: Optional[int] = Field(default=None, gt=0)
    limite_asistencias_totales: Optional[int] = Field(default=None, gt=0)
    estado: Optional[bool] = None
    # Campo protegido capturado explícitamente para rechazo 403 si se intenta reasignar gimnasio
    id_gimnasio: Optional[Any] = None


class PlanMembresiaEstadoUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estado: bool
