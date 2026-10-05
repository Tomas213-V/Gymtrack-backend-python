from datetime import date
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException, status, Depends, Query
from app.config import supabase
from app.core.dependencies import get_current_user
from app.services.utils import get_data
from app.models.gimnasio import GimnasioCreate

router = APIRouter(prefix="/api/gimnasios", tags=["Gimnasios"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def crear_gimnasio(
    req: GimnasioCreate,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Crea un gimnasio asociado al usuario autenticado."""
    id_usuario = current_user.get("id_usuario") or req.id_usuario
    if not id_usuario:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No se pudo identificar al usuario autenticado."
        )

    if not req.nombre or not req.nombre.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El nombre del gimnasio es obligatorio."
        )

    # Verificar si el usuario ya tiene gimnasio
    res_exist = (
        supabase.from_("gimnasio")
        .select("id_gimnasio, nombre")
        .eq("id_usuario", id_usuario)
        .maybe_single()
        .execute()
    )
    if get_data(res_exist):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El usuario ya tiene un gimnasio registrado."
        )

    nuevo_gym = {
        "nombre": req.nombre.strip(),
        "direccion": req.direccion.strip() if req.direccion else None,
        "telefono": req.telefono.strip() if req.telefono else None,
        "email": req.email.strip().lower() if req.email else None,
        "fecha_registro": date.today().isoformat(),
        "estado": "activo",
        "id_usuario": id_usuario,
    }

    res_ins = supabase.from_("gimnasio").insert([nuevo_gym]).execute()
    if not res_ins.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al registrar el gimnasio."
        )

    return {
        "mensaje": "Gimnasio registrado exitosamente.",
        "gimnasio": res_ins.data[0],
    }


@router.get("/mi-gimnasio", status_code=status.HTTP_200_OK)
async def obtener_mi_gimnasio(
    id_usuario: Optional[int] = Query(None),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Obtiene el gimnasio del usuario autenticado."""
    target_user_id = current_user.get("id_usuario") or id_usuario
    if not target_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No se pudo identificar al usuario autenticado."
        )

    res = (
        supabase.from_("gimnasio")
        .select("*")
        .eq("id_usuario", target_user_id)
        .maybe_single()
        .execute()
    )

    gimnasio = get_data(res)
    return {
        "tieneGimnasio": bool(gimnasio),
        "gimnasio": gimnasio or None,
    }
