from datetime import date
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, HTTPException, status, Query, Path, Depends, Body
from supabase import Client

from app.core.dependencies import get_current_user
from app.services.utils import get_data
from app.models.rutina import (
    RutinaSocioCreate,
    RutinaSocioUpdate,
    RutinaEjercicioSocioAdd,
    RutinaEjercicioUpdate,
    RutinaGimnasioCreate,
    RutinaGimnasioUpdate,
    RutinaEjercicioGimnasioAdd,
    RutinaReordenarRequest,
    RutinaAsignarRequest,
    RutinaReasignarResponsableRequest,
    RutinaFinalizarAsignacionRequest,
)
from app.routers.gimnasios import ensure_active_organizational_assignments
from app.routers.socios import (
    _resolve_socio_profile_context,
    _fetch_socio_gimnasios_for_socio,
)
from app.routers.ejercicios import (
    EJERCICIO_GIMNASIO_COLUMNS,
    EJERCICIO_SOCIO_COLUMNS,
    verify_can_manage_gym_training,
)

router = APIRouter(prefix="/api/rutinas", tags=["Rutinas Personales, del Gimnasio y Asignaciones"])

RUTINA_SOCIO_SELECT = (
    "id_rutina_socio, id_socio, nombre, descripcion, fecha_inicio, fecha_fin, estado, creado_en, "
    "rutina_ejercicio_socio(id_rutina_ejercicio_socio, id_rutina_socio, id_socio, id_ejercicio_socio, "
    "series, repeticiones, peso, descanso, orden, "
    "ejercicio_socio(id_ejercicio_socio, id_socio, nombre, descripcion, grupo_muscular, dificultad, estado))"
)

RUTINA_GIMNASIO_SELECT = (
    "id_rutina_gimnasio, id_gimnasio, nombre, descripcion, nivel, estado, creado_en, "
    "rutina_ejercicio_gimnasio(id_rutina_ejercicio_gimnasio, id_rutina_gimnasio, id_gimnasio, id_ejercicio_gimnasio, "
    "series, repeticiones, peso, descanso, orden, "
    "ejercicio_gimnasio(id_ejercicio_gimnasio, id_gimnasio, nombre, descripcion, grupo_muscular, dificultad, estado))"
)

ASIGNACION_SELECT = (
    "id_rutina_asignada, id_rutina_gimnasio, id_socio_gimnasio, id_gimnasio, "
    "id_empleado_responsable, fecha_asignacion, fecha_finalizacion, estado, observaciones, creado_en, "
    "rutina_gimnasio(id_rutina_gimnasio, id_gimnasio, nombre, descripcion, nivel, estado, "
    "rutina_ejercicio_gimnasio(id_rutina_ejercicio_gimnasio, id_ejercicio_gimnasio, series, repeticiones, peso, descanso, orden, "
    "ejercicio_gimnasio(id_ejercicio_gimnasio, nombre, grupo_muscular, dificultad, descripcion))), "
    "socio_gimnasio(id_socio_gimnasio, id_socio, id_gimnasio, estado, "
    "socio(id_socio, nombre, apellido, dni, email)), "
    "empleado(id_empleado, nombre, apellido, email, estado)"
)


def _map_rutina_rpc_error(exc: Exception) -> int:
    msg = str(exc).lower()
    if any(k in msg for k in ("ya se encuentra asignada", "ya finalizada", "duplicad")):
        return status.HTTP_409_CONFLICT
    if any(k in msg for k in ("no tienes autorización", "sin permisos", "no autorizado", "no autorizados")):
        return status.HTTP_403_FORBIDDEN
    if "no existe" in msg or "no encontrada" in msg:
        return status.HTTP_404_NOT_FOUND
    return status.HTTP_400_BAD_REQUEST


def _sort_rutina_items(rutina: Dict[str, Any], key: str) -> Dict[str, Any]:
    out = dict(rutina)
    items = out.get(key)
    if isinstance(items, list):
        out[key] = sorted(items, key=lambda x: int(x.get("orden") or 0))
    else:
        out[key] = []
    return out


def _fetch_rutina_socio_or_404(db: Client, id_rutina_socio: int) -> Dict[str, Any]:
    res = (
        db.from_("rutina_socio")
        .select(RUTINA_SOCIO_SELECT)
        .eq("id_rutina_socio", id_rutina_socio)
        .maybe_single()
        .execute()
    )
    row = get_data(res)
    if not isinstance(row, dict):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rutina personal no encontrada.")
    return _sort_rutina_items(row, "rutina_ejercicio_socio")


def _fetch_rutina_gimnasio_or_404(db: Client, id_rutina_gimnasio: int) -> Dict[str, Any]:
    res = (
        db.from_("rutina_gimnasio")
        .select(RUTINA_GIMNASIO_SELECT)
        .eq("id_rutina_gimnasio", id_rutina_gimnasio)
        .maybe_single()
        .execute()
    )
    row = get_data(res)
    if not isinstance(row, dict):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rutina de gimnasio no encontrada.")
    return _sort_rutina_items(row, "rutina_ejercicio_gimnasio")


def _fetch_asignacion_or_404(db: Client, id_rutina_asignada: int) -> Dict[str, Any]:
    res = (
        db.from_("rutinas_asignadas_socio_gimnasio")
        .select(ASIGNACION_SELECT)
        .eq("id_rutina_asignada", id_rutina_asignada)
        .maybe_single()
        .execute()
    )
    row = get_data(res)
    if not isinstance(row, dict):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asignación de rutina no encontrada.")
    return row


# ==============================================================================
# 1. RUTINAS PERSONALES DEL SOCIO (`public.rutina_socio`)
# ==============================================================================

