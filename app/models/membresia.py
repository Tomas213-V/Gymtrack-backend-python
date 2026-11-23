from datetime import date
from typing import Optional, Literal, Any
from pydantic import BaseModel, ConfigDict, Field, field_validator

EstadoMembresiaInicial = Literal["pendiente", "activa"]
EstadoMembresia = Literal["pendiente", "activa", "vencida", "cancelada"]
EstadoPagoInicial = Literal["pendiente", "aprobado", "rechazado"]
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


def _validate_iso_date_str(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    clean = str(value).strip().split("T")[0]
    if not clean:
        return None
    # Lanza ValueError si el formato no es YYYY-MM-DD válido
    date.fromisoformat(clean)
    return clean


class MembresiaCreate(BaseModel):
    """
    Contrato para contratar o renovar una membresía (`public.membresia_socio`)
    exclusivamente mediante `public.rpc_contratar_membresia`.
    - El importe definitivo proviene siempre de `public.membresia_gimnasio.precio` (el cliente no decide precio).
    - El vínculo `id_socio_gimnasio` y `id_gimnasio` se resuelven y validan en backend/RPC.
    """
    model_config = ConfigDict(extra="forbid")

    id_socio: Optional[int] = Field(default=None, gt=0)
    id_membresia_gimnasio: int = Field(..., gt=0)
    id_gimnasio: Optional[int] = Field(default=None, gt=0)
    fecha_inicio: Optional[str] = None
    estado: EstadoMembresiaInicial = "activa"
    registrar_pago: bool = False
    metodo_pago: MetodoPagoPermitido = "efectivo"
    estado_pago: EstadoPagoInicial = "aprobado"
    comprobante: Optional[str] = Field(default=None, max_length=180)
    # Campos protegidos capturados explícitamente para bloqueo controlado 403/400
    precio: Optional[Any] = None
    monto_override: Optional[Any] = None
    id_socio_gimnasio: Optional[Any] = None
    fecha_vencimiento: Optional[Any] = None

    @field_validator("fecha_inicio")
    @classmethod
    def validate_fecha_inicio(cls, v: Optional[str]) -> Optional[str]:
        return _validate_iso_date_str(v)


class MembresiaRenovarRequest(BaseModel):
    """
    Contrato específico para renovar una membresía creando una NUEVA fila histórica
    en `public.membresia_socio` mediante `public.rpc_contratar_membresia`.
    - Si hay contrato activo vigente, la RPC encadena `fecha_inicio = max(fecha_vencimiento) + 1`.
    - Si la membresía anterior venció, inicia desde `current_date` (o `fecha_inicio`).
    """
    model_config = ConfigDict(extra="forbid")

    id_socio: Optional[int] = Field(default=None, gt=0)
    id_membresia_gimnasio: int = Field(..., gt=0)
    id_gimnasio: Optional[int] = Field(default=None, gt=0)
    fecha_inicio: Optional[str] = None
    estado: EstadoMembresiaInicial = "activa"
    registrar_pago: bool = False
    metodo_pago: MetodoPagoPermitido = "efectivo"
    estado_pago: EstadoPagoInicial = "aprobado"
    comprobante: Optional[str] = Field(default=None, max_length=180)
    precio: Optional[Any] = None
    monto_override: Optional[Any] = None

    @field_validator("fecha_inicio")
    @classmethod
    def validate_fecha_inicio(cls, v: Optional[str]) -> Optional[str]:
        return _validate_iso_date_str(v)


class MembresiaUpdate(BaseModel):
    """
    Contrato para actualizar estado o fecha de vencimiento de una contratación
    exclusivamente mediante `public.rpc_actualizar_estado_membresia`.
    - No permite alterar `fecha_inicio`, `precio`, `id_membresia_gimnasio`, `id_socio_gimnasio` ni `id_gimnasio`
      (protegidos por `trg_proteger_membresia_socio_inmutable`).
    """
    model_config = ConfigDict(extra="forbid")

    estado: Optional[EstadoMembresia] = None
    fecha_vencimiento: Optional[str] = None
    # Campos inmutables capturados para rechazo explícito 403
    id_membresia_gimnasio: Optional[Any] = None
    id_socio_gimnasio: Optional[Any] = None
    id_gimnasio: Optional[Any] = None
    fecha_inicio: Optional[Any] = None
    precio: Optional[Any] = None

    @field_validator("fecha_vencimiento")
    @classmethod
    def validate_fecha_vencimiento(cls, v: Optional[str]) -> Optional[str]:
        return _validate_iso_date_str(v)

