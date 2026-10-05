from datetime import datetime, date, timedelta
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException, status, Depends, Query, Path
from app.config import supabase
from app.core.dependencies import get_current_user
from app.models.membresia import MembresiaCreate, MembresiaUpdate

router = APIRouter(prefix="/api/membresias", tags=["Membresías"])


@router.get("", status_code=status.HTTP_200_OK)
async def get_membresias(
    id_socio: Optional[int] = Query(None),
    estado: Optional[str] = Query(None),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Obtener todas las membresías del gimnasio con filtros de socio y estado."""
    id_gimnasio = current_user.get("id_gimnasio")
    if not id_gimnasio:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se identificó el gimnasio autenticado."
        )

    query = (
        supabase.from_("membresia")
        .select("""
            *,
            socio!inner (
                id_socio,
                nombre,
                apellido,
                dni,
                id_gimnasio
            ),
            plan_membresia (*)
        """)
        .eq("socio.id_gimnasio", id_gimnasio)
        .order("fecha_vencimiento", desc=False)
    )

    if id_socio:
        query = query.eq("id_socio", id_socio)
    if estado and estado.lower() != "todos":
        query = query.eq("estado", estado.lower())

    res = query.execute()
    return {"membresias": res.data or []}


@router.get("/{id}", status_code=status.HTTP_200_OK)
async def get_membresia_by_id(
    id: int = Path(...),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Obtener una membresía específica por ID."""
    id_gimnasio = current_user.get("id_gimnasio")
    res = (
        supabase.from_("membresia")
        .select("""
            *,
            socio!inner (
                id_socio,
                nombre,
                apellido,
                dni,
                id_gimnasio
            ),
            plan_membresia (*)
        """)
        .eq("id_membresia", id)
        .eq("socio.id_gimnasio", id_gimnasio)
        .maybe_single()
        .execute()
    )

    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Membresía no encontrada o no pertenece a tu gimnasio."
        )

    return {"membresia": res.data}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_membresia(
    req: MembresiaCreate,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Registrar / Asignar una membresía a un socio calculando automáticamente la fecha de vencimiento."""
    id_gimnasio = current_user.get("id_gimnasio")

    # 1. Validar que el socio existe y pertenece al gimnasio
    res_socio = (
        supabase.from_("socio")
        .select("id_socio")
        .eq("id_socio", req.id_socio)
        .eq("id_gimnasio", id_gimnasio)
        .maybe_single()
        .execute()
    )
    if not res_socio.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El socio especificado no existe o no pertenece a tu gimnasio."
        )

    # 2. Validar el plan seleccionado
    res_plan = (
        supabase.from_("plan_membresia")
        .select("*")
        .eq("id_plan_membresia", req.id_plan_membresia)
        .eq("id_gimnasio", id_gimnasio)
        .maybe_single()
        .execute()
    )
    plan = res_plan.data
    if not plan:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El plan de membresía especificado no existe."
        )

    # 3. Cálculo de fechas
    f_inicio = date.fromisoformat(req.fecha_inicio.split("T")[0]) if req.fecha_inicio else date.today()
    duracion_total = (plan.get("duracion_dias") or 30) + (req.dias_adicionales or 0)
    f_vencimiento = f_inicio + timedelta(days=duracion_total)

    precio_final = float(req.precio) if req.precio is not None else float(plan.get("precio", 0))

    nueva_memb_data = {
        "id_socio": req.id_socio,
        "id_plan_membresia": req.id_plan_membresia,
        "precio": precio_final,
        "fecha_inicio": f_inicio.isoformat(),
        "fecha_vencimiento": f_vencimiento.isoformat(),
        "estado": (req.estado or "activo").strip().lower(),
    }

    res_ins = (
        supabase.from_("membresia")
        .insert([nueva_memb_data])
        .execute()
    )
    if not res_ins.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al registrar la membresía.")

    # Consultar membresía completa con plan
    res_completa = (
        supabase.from_("membresia")
        .select("*, plan_membresia(*)")
        .eq("id_membresia", res_ins.data[0]["id_membresia"])
        .single()
        .execute()
    )

    return {
        "mensaje": "Membresía asignada al socio exitosamente.",
        "membresia": res_completa.data,
    }


@router.patch("/{id}", status_code=status.HTTP_200_OK)
async def update_membresia(
    id: int = Path(...),
    req: MembresiaUpdate = ...,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Actualizar / Extender fechas o estado de una membresía."""
    id_gimnasio = current_user.get("id_gimnasio")

    # 1. Verificar existencia
    res_m = (
        supabase.from_("membresia")
        .select("*, socio!inner(id_gimnasio)")
        .eq("id_membresia", id)
        .eq("socio.id_gimnasio", id_gimnasio)
        .maybe_single()
        .execute()
    )
    memb_actual = res_m.data
    if not memb_actual:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Membresía no encontrada o no pertenece a tu gimnasio."
        )

    update_payload = {}
    if req.fecha_inicio:
        update_payload["fecha_inicio"] = req.fecha_inicio.split("T")[0]

    if req.fecha_vencimiento:
        update_payload["fecha_vencimiento"] = req.fecha_vencimiento.split("T")[0]
    elif req.dias_extension:
        raw_venc = memb_actual.get("fecha_vencimiento")
        base_date = date.fromisoformat(str(raw_venc).split("T")[0]) if raw_venc else date.today()
        new_venc = base_date + timedelta(days=int(req.dias_extension))
        update_payload["fecha_vencimiento"] = new_venc.isoformat()

    if req.estado:
        update_payload["estado"] = req.estado.strip().lower()

    if req.precio is not None:
        update_payload["precio"] = float(req.precio)

    res_upd = (
        supabase.from_("membresia")
        .update(update_payload)
        .eq("id_membresia", id)
        .execute()
    )
    if not res_upd.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al actualizar la membresía.")

    res_full = (
        supabase.from_("membresia")
        .select("*, plan_membresia(*)")
        .eq("id_membresia", id)
        .single()
        .execute()
    )

    return {
        "mensaje": "Membresía actualizada exitosamente.",
        "membresia": res_full.data,
    }
