from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException, status, Depends, Query, Path
from app.config import supabase
from app.core.dependencies import get_current_user
from app.models.plan_membresia import PlanMembresiaCreate, PlanMembresiaUpdate

router = APIRouter(prefix="/api/planes-membresia", tags=["Planes de Membresía"])


@router.get("", status_code=status.HTTP_200_OK)
async def get_planes_membresia(
    estado: Optional[str] = Query(None),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Obtener planes de membresía del gimnasio."""
    id_gimnasio = current_user.get("id_gimnasio")
    if not id_gimnasio:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se identificó el gimnasio autenticado."
        )

    query = (
        supabase.from_("plan_membresia")
        .select("*")
        .eq("id_gimnasio", id_gimnasio)
    )

    if estado and estado.lower() != "todos":
        query = query.eq("estado", estado.lower())

    res = query.order("id_plan_membresia", desc=False).execute()
    return {"planes": res.data or []}


@router.get("/{id}", status_code=status.HTTP_200_OK)
async def get_plan_membresia_by_id(
    id: int = Path(...),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Obtener un plan de membresía por ID."""
    id_gimnasio = current_user.get("id_gimnasio")
    res = (
        supabase.from_("plan_membresia")
        .select("*")
        .eq("id_plan_membresia", id)
        .eq("id_gimnasio", id_gimnasio)
        .maybe_single()
        .execute()
    )
    plan = res.data
    if not plan:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El plan de membresía no existe o no pertenece a tu gimnasio."
        )
    return {"plan": plan}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_plan_membresia(
    req: PlanMembresiaCreate,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Crear un nuevo plan de membresía."""
    id_gimnasio = current_user.get("id_gimnasio")
    if not id_gimnasio:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se identificó el gimnasio autenticado."
        )

    if not req.nombre or not req.nombre.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre del plan es obligatorio.")

    nuevo_plan_data = {
        "id_gimnasio": id_gimnasio,
        "nombre": req.nombre.strip(),
        "precio": float(req.precio),
        "duracion_dias": int(req.duracion_dias),
        "estado": (req.estado or "activo").strip().lower(),
    }

    res = supabase.from_("plan_membresia").insert([nuevo_plan_data]).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al crear el plan de membresía.")

    return {
        "mensaje": "Plan de membresía creado correctamente.",
        "plan": res.data[0],
    }


@router.put("/{id}", status_code=status.HTTP_200_OK)
async def update_plan_membresia(
    id: int = Path(...),
    req: PlanMembresiaUpdate = ...,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Actualizar un plan de membresía existente."""
    id_gimnasio = current_user.get("id_gimnasio")
    update_data = {}
    if req.nombre is not None:
        update_data["nombre"] = req.nombre.strip()
    if req.precio is not None:
        update_data["precio"] = float(req.precio)
    if req.duracion_dias is not None:
        update_data["duracion_dias"] = int(req.duracion_dias)
    if req.estado is not None:
        update_data["estado"] = req.estado.strip().lower()

    res = (
        supabase.from_("plan_membresia")
        .update(update_data)
        .eq("id_plan_membresia", id)
        .eq("id_gimnasio", id_gimnasio)
        .execute()
    )
    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plan de membresía no encontrado o no pertenece a tu gimnasio."
        )

    return {
        "mensaje": "Plan de membresía actualizado exitosamente.",
        "plan": res.data[0],
    }


@router.patch("/{id}/desactivar", status_code=status.HTTP_200_OK)
async def deshabilitar_plan_membresia(
    id: int = Path(...),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Desactivar plan de membresía (marcar como inactivo)."""
    id_gimnasio = current_user.get("id_gimnasio")
    res = (
        supabase.from_("plan_membresia")
        .update({"estado": "inactivo"})
        .eq("id_plan_membresia", id)
        .eq("id_gimnasio", id_gimnasio)
        .execute()
    )
    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plan de membresía no encontrado o no pertenece a tu gimnasio."
        )

    return {
        "mensaje": "Plan de membresía marcado como 'inactivo' exitosamente.",
        "plan": res.data[0],
    }