@router.get("/personales", status_code=status.HTTP_200_OK)
async def listar_rutinas_personales(
    solo_activas: bool = Query(default=False),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Lista las rutinas personales (`public.rutina_socio`) del socio autenticado."""
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]

    res = (
        db.from_("rutina_socio")
        .select(RUTINA_SOCIO_SELECT)
        .eq("id_socio", own_socio_id)
        .order("id_rutina_socio", desc=True)
        .execute()
    )
    rows = get_data(res) or []
    rutinas = [
        _sort_rutina_items(r, "rutina_ejercicio_socio")
        for r in (rows if isinstance(rows, list) else [])
        if isinstance(r, dict)
        and int(r.get("id_socio") or 0) == own_socio_id
        and (not solo_activas or r.get("estado") is True)
    ]
    return {"id_socio": own_socio_id, "total": len(rutinas), "rutinas": rutinas}


@router.get("/personales/{id_rutina_socio}", status_code=status.HTTP_200_OK)
async def obtener_rutina_personal(
    id_rutina_socio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Obtiene una rutina personal validando propiedad exclusiva del socio."""
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    rutina = _fetch_rutina_socio_or_404(socio_ctx["supabase"], id_rutina_socio)
    if int(rutina.get("id_socio") or 0) != own_socio_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes autorización para consultar una rutina personal ajena.",
        )
    return {"rutina": rutina}


@router.post("/personales", status_code=status.HTTP_201_CREATED)
async def crear_rutina_personal(
    req: RutinaSocioCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Crea una rutina personal en `public.rutina_socio` estrictamente para el socio autenticado."""
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    if req.id_socio is not None and str(req.id_socio).strip() != str(own_socio_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No puedes crear rutinas personales para otro socio.",
        )

    if req.fecha_inicio and req.fecha_fin and req.fecha_fin < req.fecha_inicio:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La fecha_fin no puede ser anterior a fecha_inicio.",
        )

    clean_nombre = req.nombre.strip()
    if not clean_nombre:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre de la rutina es obligatorio.")

    db: Client = socio_ctx["supabase"]
    nueva = {
        "id_socio": own_socio_id,
        "nombre": clean_nombre,
        "descripcion": req.descripcion.strip() if req.descripcion and req.descripcion.strip() else None,
        "fecha_inicio": req.fecha_inicio or date.today().isoformat(),
        "fecha_fin": req.fecha_fin,
        "estado": bool(req.estado),
    }
    res = db.from_("rutina_socio").insert([nueva]).execute()
    rows = get_data(res) or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al crear la rutina personal.")

    rutina = _sort_rutina_items(rows[0], "rutina_ejercicio_socio")
    return {"mensaje": "Rutina personal creada exitosamente.", "rutina": rutina}


@router.put("/personales/{id_rutina_socio}", status_code=status.HTTP_200_OK)
async def actualizar_rutina_personal(
    req: RutinaSocioUpdate,
    id_rutina_socio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Actualiza una rutina personal en `public.rutina_socio` sin permitir transferencia de propietario."""
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    if req.id_socio is not None and str(req.id_socio).strip() != str(own_socio_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No está permitido transferir una rutina personal a otro socio.",
        )

    db: Client = socio_ctx["supabase"]
    actual = _fetch_rutina_socio_or_404(db, id_rutina_socio)
    if int(actual.get("id_socio") or 0) != own_socio_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes autorización para modificar una rutina personal ajena.",
        )

    update_data: Dict[str, Any] = {}
    if req.nombre is not None:
        clean_nombre = req.nombre.strip()
        if not clean_nombre:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre no puede estar vacío.")
        update_data["nombre"] = clean_nombre
    if req.descripcion is not None:
        update_data["descripcion"] = req.descripcion.strip() or None
    if req.fecha_inicio is not None:
        update_data["fecha_inicio"] = req.fecha_inicio
    if req.fecha_fin is not None:
        update_data["fecha_fin"] = req.fecha_fin
    if req.estado is not None:
        update_data["estado"] = bool(req.estado)

    fi = update_data.get("fecha_inicio", actual.get("fecha_inicio"))
    ff = update_data.get("fecha_fin", actual.get("fecha_fin"))
    if fi and ff and str(ff) < str(fi):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La fecha_fin no puede ser anterior a fecha_inicio.",
        )
    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No se enviaron campos para actualizar.")

    res = (
        db.from_("rutina_socio")
        .update(update_data)
        .eq("id_rutina_socio", id_rutina_socio)
        .eq("id_socio", own_socio_id)
        .execute()
    )
    rows = get_data(res) or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rutina personal no encontrada.")
    return {"mensaje": "Rutina personal actualizada con éxito.", "rutina": rows[0]}


