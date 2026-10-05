from typing import Optional
from pydantic import BaseModel


class GimnasioCreate(BaseModel):
    nombre: str
    direccion: Optional[str] = None
    telefono: Optional[str] = None
    email: Optional[str] = None
    id_usuario: Optional[int] = None
