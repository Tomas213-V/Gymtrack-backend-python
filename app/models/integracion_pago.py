from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class IniciarConexionMPRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id_gimnasio: Optional[int] = Field(default=None, gt=0)
    reconectar: bool = False


class DesconectarMPRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id_gimnasio: Optional[int] = Field(default=None, gt=0)


class CallbackSimuladoMPRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str = Field(..., min_length=10)
    autorizado: bool = True
