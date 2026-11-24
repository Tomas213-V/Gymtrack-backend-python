from typing import Optional, Dict, Any, List, Set, Tuple
from fastapi import APIRouter, HTTPException, status, Query, Path, Depends
from supabase import Client

from app.core.dependencies import (
    get_current_user,
    _switch_profile_context_if_available,
    run_db_parallel,
)
from app.services.utils import get_data
from app.models.ejercicio import (
    EjercicioGimnasioCreate,
    EjercicioGimnasioUpdate,
    EjercicioSocioCreate,
)
from app.routers.gimnasios import (
    ensure_active_organizational_assignments,
    verify_can_read_gimnasio,
)
from app.routers.socios import (
    _resolve_socio_profile_context,
    _fetch_socio_gimnasios_for_socio,
)

router = APIRouter(prefix="/api/ejercicios", tags=["Ejercicios del Gimnasio"])

EJERCICIO_GIMNASIO_COLUMNS = (
    "id_ejercicio_gimnasio, id_gimnasio, nombre, descripcion, grupo_muscular, dificultad, estado, creado_en"
)
EJERCICIO_SOCIO_COLUMNS = (
    "id_ejercicio_socio, id_socio, nombre, descripcion, grupo_muscular, dificultad, estado, creado_en"
)


def verify_can_manage_gym_training(
    current_user: Dict[str, Any],
    id_gimnasio: Optional[int] = None,
) -> Tuple[Dict[str, Any], int]:
    """
    Valida permisos de escritura sobre `public.ejercicio_gimnasio`, `public.rutina_gimnasio`,
    `public.rutina_ejercicio_gimnasio` y asignaciones de rutinas, en correspondencia estricta
    con las políticas RLS `*_write_authorized`:
      - Dueño del gimnasio.
      - Administrador organizacional activo del gimnasio.
      - Empleado activo en ese gimnasio con rol `'administrador'` o `'entrenador'`.
    Rechaza estrictamente a `recepcionista`, `socio`, empleados desactivados y terceros con `403`.
    """
    ensure_active_organizational_assignments(current_user)

    req_gym = current_user.get("requested_gym_id")
    if id_gimnasio is not None and req_gym is not None and int(id_gimnasio) != int(req_gym):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No se permite cruzar gimnasios al gestionar ejercicios o rutinas.",
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
            roles_gym = {
                r.lower() for r in ((ctx.get("roles_empleado_por_gimnasio") or {}).get(resolved) or [])
            }
            return resolved if roles_gym.intersection({"administrador", "recepcionista", "entrenador"}) else None
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
        detail="No tienes permisos para crear o modificar ejercicios/rutinas de este gimnasio (requiere dueño, administrador organizacional o empleado activo del gimnasio).",
    )


def _enrich_gym_ejercicio(ej: Dict[str, Any]) -> Dict[str, Any]:
    return {
        **ej,
        "origen": "Gimnasio",
        "tipo_origen": "gimnasio",
        "es_gimnasio": True,
        "es_propio": False,
    }


def _enrich_socio_ejercicio(ej: Dict[str, Any]) -> Dict[str, Any]:
    return {
        **ej,
        "origen": "Mi ejercicio",
        "tipo_origen": "propio",
        "es_gimnasio": False,
        "es_propio": True,
    }


def _filter_ejercicios(
    rows: List[Dict[str, Any]],
    *,
    grupo_muscular: Optional[str] = None,
    search: Optional[str] = None,
    only_active: bool = False,
) -> List[Dict[str, Any]]:
    gm_norm = grupo_muscular.strip().lower() if grupo_muscular and grupo_muscular.strip().lower() != "todos" else None
    s_norm = search.strip().lower() if search and search.strip() else None
    out: List[Dict[str, Any]] = []
    for ej in rows:
        if not isinstance(ej, dict):
            continue
        if only_active and ej.get("estado") is not True:
            continue
        if gm_norm and gm_norm not in str(ej.get("grupo_muscular") or "").lower():
            continue
        if s_norm and s_norm not in str(ej.get("nombre") or "").lower():
            continue
        out.append(ej)
    return out


def _fetch_ejercicio_gym_or_404(db: Client, id_ejercicio_gimnasio: int) -> Dict[str, Any]:
    res = (
        db.from_("ejercicio_gimnasio")
        .select(EJERCICIO_GIMNASIO_COLUMNS)
        .eq("id_ejercicio_gimnasio", id_ejercicio_gimnasio)
        .maybe_single()
        .execute()
    )
    ej = get_data(res)
    if not isinstance(ej, dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ejercicio de gimnasio no encontrado.",
        )
    return ej


