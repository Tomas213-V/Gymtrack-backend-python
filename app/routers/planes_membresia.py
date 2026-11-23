from typing import Dict, Any, Optional, Set
from fastapi import APIRouter, HTTPException, status, Depends, Query, Path
from supabase import Client

from app.core.dependencies import (
    get_current_user,
    _switch_profile_context_if_available,
)
from app.services.utils import get_data
from app.models.plan_membresia import (
    PlanMembresiaCreate,
    PlanMembresiaUpdate,
    PlanMembresiaEstadoUpdate,
)
from app.routers.gimnasios import (
    ensure_active_organizational_assignments,
    verify_can_read_gimnasio,
)

router = APIRouter(prefix="/api/planes-membresia", tags=["Planes de Membresía"])

PLAN_COLUMNS = (
    "id_membresia_gimnasio, nombre, descripcion, precio, duracion_dias, "
    "limite_asistencias_semanales, limite_asistencias_totales, estado, id_gimnasio"
)


def _parse_optional_bool_estado(estado: Optional[str]) -> Optional[bool]:
    if estado is None:
        return None
    clean = str(estado).strip().lower()
    if not clean or clean == "todos":
        return None
    if clean in ("true", "1", "activo", "activa"):
        return True
    if clean in ("false", "0", "inactivo", "inactiva"):
        return False
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Estado inválido. Valores permitidos: true, false, activo, inactivo, todos.",
    )


def verify_can_manage_planes(
    current_user: Dict[str, Any],
    id_gimnasio: Optional[int] = None,
) -> tuple[Dict[str, Any], int]:
    """
    Valida permisos de escritura sobre `public.membresia_gimnasio` en correspondencia con la
    política RLS `plan_write_dueno_o_admin`:
      - Dueño del gimnasio (`fn_dueno_posee_gimnasio`).
      - Administrador organizacional activo (`fn_admin_posee_gimnasio`).
      - Empleado operativo con subrol activo `'administrador'` en ese gimnasio (`fn_empleado_en_gimnasio(id, array['administrador'])`).
    Rechaza recepcionistas, entrenadores, socios y usuarios de otros gimnasios con `403 Forbidden`.
    """
    ensure_active_organizational_assignments(current_user)

    req_gym = current_user.get("requested_gym_id")
    if id_gimnasio is not None and req_gym is not None and int(id_gimnasio) != int(req_gym):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No se permite cruzar gimnasios al gestionar planes de membresía.",
        )

    target_gid = id_gimnasio if id_gimnasio is not None else current_user.get("id_gimnasio")

    def _matches(ctx: Dict[str, Any], gid: Optional[int]) -> Optional[int]:
        rol = ctx.get("rol")
        gym_ids: Set[int] = {
            int(g["id_gimnasio"])
            for g in (ctx.get("gimnasios") or [])
            if isinstance(g, dict) and g.get("id_gimnasio") is not None
        }
        resolved = int(gid) if gid is not None else (
            int(ctx["id_gimnasio"]) if ctx.get("id_gimnasio") is not None else None
        )
        if resolved is None or resolved not in gym_ids:
            return None
        if rol in ("dueño", "administrador"):
            return resolved
        if rol == "empleado":
            roles_gym = {r.lower() for r in ((ctx.get("roles_empleado_por_gimnasio") or {}).get(resolved) or [])}
            return resolved if "administrador" in roles_gym else None
        return None

    matched = _matches(current_user, target_gid)
    if matched is not None:
        return current_user, matched

    if current_user.get("requested_role") is None:
        for candidate in ("dueño", "administrador", "empleado"):
            if candidate in (current_user.get("perfiles_disponibles") or []):
                switched = _switch_profile_context_if_available(current_user, candidate)
                matched = _matches(switched, target_gid)
                if matched is not None:
                    return switched, matched

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="No tienes permisos para administrar los planes de membresía de este gimnasio (requiere dueño, administrador organizacional o administrador de sucursal).",
    )


def _fetch_plan_or_404(db: Client, id_plan: int) -> Dict[str, Any]:
    res = (
        db.from_("membresia_gimnasio")
        .select(PLAN_COLUMNS)
        .eq("id_membresia_gimnasio", id_plan)
        .maybe_single()
        .execute()
    )
    plan = get_data(res)
    if not isinstance(plan, dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plan de membresía no encontrado.",
        )
    return plan


