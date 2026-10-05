from typing import Optional, Any
from pydantic import BaseModel


class PagoCreate(BaseModel):
    id_membresia: Optional[int] = None
    id_socio: Optional[int] = None
    socio_id: Optional[int] = None
    monto: float
    fecha_pago: Optional[str] = None
    metodo_pago: Optional[str] = "efectivo"
    estado: Optional[str] = "pagado"
    comprobante: Optional[str] = None
    id_plan_membresia: Optional[int] = None
    plan: Optional[str] = None
    # Creación automática de socio si no existe
    nombre: Optional[str] = None
    apellido: Optional[str] = None
    dni: Optional[Any] = None
    telefono: Optional[str] = None
    email: Optional[str] = None
    id_gimnasio: Optional[int] = None


class PagoUpdateEstado(BaseModel):
    estado: str
