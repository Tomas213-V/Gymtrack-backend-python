from typing import Optional, Any
from pydantic import BaseModel, Field


class ProgresoCreate(BaseModel):
    peso: float = Field(..., gt=0)
    observaciones: Optional[str] = None
    fecha: Optional[str] = None


class ActualizarMembresiaSocio(BaseModel):
    id_plan: Optional[int] = None
    plan_id: Optional[int] = None
    meses: Optional[int] = 1
    notas: Optional[str] = None


class IniciarPagoSocio(BaseModel):
    proveedor: Optional[str] = "mercadopago"
    id_plan: Optional[int] = None
    plan_id: Optional[int] = None
    meses: Optional[int] = 1
    metodo_pago: Optional[str] = None


class ConfirmarPagoSocio(BaseModel):
    proveedor: Optional[str] = "mercadopago"
    id_plan: Optional[int] = None
    plan_id: Optional[int] = None
    id_pago_externo: Optional[str] = None
    comprobante: Optional[str] = None
    monto: Optional[float] = None
    metodo_pago: Optional[str] = None
    referencia: Optional[str] = None