@router.get("/catalogo", status_code=status.HTTP_200_OK)
async def listar_catalogo_planes(
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Consulta el catálogo público controlado `public.vw_catalogo_planes` (security_barrier = true)
    de planes activos pertenecientes a gimnasios activos.
    """
    db: Client = current_user["supabase"]
    query = db.from_("vw_catalogo_planes").select(PLAN_COLUMNS)
    if id_gimnasio is not None:
        query = query.eq("id_gimnasio", id_gimnasio)

    res = query.order("precio", desc=False).execute()
    rows = get_data(res)
    planes = rows if isinstance(rows, list) else []
    if id_gimnasio is not None:
        planes = [p for p in planes if isinstance(p, dict) and int(p.get("id_gimnasio") or 0) == id_gimnasio]
    return {
        "total": len(planes),
        "planes": planes,
    }


@router.get("", status_code=status.HTTP_200_OK)
async def get_planes_membresia(
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    estado: Optional[str] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Lista los planes de membresía (`public.membresia_gimnasio`) del gimnasio autorizado.
    - Para socios, devuelve únicamente planes activos (`estado = true`) de su gimnasio asociado.
    - Bloquea con 403 cualquier consulta a un gimnasio no autorizado.
    """
    ensure_active_organizational_assignments(current_user)
    target_gym_id = id_gimnasio if id_gimnasio is not None else current_user.get("id_gimnasio")
    if not target_gym_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se identificó un gimnasio autorizado para consultar planes.",
        )

    user_ctx = verify_can_read_gimnasio(current_user, int(target_gym_id))
    estado_bool = _parse_optional_bool_estado(estado)
    if user_ctx.get("rol") == "socio":
        estado_bool = True

    db: Client = user_ctx["supabase"]
    query = (
        db.from_("membresia_gimnasio")
        .select(PLAN_COLUMNS)
        .eq("id_gimnasio", int(target_gym_id))
        .order("id_membresia_gimnasio", desc=False)
    )
    res = query.execute()
    rows = get_data(res)
    planes = [
        p for p in (rows if isinstance(rows, list) else [])
        if isinstance(p, dict)
        and int(p.get("id_gimnasio") or 0) == int(target_gym_id)
        and (estado_bool is None or bool(p.get("estado")) is estado_bool)
    ]
    return {
        "id_gimnasio": int(target_gym_id),
        "total": len(planes),
        "planes": planes,
    }


@router.get("/{id_membresia_gimnasio}", status_code=status.HTTP_200_OK)
async def get_plan_membresia_by_id(
    id_membresia_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Obtiene un plan de `public.membresia_gimnasio` validando pertenencia al gimnasio autorizado."""
    ensure_active_organizational_assignments(current_user)
    db: Client = current_user["supabase"]
    plan = _fetch_plan_or_404(db, id_membresia_gimnasio)

    plan_gym_id = int(plan["id_gimnasio"])
    user_ctx = verify_can_read_gimnasio(current_user, plan_gym_id)

    req_gym = current_user.get("requested_gym_id")
    if req_gym is not None and int(req_gym) != plan_gym_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El plan solicitado no pertenece al gimnasio del contexto activo.",
        )

    if user_ctx.get("rol") == "socio" and plan.get("estado") is False:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El plan solicitado se encuentra inactivo.",
        )

    return {"plan": plan}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_plan_membresia(
    req: PlanMembresiaCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Crea un plan en `public.membresia_gimnasio` para el gimnasio autorizado."""
    manager_ctx, gym_id = verify_can_manage_planes(current_user, id_gimnasio=req.id_gimnasio)
    clean_nombre = req.nombre.strip()
    if not clean_nombre:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre del plan es obligatorio.")

    db: Client = manager_ctx["supabase"]

    # Verificar unicidad (id_gimnasio, nombre) (`uq_plan_nombre_gimnasio`)
    res_exist = (
        db.from_("membresia_gimnasio")
        .select("id_membresia_gimnasio, nombre")
        .eq("id_gimnasio", gym_id)
        .execute()
    )
    existing_plans = get_data(res_exist) or []
    for p in (existing_plans if isinstance(existing_plans, list) else []):
        if isinstance(p, dict) and str(p.get("nombre") or "").strip().lower() == clean_nombre.lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Ya existe un plan con el nombre '{clean_nombre}' en este gimnasio.",
            )

    nuevo_plan = {
        "nombre": clean_nombre,
        "descripcion": req.descripcion.strip() if req.descripcion and req.descripcion.strip() else None,
        "precio": round(float(req.precio), 2),
        "duracion_dias": int(req.duracion_dias),
        "limite_asistencias_semanales": req.limite_asistencias_semanales,
        "limite_asistencias_totales": req.limite_asistencias_totales,
        "estado": bool(req.estado),
        "id_gimnasio": gym_id,
    }

    res_ins = db.from_("membresia_gimnasio").insert([nuevo_plan]).execute()
    rows = get_data(res_ins) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al crear el plan de membresía.",
        )

    return {
        "mensaje": "Plan de membresía creado correctamente.",
        "plan": rows[0],
    }


