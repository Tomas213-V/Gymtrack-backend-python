from typing import Optional
from pydantic import BaseModel, Field


class PlanMembresiaCreate(BaseModel):
    nombre: str
    precio: float = Field(..., ge=0)
    duracion_dias: int = Field(..., gt=0)
    estado: Optional[str] = "activo"


class PlanMembresiaUpdate(BaseModel):
    nombre: Optional[str] = None
    precio: Optional[float] = Field(None, ge=0)
    duracion_dias: Optional[int] = Field(None, gt=0)
    estado: Optional[str] = None
