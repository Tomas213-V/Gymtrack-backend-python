import re
from typing import Optional, Literal
from pydantic import BaseModel, Field, field_validator, model_validator

RolEmpleadoSucursal = Literal["administrador", "recepcionista", "entrenador"]
EMAIL_REGEX = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _validate_optional_email(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    clean = str(v).strip().lower()
    if not clean:
        return None
    if not EMAIL_REGEX.match(clean):
        raise ValueError("El formato del email del empleado no es válido.")
    return clean


class RegistrarEmpleadoSucursalRequest(BaseModel):
    """
    Contrato para registrar un nuevo empleado o reutilizar una identidad `public.empleado`
    existente asignándola a una sucursal mediante `public.rpc_registrar_empleado_sucursal`.
    `public.empleado` NO posee `id_gimnasio`; la relación laboral vive en `public.empleado_sucursal`.
    """
    id_sucursal: Optional[int] = None
    id_gimnasio: Optional[int] = None
    rol: RolEmpleadoSucursal
    id_empleado: Optional[int] = None
    nombre: Optional[str] = Field(default=None, max_length=80)
    apellido: Optional[str] = Field(default=None, max_length=80)
    dni: Optional[str] = Field(default=None, max_length=20)
    email: Optional[str] = Field(default=None, max_length=254)
    telefono: Optional[str] = Field(default=None, max_length=30)

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        return _validate_optional_email(v)

    @model_validator(mode="after")
    def validate_identificador_o_datos(self) -> "RegistrarEmpleadoSucursalRequest":
        has_id = self.id_empleado is not None and self.id_empleado > 0
        has_dni = bool(self.dni and self.dni.strip())
        has_email = bool(self.email and str(self.email).strip())
        if not (has_id or has_dni or has_email):
            raise ValueError(
                "Debes proporcionar id_empleado, DNI o email para identificar o registrar al empleado."
            )
        return self


class AsignacionEmpleadoSucursalCreate(BaseModel):
    """
    Contrato para asignar un `id_empleado` existente a una sucursal con un rol operativo.
    """
    id_sucursal: Optional[int] = None
    id_gimnasio: Optional[int] = None
    rol: RolEmpleadoSucursal


class AsignacionEmpleadoSucursalUpdate(BaseModel):
    """
    Contrato para actualizar el rol operativo o estado de una asignación en `public.empleado_sucursal`.
    Respeta `trg_validar_empleado_sucursal_gimnasio` (no permite transferir la asignación a otro empleado o sucursal).
    """
    rol: Optional[RolEmpleadoSucursal] = None
    estado: Optional[bool] = None
    id_empleado: Optional[int] = None
    id_sucursal: Optional[int] = None


class AsignacionEmpleadoSucursalEstadoUpdate(BaseModel):
    estado: bool


class EmpleadoUpdate(BaseModel):
    """
    Contrato para actualizar datos personales de `public.empleado`.
    Respeta el column-level GRANT: `update (nombre, apellido, dni, telefono, email, estado)`.
    """
    nombre: Optional[str] = Field(default=None, max_length=80)
    apellido: Optional[str] = Field(default=None, max_length=80)
    dni: Optional[str] = Field(default=None, max_length=20)
    telefono: Optional[str] = Field(default=None, max_length=30)
    email: Optional[str] = Field(default=None, max_length=254)
    estado: Optional[bool] = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        return _validate_optional_email(v)