async def _update_plan_logic(
    id_membresia_gimnasio: int,
    req: PlanMembresiaUpdate,
    current_user: Dict[str, Any],
) -> Dict[str, Any]:
    ensure_active_organizational_assignments(current_user)
    db: Client = current_user["supabase"]
    plan_actual = _fetch_plan_or_404(db, id_membresia_gimnasio)
    plan_gym_id = int(plan_actual["id_gimnasio"])

    if req.id_gimnasio is not None and str(req.id_gimnasio).strip() != str(plan_gym_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No está permitido transferir un plan de membresía a otro gimnasio.",
        )

    manager_ctx, _ = verify_can_manage_planes(current_user, id_gimnasio=plan_gym_id)

    update_data: Dict[str, Any] = {}
    if req.nombre is not None:
        clean_nombre = req.nombre.strip()
        if not clean_nombre:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre del plan no puede estar vacío.")
        update_data["nombre"] = clean_nombre
    if req.descripcion is not None:
        update_data["descripcion"] = req.descripcion.strip() if req.descripcion.strip() else None
    if req.precio is not None:
        update_data["precio"] = round(float(req.precio), 2)
    if req.duracion_dias is not None:
        update_data["duracion_dias"] = int(req.duracion_dias)
    if req.limite_asistencias_semanales is not None:
        update_data["limite_asistencias_semanales"] = int(req.limite_asistencias_semanales)
    if req.limite_asistencias_totales is not None:
        update_data["limite_asistencias_totales"] = int(req.limite_asistencias_totales)
    if req.estado is not None:
        update_data["estado"] = bool(req.estado)

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se enviaron campos válidos para actualizar el plan.",
        )

    manager_db: Client = manager_ctx["supabase"]
    res_upd = (
        manager_db.from_("membresia_gimnasio")
        .update(update_data)
        .eq("id_membresia_gimnasio", id_membresia_gimnasio)
        .eq("id_gimnasio", plan_gym_id)
        .execute()
    )
    rows = get_data(res_upd) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plan de membresía no encontrado o sin autorización.",
        )

    return {
        "mensaje": "Plan de membresía actualizado exitosamente.",
        "plan": rows[0],
    }


@router.put("/{id_membresia_gimnasio}", status_code=status.HTTP_200_OK)
async def update_plan_membresia(
    req: PlanMembresiaUpdate,
    id_membresia_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Actualiza los datos permitidos de un plan en `public.membresia_gimnasio`."""
    return await _update_plan_logic(id_membresia_gimnasio, req, current_user)


@router.patch("/{id_membresia_gimnasio}/estado", status_code=status.HTTP_200_OK)
async def cambiar_estado_plan_membresia(
    req: PlanMembresiaEstadoUpdate,
    id_membresia_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Activa o desactiva un plan en `public.membresia_gimnasio` preservando contrataciones históricas."""
    return await _update_plan_logic(
        id_membresia_gimnasio,
        PlanMembresiaUpdate(estado=req.estado),
        current_user,
    )


@router.patch("/{id_membresia_gimnasio}/desactivar", status_code=status.HTTP_200_OK)
async def deshabilitar_plan_membresia(
    id_membresia_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Desactiva un plan (`estado = false`) preservando la integridad de las contrataciones históricas."""
    res = await _update_plan_logic(
        id_membresia_gimnasio,
        PlanMembresiaUpdate(estado=False),
        current_user,
    )
    return {
        "mensaje": "Plan de membresía desactivado exitosamente conservando el historial de contrataciones.",
        "plan": res["plan"],
    }


@router.delete("/{id_membresia_gimnasio}", status_code=status.HTTP_200_OK)
async def delete_plan_membresia(
    id_membresia_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Desactiva lógicamente (`estado = false`) un plan en `public.membresia_gimnasio` para
    garantizar la integridad histórica de `public.membresia_socio` (`ON DELETE RESTRICT`).
    """
    res = await _update_plan_logic(
        id_membresia_gimnasio,
        PlanMembresiaUpdate(estado=False),
        current_user,
    )
    return {
        "mensaje": "Plan de membresía desactivado para preservar la integridad histórica de contrataciones.",
        "inactivo": True,
        "plan": res["plan"],
    }

