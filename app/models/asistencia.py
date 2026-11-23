from typing import Optional, Any
from pydantic import BaseModel, ConfigDict, Field


class AsistenciaCreate(BaseModel):
    """
    Contrato para registrar un ingreso en `public.asistencia_socio` mediante
    `public.rpc_registrar_ingreso_asistencia(p_id_sucursal, p_id_socio)`.
    - `id_sucursal` es obligatorio (o resuelto del contexto único de sucursal del empleado).
    - No almacena ni confía en `id_gimnasio`, `id_membresia_socio`, `hora_ingreso` ni `estado`
      impuestos por el cliente: la RPC determina fecha/hora, estado y valida membresía vigente,
      asistencia abierta única y límite de 3 asistencias/día por gimnasio.
    """
    model_config = ConfigDict(extra="forbid")

    id_sucursal: Optional[int] = Field(default=None, gt=0)
    id_socio: Optional[int] = Field(default=None, gt=0)
    dni: Optional[str] = Field(default=None, max_length=30)
    # Campos protegidos capturados explícitamente para rechazo 403/400 si el cliente intenta manipularlos
    id_gimnasio: Optional[Any] = None
    id_membresia_socio: Optional[Any] = None
    hora_ingreso: Optional[Any] = None
    hora_salida: Optional[Any] = None
    estado: Optional[Any] = None


class AsistenciaCheckout(BaseModel):
    """
    Contrato para registrar salida mediante `public.rpc_registrar_salida_asistencia`.
    La RPC determina `hora_salida = greatest(now(), hora_ingreso)` y `estado = 'finalizada'`.
    """
    model_config = ConfigDict(extra="forbid")

    id_socio: Optional[int] = Field(default=None, gt=0)
    # Campos protegidos que no puede imponer el cliente
    hora_salida: Optional[Any] = None
    estado: Optional[Any] = None


class AsistenciaAnularRequest(BaseModel):
    """
    Contrato para anular lógicamente una asistencia mediante `public.rpc_anular_asistencia`.
    """
    model_config = ConfigDict(extra="forbid")

    motivo: Optional[str] = Field(default=None, max_length=500)