@router.get("/disponibles", status_code=status.HTTP_200_OK)
@router.get("/disponibles-socio", status_code=status.HTTP_200_OK)
async def get_ejercicios_disponibles_socio(
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    origen: str = Query(default="todos"),
    grupo_muscular: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Devuelve los ejercicios personales (`public.ejercicio_socio`) y/o los del gimnasio asociado
    (`public.ejercicio_gimnasio`) claramente separados por tipo y tabla de origen.
    """
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]
    access_token = socio_ctx.get("access_token")

    [asocs] = await run_db_parallel(
        db,
        access_token,
        [lambda c: _fetch_socio_gimnasios_for_socio(c, own_socio_id)],
    )
    active_gym_ids = {
        int(a["id_gimnasio"])
        for a in asocs
        if a.get("estado") is True and a.get("id_gimnasio") is not None
    }
    if id_gimnasio is not None and id_gimnasio not in active_gym_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No posees una asociación activa en el gimnasio solicitado.",
        )

    origen_norm = origen.strip().lower()
    ejercicios_propios: List[Dict[str, Any]] = []
    ejercicios_gimnasio: List[Dict[str, Any]] = []
    target_gyms = [id_gimnasio] if id_gimnasio is not None else sorted(active_gym_ids)

    fetch_propios = origen_norm in ("todos", "propio")
    fetch_gimnasio = origen_norm in ("todos", "gimnasio") and bool(target_gyms)

    def _q_propios(c: Client):
        return get_data(
            c.from_("ejercicio_socio")
            .select(EJERCICIO_SOCIO_COLUMNS)
            .eq("id_socio", own_socio_id)
            .order("nombre", desc=False)
            .execute()
        ) or []

    def _q_gimnasio(c: Client):
        q_g = c.from_("ejercicio_gimnasio").select(EJERCICIO_GIMNASIO_COLUMNS)
        if len(target_gyms) == 1:
            q_g = q_g.eq("id_gimnasio", target_gyms[0])
        else:
            q_g = q_g.in_("id_gimnasio", target_gyms)
        return get_data(q_g.order("nombre", desc=False).execute()) or []

    tasks = []
    if fetch_propios:
        tasks.append(_q_propios)
    if fetch_gimnasio:
        tasks.append(_q_gimnasio)

    results = await run_db_parallel(db, access_token, tasks)
    idx = 0
    if fetch_propios:
        raw_p = results[idx]; idx += 1
        ejercicios_propios = [
            _enrich_socio_ejercicio(e)
            for e in _filter_ejercicios(
                raw_p if isinstance(raw_p, list) else [],
                grupo_muscular=grupo_muscular,
                search=search,
            )
            if int(e.get("id_socio") or 0) == own_socio_id
        ]

    if fetch_gimnasio:
        raw_g = results[idx]
        ejercicios_gimnasio = [
            _enrich_gym_ejercicio(e)
            for e in _filter_ejercicios(
                raw_g if isinstance(raw_g, list) else [],
                grupo_muscular=grupo_muscular,
                search=search,
                only_active=True,
            )
            if int(e.get("id_gimnasio") or 0) in set(target_gyms)
        ]

    return {
        "id_socio": own_socio_id,
        "ejercicios_socio": ejercicios_propios,
        "ejercicios_gimnasio": ejercicios_gimnasio,
        "total": len(ejercicios_propios) + len(ejercicios_gimnasio),
    }


@router.get("", status_code=status.HTTP_200_OK)
async def get_ejercicios_gimnasio(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    grupo_muscular: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    solo_activos: bool = Query(default=False),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Lista el catálogo de ejercicios organizacionales (`public.ejercicio_gimnasio`) de un gimnasio autorizado."""
    ensure_active_organizational_assignments(current_user)
    target_gym_id = id_gimnasio if id_gimnasio is not None else current_user.get("id_gimnasio")
    if not target_gym_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes indicar id_gimnasio para consultar el catálogo de ejercicios del gimnasio.",
        )

    user_ctx = verify_can_read_gimnasio(current_user, int(target_gym_id))
    only_active_effective = True if user_ctx.get("rol") == "socio" else solo_activos
    db: Client = user_ctx["supabase"]

    res = (
        db.from_("ejercicio_gimnasio")
        .select(EJERCICIO_GIMNASIO_COLUMNS)
        .eq("id_gimnasio", int(target_gym_id))
        .order("id_ejercicio_gimnasio", desc=True)
        .execute()
    )
    rows = get_data(res) or []
    filtrados = [
        _enrich_gym_ejercicio(e)
        for e in _filter_ejercicios(
            rows if isinstance(rows, list) else [],
            grupo_muscular=grupo_muscular,
            search=search,
            only_active=only_active_effective,
        )
        if int(e.get("id_gimnasio") or 0) == int(target_gym_id)
    ]
    total = len(filtrados)
    offset = (page - 1) * limit
    return {
        "id_gimnasio": int(target_gym_id),
        "total": total,
        "pagina": page,
        "limite": limit,
        "datos": filtrados[offset : offset + limit],
    }


