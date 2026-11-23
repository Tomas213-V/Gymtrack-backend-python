from typing import Optional, Literal
from pydantic import BaseModel, ConfigDict, Field


class CheckinSocioRequest(BaseModel):
    """
    Solicitud de check-in desde el panel del socio.
    Requiere `id_sucursal` y delega toda validación en `public.rpc_registrar_ingreso_asistencia`.
    """
    model_config = ConfigDict(extra="forbid")

    id_sucursal: int = Field(..., gt=0)


class SolicitarMembresiaSocio(BaseModel):
    """
    Solicitud de contratación o renovación pendiente desde el panel del socio.
    Invoca `public.rpc_contratar_membresia` con `p_estado = 'pendiente'` y `p_registrar_pago = false`.
    """
    model_config = ConfigDict(extra="forbid")

    id_membresia_gimnasio: int = Field(..., gt=0)
    fecha_inicio: Optional[str] = None


class IniciarPagoSocio(BaseModel):
    """
    Prepara un checkout externo (Mercado Pago / Ualá) tomando el precio real desde `public.membresia_gimnasio`.
    """
    model_config = ConfigDict(extra="forbid")

    id_membresia_gimnasio: int = Field(..., gt=0)
    proveedor: Literal["mercadopago", "uala"] = "mercadopago"
