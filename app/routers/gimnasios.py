from datetime import date
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException, status, Depends, Query, Path
from app.config import supabase
from app.core.dependencies import get_current_user
from app.services.utils import get_data
from app.models.gimnasio import GimnasioCreate, GimnasioUpdate

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


async def _actualizar_gimnasio_logic(
    req: GimnasioUpdate,
    current_user: Dict[str, Any],
    gym_id: Optional[int] = None,
):
    id_usuario = current_user.get("id_usuario")
    id_gimnasio = gym_id or current_user.get("id_gimnasio")

    if not id_usuario and not id_gimnasio:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No se pudo identificar al usuario autenticado."
        )

    # Buscar el gimnasio actual
    query = supabase.from_("gimnasio").select("*")
    if id_usuario:
        query = query.eq("id_usuario", id_usuario)
    else:
        query = query.eq("id_gimnasio", id_gimnasio)

    res_fetch = query.maybe_single().execute()
    gym_existente = get_data(res_fetch)

    if not gym_existente:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Gimnasio no encontrado o no pertenece a tu cuenta."
        )

    update_fields = {}
    if req.nombre is not None:
        if not req.nombre.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El nombre del gimnasio no puede estar vacío."
            )
        update_fields["nombre"] = req.nombre.strip()

    if req.direccion is not None:
        update_fields["direccion"] = req.direccion.strip() if req.direccion else None
    if req.telefono is not None:
        update_fields["telefono"] = req.telefono.strip() if req.telefono else None
    if req.email is not None:
        update_fields["email"] = req.email.strip().lower() if req.email else None

    if not update_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se enviaron datos para actualizar."
        )

    res_upd = (
        supabase.from_("gimnasio")
        .update(update_fields)
        .eq("id_gimnasio", gym_existente["id_gimnasio"])
        .execute()
    )
    if not res_upd.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al actualizar el gimnasio."
        )

    return {
        "mensaje": "Datos del gimnasio actualizados exitosamente.",
        "gimnasio": res_upd.data[0],
    }


@router.put("/mi-gimnasio", status_code=status.HTTP_200_OK)
async def actualizar_mi_gimnasio(
    req: GimnasioUpdate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Actualiza los datos del gimnasio del usuario autenticado."""
    return await _actualizar_gimnasio_logic(req, current_user)


@router.put("/{id}", status_code=status.HTTP_200_OK)
async def actualizar_gimnasio_por_id(
    req: GimnasioUpdate,
    id: int = Path(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Actualiza los datos del gimnasio por ID."""
    return await _actualizar_gimnasio_logic(req, current_user, gym_id=id)