@router.get("/{id_ejercicio_gimnasio}", status_code=status.HTTP_200_OK)
async def get_ejercicio_gimnasio_by_id(
    id_ejercicio_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Obtiene un ejercicio de `public.ejercicio_gimnasio` validando pertenencia al gimnasio autorizado."""
    ensure_active_organizational_assignments(current_user)
    db: Client = current_user["supabase"]
    ej = _fetch_ejercicio_gym_or_404(db, id_ejercicio_gimnasio)

    gym_id = int(ej["id_gimnasio"])
    user_ctx = verify_can_read_gimnasio(current_user, gym_id)
    if user_ctx.get("rol") == "socio" and ej.get("estado") is not True:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El ejercicio solicitado se encuentra inactivo.",
        )
    return _enrich_gym_ejercicio(ej)


@router.post("/personalizado", status_code=status.HTTP_201_CREATED)
async def crear_ejercicio_personalizado(
    req: EjercicioSocioCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Crea un ejercicio personal en `public.ejercicio_socio` estrictamente para el socio autenticado."""
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    if req.id_socio is not None and str(req.id_socio).strip() != str(own_socio_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No puedes crear ejercicios personales para otro socio.",
        )

    clean_nombre = req.nombre.strip()
    if not clean_nombre:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre del ejercicio es obligatorio.")

    db: Client = socio_ctx["supabase"]
    nuevo = {
        "id_socio": own_socio_id,
        "nombre": clean_nombre,
        "grupo_muscular": req.grupo_muscular.strip() if req.grupo_muscular and req.grupo_muscular.strip() else "General",
        "descripcion": req.descripcion.strip() if req.descripcion and req.descripcion.strip() else None,
        "dificultad": req.dificultad.strip() if req.dificultad and req.dificultad.strip() else "Intermedia",
        "estado": bool(req.estado),
    }
    res = db.from_("ejercicio_socio").insert([nuevo]).execute()
    rows = get_data(res) or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al crear el ejercicio personal.")
    return {
        "mensaje": "Ejercicio personal creado con éxito.",
        "ejercicio": _enrich_socio_ejercicio(rows[0]),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def crear_ejercicio_gimnasio(
    req: EjercicioGimnasioCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Crea un ejercicio organizacional en `public.ejercicio_gimnasio` (dueño, admin org, admin sucursal o entrenador)."""
    manager_ctx, gym_id = verify_can_manage_gym_training(current_user, id_gimnasio=req.id_gimnasio)
    clean_nombre = req.nombre.strip()
    if not clean_nombre:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre del ejercicio es obligatorio.")

    db: Client = manager_ctx["supabase"]
    nuevo = {
        "id_gimnasio": gym_id,
        "nombre": clean_nombre,
        "grupo_muscular": req.grupo_muscular.strip() if req.grupo_muscular and req.grupo_muscular.strip() else "General",
        "descripcion": req.descripcion.strip() if req.descripcion and req.descripcion.strip() else None,
        "dificultad": req.dificultad.strip() if req.dificultad and req.dificultad.strip() else "Intermedia",
        "estado": bool(req.estado),
    }
    res = db.from_("ejercicio_gimnasio").insert([nuevo]).execute()
    rows = get_data(res) or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al crear ejercicio del gimnasio.")
    return {
        "mensaje": "Ejercicio del gimnasio creado con éxito.",
        "ejercicio": _enrich_gym_ejercicio(rows[0]),
    }


@router.put("/{id_ejercicio_gimnasio}", status_code=status.HTTP_200_OK)
async def actualizar_ejercicio_gimnasio(
    req: EjercicioGimnasioUpdate,
    id_ejercicio_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Actualiza un ejercicio de `public.ejercicio_gimnasio` sin permitir transferirlo a otro gimnasio."""
    ensure_active_organizational_assignments(current_user)
    db_initial: Client = current_user["supabase"]
    ej_actual = _fetch_ejercicio_gym_or_404(db_initial, id_ejercicio_gimnasio)
    gym_id = int(ej_actual["id_gimnasio"])

    if req.id_gimnasio is not None and str(req.id_gimnasio).strip() != str(gym_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No está permitido transferir un ejercicio_gimnasio a otro gimnasio.",
        )

    manager_ctx, _ = verify_can_manage_gym_training(current_user, id_gimnasio=gym_id)
    update_data: Dict[str, Any] = {}
    if req.nombre is not None:
        clean_nombre = req.nombre.strip()
        if not clean_nombre:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre no puede estar vacío.")
        update_data["nombre"] = clean_nombre
    if req.grupo_muscular is not None:
        update_data["grupo_muscular"] = req.grupo_muscular.strip() or None
    if req.descripcion is not None:
        update_data["descripcion"] = req.descripcion.strip() or None
    if req.dificultad is not None:
        update_data["dificultad"] = req.dificultad.strip() or None
    if req.estado is not None:
        update_data["estado"] = bool(req.estado)

    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No se enviaron campos para actualizar.")

    db: Client = manager_ctx["supabase"]
    res = (
        db.from_("ejercicio_gimnasio")
        .update(update_data)
        .eq("id_ejercicio_gimnasio", id_ejercicio_gimnasio)
        .eq("id_gimnasio", gym_id)
        .execute()
    )
    rows = get_data(res) or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ejercicio no encontrado para actualizar.")
    return {
        "mensaje": "Ejercicio del gimnasio actualizado con éxito.",
        "ejercicio": _enrich_gym_ejercicio(rows[0]),
    }


@router.delete("/{id_ejercicio_gimnasio}", status_code=status.HTTP_200_OK)
async def eliminar_ejercicio_gimnasio(
    id_ejercicio_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Elimina un ejercicio de `public.ejercicio_gimnasio` verificando que no esté referenciado en `rutina_ejercicio_gimnasio`."""
    ensure_active_organizational_assignments(current_user)
    db_initial: Client = current_user["supabase"]
    ej_actual = _fetch_ejercicio_gym_or_404(db_initial, id_ejercicio_gimnasio)
    gym_id = int(ej_actual["id_gimnasio"])
    manager_ctx, _ = verify_can_manage_gym_training(current_user, id_gimnasio=gym_id)

    db: Client = manager_ctx["supabase"]
    if manager_ctx.get("rol") == "empleado":
        res_upd = (
            db.from_("ejercicio_gimnasio")
            .update({"estado": False})
            .eq("id_ejercicio_gimnasio", id_ejercicio_gimnasio)
            .eq("id_gimnasio", gym_id)
            .execute()
        )
        rows = get_data(res_upd) or []
        return {
            "mensaje": "Ejercicio del gimnasio desactivado lógicamente para conservar su historial.",
            "desactivado": True,
            "ejercicio": _enrich_gym_ejercicio(rows[0]) if rows else _enrich_gym_ejercicio(ej_actual),
        }

    res_uso = (
        db.from_("rutina_ejercicio_gimnasio")
        .select("id_rutina_ejercicio_gimnasio")
        .eq("id_ejercicio_gimnasio", id_ejercicio_gimnasio)
        .execute()
    )
    usos = get_data(res_uso) or []
    if isinstance(usos, list) and len(usos) > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"No se puede eliminar el ejercicio porque está en uso en {len(usos)} rutina(s) del gimnasio. Desactívalo (`estado = false`) o retíralo primero.",
        )

    res_del = (
        db.from_("ejercicio_gimnasio")
        .delete()
        .eq("id_ejercicio_gimnasio", id_ejercicio_gimnasio)
        .eq("id_gimnasio", gym_id)
        .execute()
    )
    rows = get_data(res_del) or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ejercicio no encontrado para eliminar.")
    return {"mensaje": "Ejercicio del gimnasio eliminado con éxito."}