@router.delete("/personales/{id_rutina_socio}", status_code=status.HTTP_200_OK)
async def eliminar_rutina_personal(
    id_rutina_socio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Elimina una rutina personal del socio autenticado."""
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]

    actual = _fetch_rutina_socio_or_404(db, id_rutina_socio)
    if int(actual.get("id_socio") or 0) != own_socio_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes autorización para eliminar una rutina personal ajena.",
        )

    db.from_("rutina_socio").delete().eq("id_rutina_socio", id_rutina_socio).eq("id_socio", own_socio_id).execute()
    return {"mensaje": f"Rutina personal {id_rutina_socio} eliminada correctamente."}


@router.post("/personales/{id_rutina_socio}/ejercicios", status_code=status.HTTP_201_CREATED)
async def agregar_ejercicio_rutina_personal(
    req: RutinaEjercicioSocioAdd,
    id_rutina_socio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Agrega un ejercicio de `public.ejercicio_socio` a `public.rutina_ejercicio_socio`.
    Bloquea cualquier intento de mezclar un `ejercicio_gimnasio` o un ejercicio de otro socio.
    """
    if req.id_ejercicio_gimnasio is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se permite mezclar ejercicios del gimnasio en una rutina personal (`rutina_socio`).",
        )

    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]

    rutina = _fetch_rutina_socio_or_404(db, id_rutina_socio)
    if int(rutina.get("id_socio") or 0) != own_socio_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes autorización para modificar una rutina personal ajena.",
        )

    res_ej = (
        db.from_("ejercicio_socio")
        .select(EJERCICIO_SOCIO_COLUMNS)
        .eq("id_ejercicio_socio", req.id_ejercicio_socio)
        .maybe_single()
        .execute()
    )
    ej = get_data(res_ej)
    if not isinstance(ej, dict) or int(ej.get("id_socio") or 0) != own_socio_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El ejercicio especificado no existe en `ejercicio_socio` o pertenece a otro socio.",
        )

    # Validar unicidad de ejercicio y orden dentro de la rutina
    items_actuales = rutina.get("rutina_ejercicio_socio") or []
    for it in items_actuales:
        if int(it.get("id_ejercicio_socio") or 0) == req.id_ejercicio_socio:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Este ejercicio personal ya se encuentra en la rutina.",
            )
        if int(it.get("orden") or 0) == req.orden:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Ya existe un ejercicio en el orden {req.orden} de esta rutina.",
            )

    nuevo = {
        "id_rutina_socio": id_rutina_socio,
        "id_socio": own_socio_id,
        "id_ejercicio_socio": req.id_ejercicio_socio,
        "series": int(req.series),
        "repeticiones": int(req.repeticiones),
        "peso": round(float(req.peso), 2) if req.peso is not None else 0.0,
        "descanso": int(req.descanso) if req.descanso is not None else 60,
        "orden": int(req.orden),
    }
    res = db.from_("rutina_ejercicio_socio").insert([nuevo]).execute()
    rows = get_data(res) or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al agregar ejercicio a la rutina personal.")
    return {"mensaje": "Ejercicio agregado a la rutina personal.", "item": rows[0]}


@router.put("/personales/{id_rutina_socio}/ejercicios/{id_item}", status_code=status.HTTP_200_OK)
async def actualizar_ejercicio_rutina_personal(
    req: RutinaEjercicioUpdate,
    id_rutina_socio: int = Path(..., gt=0),
    id_item: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]

    rutina = _fetch_rutina_socio_or_404(db, id_rutina_socio)
    if int(rutina.get("id_socio") or 0) != own_socio_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sin autorización sobre rutina ajena.")

    update_data: Dict[str, Any] = {}
    if req.series is not None:
        update_data["series"] = int(req.series)
    if req.repeticiones is not None:
        update_data["repeticiones"] = int(req.repeticiones)
    if req.peso is not None:
        update_data["peso"] = round(float(req.peso), 2)
    if req.descanso is not None:
        update_data["descanso"] = int(req.descanso)
    if req.orden is not None:
        update_data["orden"] = int(req.orden)
    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No se enviaron campos a actualizar.")

    res = (
        db.from_("rutina_ejercicio_socio")
        .update(update_data)
        .eq("id_rutina_ejercicio_socio", id_item)
        .eq("id_rutina_socio", id_rutina_socio)
        .eq("id_socio", own_socio_id)
        .execute()
    )
    rows = get_data(res) or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ítem de rutina personal no encontrado.")
    return {"mensaje": "Ejercicio actualizado en la rutina personal.", "item": rows[0]}


@router.delete("/personales/{id_rutina_socio}/ejercicios/{id_item}", status_code=status.HTTP_200_OK)
async def eliminar_ejercicio_rutina_personal(
    id_rutina_socio: int = Path(..., gt=0),
    id_item: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]

    rutina = _fetch_rutina_socio_or_404(db, id_rutina_socio)
    if int(rutina.get("id_socio") or 0) != own_socio_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sin autorización sobre rutina ajena.")

    res = (
        db.from_("rutina_ejercicio_socio")
        .delete()
        .eq("id_rutina_ejercicio_socio", id_item)
        .eq("id_rutina_socio", id_rutina_socio)
        .eq("id_socio", own_socio_id)
        .execute()
    )
    rows = get_data(res) or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ítem de rutina personal no encontrado.")
    return {"mensaje": "Ejercicio retirado de la rutina personal."}


