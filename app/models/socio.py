import re
from typing import Optional, Any
from pydantic import BaseModel, Field, field_validator, model_validator

EMAIL_REGEX = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _normalize_optional_email(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    clean = str(value).strip().lower()
    if not clean:
        return None
    if not EMAIL_REGEX.match(clean):
        raise ValueError("El formato del email ingresado no es válido.")
    return clean


def _normalize_optional_dni(value: Any) -> Optional[str]:
    if value is None:
        return None
    clean = str(value).strip()
    if not clean:
        return None
    if len(clean) > 20:
        raise ValueError("El DNI no puede superar los 20 caracteres.")
    return clean


class SocioCreate(BaseModel):
    """
    Contrato para registrar un socio nuevo o reutilizar una identidad existente en `public.socio`
    y asociarla a un gimnasio mediante `public.rpc_registrar_socio_gimnasio`.
    - `public.socio` NO posee `id_gimnasio` ni `contrasena`.
    - El gimnasio destino se valida contra el contexto autorizado del personal.
    """
    id_gimnasio: Optional[int] = None
    nombre: Optional[str] = Field(default=None, max_length=80)
    apellido: Optional[str] = Field(default=None, max_length=80)
    dni: Optional[Any] = None
    telefono: Optional[str] = Field(default=None, max_length=30)
    email: Optional[str] = Field(default=None, max_length=254)
    # Campos protegidos/obsoletos detectados explícitamente para bloqueo controlado
    auth_user_id: Optional[Any] = None
    contrasena: Optional[Any] = None
    password: Optional[Any] = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_optional_email(v)

    @field_validator("dni")
    @classmethod
    def validate_dni(cls, v: Any) -> Optional[str]:
        return _normalize_optional_dni(v)

    @model_validator(mode="after")
    def validate_identifier_present(self) -> "SocioCreate":
        if not self.dni and not self.email:
            raise ValueError(
                "Debes proporcionar al menos DNI o email para identificar o registrar al socio."
            )
        return self


class AsociarSocioGimnasioRequest(BaseModel):
    """
    Contrato para asociar un socio existente (`id_socio`) a un gimnasio autorizado
    mediante `public.rpc_asociar_socio_gimnasio`.
    """
    id_socio: int = Field(..., gt=0)
    id_gimnasio: Optional[int] = None


class SocioUpdate(BaseModel):
    """
    Contrato de actualización de datos personales del socio por personal autorizado del gimnasio.
    Respeta `grant update (nombre, apellido, dni, telefono, email, estado) on public.socio`
    y `trg_proteger_campos_socio`.
    """
    nombre: Optional[str] = Field(default=None, max_length=80)
    apellido: Optional[str] = Field(default=None, max_length=80)
    dni: Optional[Any] = None
    telefono: Optional[str] = Field(default=None, max_length=30)
    email: Optional[str] = Field(default=None, max_length=254)
    estado: Optional[bool] = None
    # Campos protegidos detectados explícitamente para rechazo 403/400
    id_socio: Optional[Any] = None
    auth_user_id: Optional[Any] = None
    fecha_alta: Optional[Any] = None
    id_gimnasio: Optional[Any] = None
    contrasena: Optional[Any] = None
    password: Optional[Any] = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_optional_email(v)

    @field_validator("dni")
    @classmethod
    def validate_dni(cls, v: Any) -> Optional[str]:
        return _normalize_optional_dni(v)


class SocioChangeEstado(BaseModel):
    """
    Contrato para activar o desactivar una asociación `public.socio_gimnasio`
    mediante `public.rpc_cambiar_estado_socio_gimnasio`.
    """
    estado: bool
    id_socio: Optional[int] = None
    id_gimnasio: Optional[int] = None


class SocioUpdatePerfil(BaseModel):
    """
    Contrato de actualización de datos personales del socio autenticado sobre su propio perfil.
    No permite modificar `auth_user_id`, identificadores internos, `fecha_alta`, `estado` ni asociaciones.
    """
    nombre: Optional[str] = Field(default=None, max_length=80)
    apellido: Optional[str] = Field(default=None, max_length=80)
    telefono: Optional[str] = Field(default=None, max_length=30)
    email: Optional[str] = Field(default=None, max_length=254)
    dni: Optional[Any] = None
    # Campos protegidos/obsoletos capturados explícitamente para rechazo 403/400
    id_socio: Optional[Any] = None
    auth_user_id: Optional[Any] = None
    fecha_alta: Optional[Any] = None
    estado: Optional[Any] = None
    id_gimnasio: Optional[Any] = None
    id_socio_gimnasio: Optional[Any] = None
    rol: Optional[Any] = None
    contrasena: Optional[Any] = None
    password: Optional[Any] = None
    nueva_contrasena: Optional[Any] = None
    contrasena_actual: Optional[Any] = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_optional_email(v)

    @field_validator("dni")
    @classmethod
    def validate_dni(cls, v: Any) -> Optional[str]:
        return _normalize_optional_dni(v)
