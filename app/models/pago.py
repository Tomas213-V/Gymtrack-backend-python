from typing import Optional, Literal, Any
from pydantic import BaseModel, ConfigDict, Field

MetodoPagoPermitido = Literal[
    "efectivo",
    "transferencia",
    "tarjeta",
    "debito",
    "credito",
    "mercadopago",
    "uala",
    "otro",
]
EstadoPagoInicial = Literal["pendiente", "aprobado", "rechazado"]
EstadoPago = Literal["pendiente", "aprobado", "rechazado", "anulado"]


class PagoCreate(BaseModel):
    """
    Contrato para registrar un pago sobre una contratación existente `public.membresia_socio`
    exclusivamente mediante `public.rpc_registrar_pago`.
    - Si `monto` no se indica, el backend calcula el saldo pendiente real de la contratación.
    - Si `monto` se indica (ej. pago parcial), se valida `monto >= 0` y que no exceda el saldo pendiente.
    """
    model_config = ConfigDict(extra="forbid")

    id_membresia_socio: int = Field(..., gt=0)
    monto: Optional[float] = Field(default=None, ge=0)
    metodo_pago: MetodoPagoPermitido = "efectivo"
    estado: EstadoPagoInicial = "aprobado"
    comprobante: Optional[str] = Field(default=None, max_length=180)
    fecha_pago: Optional[str] = None
    id_gimnasio: Optional[int] = Field(default=None, gt=0)


class PagoUpdateEstado(BaseModel):
    """
    Contrato para transicionar el estado de un pago existente exclusivamente mediante
    `public.rpc_actualizar_estado_pago`.
    No permite alterar monto, comprobante, fecha_pago, metodo_pago ni id_membresia_socio.
    """
    model_config = ConfigDict(extra="forbid")

    estado: EstadoPago
    # Campos inmutables capturados para rechazo explícito 403
    monto: Optional[Any] = None
    metodo_pago: Optional[Any] = None
    comprobante: Optional[Any] = None
    fecha_pago: Optional[Any] = None
    id_membresia_socio: Optional[Any] = None