@router.put("/personales/{id_rutina_socio}/reordenar", status_code=status.HTTP_200_OK)
async def reordenar_rutina_personal(
    req: RutinaReordenarRequest,
    id_rutina_socio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Reordena atómicamente los ejercicios de `rutina_socio` vía `public.rpc_reordenar_rutina_socio_ejercicios`."""
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]

    rutina = _fetch_rutina_socio_or_404(db, id_rutina_socio)
    if int(rutina.get("id_socio") or 0) != own_socio_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sin autorización sobre rutina ajena.")

    try:
        res_rpc = db.rpc(
            "rpc_reordenar_rutina_socio_ejercicios",
            {
                "p_id_rutina_socio": id_rutina_socio,
                "p_ids_ordenados": [int(x) for x in req.ids_ordenados],
            },
        ).execute()
        reordenados = get_data(res_rpc)
    except Exception as exc:
        raise HTTPException(
            status_code=_map_rutina_rpc_error(exc),
            detail=f"No fue posible reordenar la rutina personal: {exc}",
        ) from exc

    return {"mensaje": "Ejercicios reordenados correctamente.", "ejercicios": reordenados}


def _resolve_socio_active_gym_context(
    current_user: Dict[str, Any],
    id_gimnasio: Optional[int] = None,
) -> tuple[Dict[str, Any], int, set[int], Optional[int]]:
    """
    Resuelve el perfil de socio y el gimnasio activo seleccionado en la app,
    verificando que posea una asociación activa (`socio_gimnasio.estado = true`).
    Rechaza con 403 cualquier intento de consultar un gimnasio donde no tenga asociación activa.
    """
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]

    req_gym = current_user.get("requested_gym_id")
    if id_gimnasio is not None and req_gym is not None and int(id_gimnasio) != int(req_gym):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No se permite cruzar gimnasios al consultar rutinas.",
        )

    asocs = _fetch_socio_gimnasios_for_socio(db, own_socio_id)
    active_gyms = {
        int(a["id_gimnasio"])
        for a in asocs
        if a.get("estado") is True and a.get("id_gimnasio") is not None
    }

    requested_gid = id_gimnasio if id_gimnasio is not None else req_gym
    if requested_gid is not None:
        resolved_gid = int(requested_gid)
        if resolved_gid not in active_gyms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No posees asociación activa con el gimnasio solicitado.",
            )
        return socio_ctx, own_socio_id, active_gyms, resolved_gid

    ctx_gid = current_user.get("id_gimnasio")
    if ctx_gid is not None and int(ctx_gid) in active_gyms:
        return socio_ctx, own_socio_id, active_gyms, int(ctx_gid)

    if active_gyms:
        return socio_ctx, own_socio_id, active_gyms, min(active_gyms)

    return socio_ctx, own_socio_id, active_gyms, None


# ==============================================================================
# 2. RUTINAS ORGANIZACIONALES DEL GIMNASIO (`public.rutina_gimnasio`)
# ==============================================================================

@router.get("/gimnasio", status_code=status.HTTP_200_OK)
@router.get("/ofrecidas", status_code=status.HTTP_200_OK)
@router.get("/ofrecidas-gym", status_code=status.HTTP_200_OK)
@router.get("/gym/ofrecidas", status_code=status.HTTP_200_OK)
async def listar_rutinas_gimnasio(
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    solo_activas: bool = Query(default=False),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Lista las plantillas de `public.rutina_gimnasio` autorizadas:
    - Para staff autorizado (dueño, admin org, admin sucursal, entrenador): catálogo completo de su gimnasio.
    - Para socio: catálogo de rutinas activas (`estado = true`) del gimnasio activo donde posee
      asociación activa (`socio_gimnasio.estado = true`), en solo lectura.
    - Rechaza a recepcionistas (`403`) en consistencia con `rutina_gimnasio_select_authorized`.
    """
    if current_user.get("rol") == "socio":
        socio_ctx, _own_socio_id, _active_gyms, target_gym_id = _resolve_socio_active_gym_context(
            current_user, id_gimnasio=id_gimnasio
        )
        if target_gym_id is None:
            return {"id_gimnasio": None, "total": 0, "rutinas": []}

        db: Client = socio_ctx["supabase"]
        res = (
            db.from_("rutina_gimnasio")
            .select(RUTINA_GIMNASIO_SELECT)
            .eq("id_gimnasio", target_gym_id)
            .eq("estado", True)
            .order("id_rutina_gimnasio", desc=True)
            .execute()
        )
        rows = get_data(res) or []
        rutinas = [
            _sort_rutina_items(r, "rutina_ejercicio_gimnasio")
            for r in (rows if isinstance(rows, list) else [])
            if isinstance(r, dict)
            and int(r.get("id_gimnasio") or 0) == target_gym_id
            and r.get("estado") is True
        ]
        return {"id_gimnasio": target_gym_id, "total": len(rutinas), "rutinas": rutinas}

    manager_ctx, gym_id = verify_can_manage_gym_training(current_user, id_gimnasio=id_gimnasio)
    db: Client = manager_ctx["supabase"]
    res = (
        db.from_("rutina_gimnasio")
        .select(RUTINA_GIMNASIO_SELECT)
        .eq("id_gimnasio", gym_id)
        .order("id_rutina_gimnasio", desc=True)
        .execute()
    )
    rows = get_data(res) or []
    rutinas = [
        _sort_rutina_items(r, "rutina_ejercicio_gimnasio")
        for r in (rows if isinstance(rows, list) else [])
        if isinstance(r, dict)
        and int(r.get("id_gimnasio") or 0) == gym_id
        and (not solo_activas or r.get("estado") is True)
    ]
    return {"id_gimnasio": gym_id, "total": len(rutinas), "rutinas": rutinas}


@router.get("/gimnasio/{id_rutina_gimnasio}", status_code=status.HTTP_200_OK)
async def obtener_rutina_gimnasio(
    id_rutina_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Obtiene el detalle de una rutina de `public.rutina_gimnasio`."""
    db: Client = current_user["supabase"]
    rutina = _fetch_rutina_gimnasio_or_404(db, id_rutina_gimnasio)

    if current_user.get("rol") == "socio":
        _socio_ctx, _own_socio_id, active_gyms, _target_gym_id = _resolve_socio_active_gym_context(current_user)
        rutina_gym_id = int(rutina.get("id_gimnasio") or 0)
        if rutina.get("estado") is True and rutina_gym_id in active_gyms:
            return {"rutina": rutina}

        asig_res = await listar_asignaciones_rutinas(
            id_gimnasio=None, id_socio=None, solo_activas=False, current_user=current_user
        )
        assigned_ids = {int(a["id_rutina_gimnasio"]) for a in asig_res["asignaciones"]}
        if id_rutina_gimnasio not in assigned_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes autorización para consultar esta rutina del gimnasio.",
            )
        return {"rutina": rutina}

    verify_can_manage_gym_training(current_user, id_gimnasio=int(rutina["id_gimnasio"]))
    return {"rutina": rutina}


@router.post("/gimnasio", status_code=status.HTTP_201_CREATED)
async def crear_rutina_gimnasio(
    req: RutinaGimnasioCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Crea una plantilla reutilizable en `public.rutina_gimnasio` (dueño, admin org, admin sucursal o entrenador)."""
    manager_ctx, gym_id = verify_can_manage_gym_training(current_user, id_gimnasio=req.id_gimnasio)
    clean_nombre = req.nombre.strip()
    if not clean_nombre:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre de la rutina es obligatorio.")

    db: Client = manager_ctx["supabase"]
    nueva = {
        "id_gimnasio": gym_id,
        "nombre": clean_nombre,
        "descripcion": req.descripcion.strip() if req.descripcion and req.descripcion.strip() else None,
        "nivel": req.nivel.strip() if req.nivel and req.nivel.strip() else None,
        "estado": bool(req.estado),
    }
    res = db.from_("rutina_gimnasio").insert([nueva]).execute()
    rows = get_data(res) or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al crear la rutina de gimnasio.")
    return {
        "mensaje": "Rutina de gimnasio creada exitosamente.",
        "rutina": _sort_rutina_items(rows[0], "rutina_ejercicio_gimnasio"),
    }


@router.put("/gimnasio/{id_rutina_gimnasio}", status_code=status.HTTP_200_OK)
async def actualizar_rutina_gimnasio(
    req: RutinaGimnasioUpdate,
    id_rutina_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Actualiza una rutina de `public.rutina_gimnasio` bloqueando transferencia de gimnasio."""
    ensure_active_organizational_assignments(current_user)
    db_initial: Client = current_user["supabase"]
    actual = _fetch_rutina_gimnasio_or_404(db_initial, id_rutina_gimnasio)
    gym_id = int(actual["id_gimnasio"])

    if req.id_gimnasio is not None and str(req.id_gimnasio).strip() != str(gym_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No está permitido transferir una rutina_gimnasio a otro gimnasio.",
        )

    manager_ctx, _ = verify_can_manage_gym_training(current_user, id_gimnasio=gym_id)
    update_data: Dict[str, Any] = {}
    if req.nombre is not None:
        clean_nombre = req.nombre.strip()
        if not clean_nombre:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre no puede estar vacío.")
        update_data["nombre"] = clean_nombre
    if req.descripcion is not None:
        update_data["descripcion"] = req.descripcion.strip() or None
    if req.nivel is not None:
        update_data["nivel"] = req.nivel.strip() or None
    if req.estado is not None:
        update_data["estado"] = bool(req.estado)
    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No se enviaron campos a actualizar.")

    db: Client = manager_ctx["supabase"]
    res = (
        db.from_("rutina_gimnasio")
        .update(update_data)
        .eq("id_rutina_gimnasio", id_rutina_gimnasio)
        .eq("id_gimnasio", gym_id)
        .execute()
    )
    rows = get_data(res) or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rutina de gimnasio no encontrada.")
    return {"mensaje": "Rutina de gimnasio actualizada con éxito.", "rutina": rows[0]}


@router.delete("/gimnasio/{id_rutina_gimnasio}", status_code=status.HTTP_200_OK)
async def desactivar_o_eliminar_rutina_gimnasio(
    id_rutina_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Si la rutina de gimnasio tiene asignaciones históricas (`ON DELETE RESTRICT`), la desactiva
    lógicamente (`estado = false`) para preservar el historial; si nunca fue asignada, permite eliminarla.
    """
    ensure_active_organizational_assignments(current_user)
    db_initial: Client = current_user["supabase"]
    actual = _fetch_rutina_gimnasio_or_404(db_initial, id_rutina_gimnasio)
    gym_id = int(actual["id_gimnasio"])
    manager_ctx, _ = verify_can_manage_gym_training(current_user, id_gimnasio=gym_id)

    db: Client = manager_ctx["supabase"]
    res_asig = (
        db.from_("rutinas_asignadas_socio_gimnasio")
        .select("id_rutina_asignada")
        .eq("id_rutina_gimnasio", id_rutina_gimnasio)
        .execute()
    )
    asigs = get_data(res_asig) or []
    if manager_ctx.get("rol") == "empleado" or (isinstance(asigs, list) and len(asigs) > 0):
        res_upd = (
            db.from_("rutina_gimnasio")
            .update({"estado": False})
            .eq("id_rutina_gimnasio", id_rutina_gimnasio)
            .eq("id_gimnasio", gym_id)
            .execute()
        )
        rows = get_data(res_upd) or []
        return {
            "mensaje": "Rutina de gimnasio desactivada lógicamente para conservar su historial y permitir reutilizarla.",
            "desactivada": True,
            "rutina": rows[0] if rows else actual,
        }

    db.from_("rutina_gimnasio").delete().eq("id_rutina_gimnasio", id_rutina_gimnasio).eq("id_gimnasio", gym_id).execute()
    return {"mensaje": f"Rutina de gimnasio {id_rutina_gimnasio} eliminada correctamente.", "eliminada": True}


@router.post("/gimnasio/{id_rutina_gimnasio}/ejercicios", status_code=status.HTTP_201_CREATED)
async def agregar_ejercicio_rutina_gimnasio(
    req: RutinaEjercicioGimnasioAdd,
    id_rutina_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Agrega un ejercicio de `public.ejercicio_gimnasio` a `public.rutina_ejercicio_gimnasio`.
    Impide mezclar ejercicios personales (`ejercicio_socio`) o ejercicios de otro gimnasio.
    """
    if req.id_ejercicio_socio is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se permite mezclar ejercicios personales (`ejercicio_socio`) en una rutina del gimnasio.",
        )

    ensure_active_organizational_assignments(current_user)
    db_initial: Client = current_user["supabase"]
    rutina = _fetch_rutina_gimnasio_or_404(db_initial, id_rutina_gimnasio)
    gym_id = int(rutina["id_gimnasio"])
    manager_ctx, _ = verify_can_manage_gym_training(current_user, id_gimnasio=gym_id)

    db: Client = manager_ctx["supabase"]
    res_ej = (
        db.from_("ejercicio_gimnasio")
        .select(EJERCICIO_GIMNASIO_COLUMNS)
        .eq("id_ejercicio_gimnasio", req.id_ejercicio_gimnasio)
        .maybe_single()
        .execute()
    )
    ej = get_data(res_ej)
    if not isinstance(ej, dict) or int(ej.get("id_gimnasio") or 0) != gym_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El ejercicio especificado no existe o no pertenece al gimnasio de la rutina.",
        )

    for it in (rutina.get("rutina_ejercicio_gimnasio") or []):
        if int(it.get("id_ejercicio_gimnasio") or 0) == req.id_ejercicio_gimnasio:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Este ejercicio ya está en la rutina.")
        if int(it.get("orden") or 0) == req.orden:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"El orden {req.orden} ya está ocupado.")

    nuevo = {
        "id_rutina_gimnasio": id_rutina_gimnasio,
        "id_gimnasio": gym_id,
        "id_ejercicio_gimnasio": req.id_ejercicio_gimnasio,
        "series": int(req.series),
        "repeticiones": int(req.repeticiones),
        "peso": round(float(req.peso), 2) if req.peso is not None else 0.0,
        "descanso": int(req.descanso) if req.descanso is not None else 60,
        "orden": int(req.orden),
    }
    res = db.from_("rutina_ejercicio_gimnasio").insert([nuevo]).execute()
    rows = get_data(res) or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al asociar ejercicio a la rutina de gimnasio.")
    return {"mensaje": "Ejercicio agregado a la rutina del gimnasio.", "item": rows[0]}


@router.put("/gimnasio/{id_rutina_gimnasio}/ejercicios/{id_item}", status_code=status.HTTP_200_OK)
async def actualizar_ejercicio_rutina_gimnasio(
    req: RutinaEjercicioUpdate,
    id_rutina_gimnasio: int = Path(..., gt=0),
    id_item: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    ensure_active_organizational_assignments(current_user)
    db_initial: Client = current_user["supabase"]
    rutina = _fetch_rutina_gimnasio_or_404(db_initial, id_rutina_gimnasio)
    gym_id = int(rutina["id_gimnasio"])
    manager_ctx, _ = verify_can_manage_gym_training(current_user, id_gimnasio=gym_id)

    update_data: Dict[str, Any] = {}
    if req.series is not None:
        update_data["series"] = int(req.series)
    if req.repeticiones is not None:
        update_data["repeticiones"] = int(req.repeticiones)
    if req.peso is not None:
        update_data["peso"] = round(float(req.peso), 2)
    if req.descanso is not None:
        update_data["descanso"] = int(req.descanso)
    if req.orden is not None:
        update_data["orden"] = int(req.orden)
    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No se enviaron campos a actualizar.")

    db: Client = manager_ctx["supabase"]
    res = (
        db.from_("rutina_ejercicio_gimnasio")
        .update(update_data)
        .eq("id_rutina_ejercicio_gimnasio", id_item)
        .eq("id_rutina_gimnasio", id_rutina_gimnasio)
        .eq("id_gimnasio", gym_id)
        .execute()
    )
    rows = get_data(res) or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ítem de rutina de gimnasio no encontrado.")
    return {"mensaje": "Ejercicio actualizado en la rutina del gimnasio.", "item": rows[0]}


@router.delete("/gimnasio/{id_rutina_gimnasio}/ejercicios/{id_item}", status_code=status.HTTP_200_OK)
async def eliminar_ejercicio_rutina_gimnasio(
    id_rutina_gimnasio: int = Path(..., gt=0),
    id_item: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    ensure_active_organizational_assignments(current_user)
    db_initial: Client = current_user["supabase"]
    rutina = _fetch_rutina_gimnasio_or_404(db_initial, id_rutina_gimnasio)
    gym_id = int(rutina["id_gimnasio"])
    manager_ctx, _ = verify_can_manage_gym_training(current_user, id_gimnasio=gym_id)

    db: Client = manager_ctx["supabase"]
    res = (
        db.from_("rutina_ejercicio_gimnasio")
        .delete()
        .eq("id_rutina_ejercicio_gimnasio", id_item)
        .eq("id_rutina_gimnasio", id_rutina_gimnasio)
        .eq("id_gimnasio", gym_id)
        .execute()
    )
    rows = get_data(res) or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ítem de rutina de gimnasio no encontrado.")
    return {"mensaje": "Ejercicio retirado de la rutina del gimnasio."}


@router.put("/gimnasio/{id_rutina_gimnasio}/reordenar", status_code=status.HTTP_200_OK)
async def reordenar_rutina_gimnasio(
    req: RutinaReordenarRequest,
    id_rutina_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Reordena atómicamente los ejercicios de `rutina_gimnasio` vía `public.rpc_reordenar_rutina_gimnasio_ejercicios`."""
    ensure_active_organizational_assignments(current_user)
    db_initial: Client = current_user["supabase"]
    rutina = _fetch_rutina_gimnasio_or_404(db_initial, id_rutina_gimnasio)
    manager_ctx, _ = verify_can_manage_gym_training(current_user, id_gimnasio=int(rutina["id_gimnasio"]))
    db: Client = manager_ctx["supabase"]

    try:
        res_rpc = db.rpc(
            "rpc_reordenar_rutina_gimnasio_ejercicios",
            {
                "p_id_rutina_gimnasio": id_rutina_gimnasio,
                "p_ids_ordenados": [int(x) for x in req.ids_ordenados],
            },
        ).execute()
        reordenados = get_data(res_rpc)
    except Exception as exc:
        raise HTTPException(
            status_code=_map_rutina_rpc_error(exc),
            detail=f"No fue posible reordenar la rutina de gimnasio: {exc}",
        ) from exc

    return {"mensaje": "Ejercicios de rutina de gimnasio reordenados correctamente.", "ejercicios": reordenados}


# ==============================================================================
# 3. ASIGNACIONES DE RUTINAS (`public.rutinas_asignadas_socio_gimnasio` + RPCs)
# ==============================================================================

@router.get("/asignaciones", status_code=status.HTTP_200_OK)
async def listar_asignaciones_rutinas(
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    id_socio: Optional[int] = Query(default=None, gt=0),
    solo_activas: bool = Query(default=False),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Lista asignaciones de `public.rutinas_asignadas_socio_gimnasio`:
    - Socio: únicamente sus propias asignaciones multi-gimnasio (rechaza `403` si intenta consultar otro `id_socio`).
    - Staff (dueño, admin org, admin sucursal, entrenador): asignaciones de su gimnasio (rechaza `403` a recepcionistas).
    """
    if current_user.get("rol") == "socio":
        socio_ctx = _resolve_socio_profile_context(current_user)
        own_socio_id = int(socio_ctx["id_socio"])
        if id_socio is not None and int(id_socio) != own_socio_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes autorización para consultar asignaciones de rutina de otro socio.",
            )
        db: Client = socio_ctx["supabase"]
        asocs = _fetch_socio_gimnasios_for_socio(db, own_socio_id)
        sg_ids_by_gym = {
            int(a["id_gimnasio"]): int(a["id_socio_gimnasio"])
            for a in asocs
            if a.get("id_socio_gimnasio") is not None and a.get("id_gimnasio") is not None
        }
        if id_gimnasio is not None and id_gimnasio not in sg_ids_by_gym:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No posees asociación con el gimnasio solicitado.",
            )
        target_sg_ids = [sg_ids_by_gym[id_gimnasio]] if id_gimnasio is not None else list(sg_ids_by_gym.values())
        if not target_sg_ids:
            return {"id_socio": own_socio_id, "total": 0, "asignaciones": []}

        q = db.from_("rutinas_asignadas_socio_gimnasio").select(ASIGNACION_SELECT)
        if len(target_sg_ids) == 1:
            q = q.eq("id_socio_gimnasio", target_sg_ids[0])
        else:
            q = q.in_("id_socio_gimnasio", target_sg_ids)
        res = q.order("id_rutina_asignada", desc=True).execute()
        rows = get_data(res) or []
        asignaciones = [
            a for a in (rows if isinstance(rows, list) else [])
            if isinstance(a, dict)
            and int(a.get("id_socio_gimnasio") or 0) in set(target_sg_ids)
            and (not solo_activas or a.get("estado") is True)
        ]
        return {"id_socio": own_socio_id, "total": len(asignaciones), "asignaciones": asignaciones}

    manager_ctx, gym_id = verify_can_manage_gym_training(current_user, id_gimnasio=id_gimnasio)
    db = manager_ctx["supabase"]
    res = (
        db.from_("rutinas_asignadas_socio_gimnasio")
        .select(ASIGNACION_SELECT)
        .eq("id_gimnasio", gym_id)
        .order("id_rutina_asignada", desc=True)
        .execute()
    )
    rows = get_data(res) or []
    asignaciones: List[Dict[str, Any]] = []
    for a in (rows if isinstance(rows, list) else []):
        if not isinstance(a, dict) or int(a.get("id_gimnasio") or 0) != gym_id:
            continue
        if solo_activas and a.get("estado") is not True:
            continue
        if id_socio is not None:
            sg = a.get("socio_gimnasio") if isinstance(a.get("socio_gimnasio"), dict) else {}
            if int(sg.get("id_socio") or 0) != id_socio:
                continue
        asignaciones.append(a)
    return {"id_gimnasio": gym_id, "total": len(asignaciones), "asignaciones": asignaciones}


@router.post("/asignaciones", status_code=status.HTTP_201_CREATED)
async def asignar_rutina_a_socio(
    req: RutinaAsignarRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Asigna una rutina existente de `public.rutina_gimnasio` a una asociación `public.socio_gimnasio`
    sin duplicar la rutina, mediante `public.rpc_asignar_rutina_gimnasio_socio`.
    """
    ensure_active_organizational_assignments(current_user)
    db_initial: Client = current_user["supabase"]
    rutina = _fetch_rutina_gimnasio_or_404(db_initial, req.id_rutina_gimnasio)
    gym_id = int(rutina["id_gimnasio"])
    manager_ctx, _ = verify_can_manage_gym_training(current_user, id_gimnasio=gym_id)

    db: Client = manager_ctx["supabase"]
    try:
        res_rpc = db.rpc(
            "rpc_asignar_rutina_gimnasio_socio",
            {
                "p_id_rutina_gimnasio": req.id_rutina_gimnasio,
                "p_id_socio_gimnasio": req.id_socio_gimnasio,
                "p_fecha_finalizacion": req.fecha_finalizacion,
                "p_observaciones": req.observaciones.strip() if req.observaciones and req.observaciones.strip() else None,
                "p_id_empleado_responsable": req.id_empleado_responsable,
            },
        ).execute()
        asignacion = get_data(res_rpc)
    except Exception as exc:
        raise HTTPException(
            status_code=_map_rutina_rpc_error(exc),
            detail=f"No fue posible asignar la rutina: {exc}",
        ) from exc

    return {
        "mensaje": "Rutina asignada exitosamente al socio sin duplicar la plantilla.",
        "asignacion": asignacion,
    }


@router.patch("/asignaciones/{id_rutina_asignada}/responsable", status_code=status.HTTP_200_OK)
async def reasignar_responsable_rutina(
    req: RutinaReasignarResponsableRequest,
    id_rutina_asignada: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Reasigna el empleado responsable de una asignación activa mediante
    `public.rpc_reasignar_responsable_rutina_asignada`.
    Un empleado desactivado no puede recibir nuevas asignaciones ni reasignaciones.
    """
    ensure_active_organizational_assignments(current_user)
    db_initial: Client = current_user["supabase"]
    asig = _fetch_asignacion_or_404(db_initial, id_rutina_asignada)
    manager_ctx, _ = verify_can_manage_gym_training(current_user, id_gimnasio=int(asig["id_gimnasio"]))

    db: Client = manager_ctx["supabase"]
    try:
        res_rpc = db.rpc(
            "rpc_reasignar_responsable_rutina_asignada",
            {
                "p_id_rutina_asignada": id_rutina_asignada,
                "p_id_empleado_responsable": req.id_empleado_responsable,
                "p_observaciones": req.observaciones.strip() if req.observaciones and req.observaciones.strip() else None,
            },
        ).execute()
        updated = get_data(res_rpc)
    except Exception as exc:
        raise HTTPException(
            status_code=_map_rutina_rpc_error(exc),
            detail=f"No fue posible reasignar el responsable: {exc}",
        ) from exc

    return {
        "mensaje": "Responsable de rutina reasignado exitosamente.",
        "asignacion": updated,
    }


@router.patch("/asignaciones/{id_rutina_asignada}/finalizar", status_code=status.HTTP_200_OK)
async def finalizar_asignacion_rutina(
    id_rutina_asignada: int = Path(..., gt=0),
    req: Optional[RutinaFinalizarAsignacionRequest] = Body(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Finaliza una asignación de rutina mediante `public.rpc_finalizar_rutina_asignada`.
    Conserva intacto `id_empleado_responsable` como responsable histórico incluso si ese
    empleado fue desactivado posteriormente.
    """
    ensure_active_organizational_assignments(current_user)
    db_initial: Client = current_user["supabase"]
    asig = _fetch_asignacion_or_404(db_initial, id_rutina_asignada)
    manager_ctx, _ = verify_can_manage_gym_training(current_user, id_gimnasio=int(asig["id_gimnasio"]))

    req_data = req or RutinaFinalizarAsignacionRequest()
    db: Client = manager_ctx["supabase"]
    try:
        res_rpc = db.rpc(
            "rpc_finalizar_rutina_asignada",
            {
                "p_id_rutina_asignada": id_rutina_asignada,
                "p_fecha_finalizacion": req_data.fecha_finalizacion or date.today().isoformat(),
                "p_observaciones": req_data.observaciones.strip() if req_data.observaciones and req_data.observaciones.strip() else None,
            },
        ).execute()
        updated = get_data(res_rpc)
    except Exception as exc:
        raise HTTPException(
            status_code=_map_rutina_rpc_error(exc),
            detail=f"No fue posible finalizar la asignación de rutina: {exc}",
        ) from exc

    return {
        "mensaje": "Asignación de rutina finalizada conservando el responsable histórico.",
        "asignacion": updated,
    }


# ==============================================================================
# 4. VISTA CONSOLIDADA (`GET /api/rutinas`)
# ==============================================================================

@router.get("", status_code=status.HTTP_200_OK)
async def get_rutinas_resumen(
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    solo_activas: bool = Query(default=False),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Endpoint unificado de lectura:
    - Socio: devuelve separadamente `rutinas_personales` (`rutina_socio`), `rutinas_asignadas`
      (`rutinas_asignadas_socio_gimnasio`) y `rutinas_gimnasio` (plantillas activas de su gimnasio activo).
    - Staff autorizado (dueño, admin org, admin sucursal, entrenador): devuelve `rutinas_gimnasio` y `asignaciones` del gimnasio.
    """
    if current_user.get("rol") == "socio":
        effective_gym_id = id_gimnasio if id_gimnasio is not None else current_user.get("requested_gym_id")
        pers = await listar_rutinas_personales(solo_activas=solo_activas, current_user=current_user)
        asig = await listar_asignaciones_rutinas(
            id_gimnasio=effective_gym_id, id_socio=None, solo_activas=solo_activas, current_user=current_user
        )
        gym_res = await listar_rutinas_gimnasio(
            id_gimnasio=id_gimnasio, solo_activas=True, current_user=current_user
        )
        return {
            "id_socio": pers["id_socio"],
            "id_gimnasio": gym_res.get("id_gimnasio"),
            "rutinas_personales": pers["rutinas"],
            "rutinas_asignadas": asig["asignaciones"],
            "rutinas_gimnasio": gym_res["rutinas"],
        }

    gym_res = await listar_rutinas_gimnasio(
        id_gimnasio=id_gimnasio, solo_activas=solo_activas, current_user=current_user
    )
    asig_res = await listar_asignaciones_rutinas(
        id_gimnasio=gym_res["id_gimnasio"], id_socio=None, solo_activas=solo_activas, current_user=current_user
    )
    return {
        "id_gimnasio": gym_res["id_gimnasio"],
        "rutinas_gimnasio": gym_res["rutinas"],
        "asignaciones": asig_res["asignaciones"],
    }
