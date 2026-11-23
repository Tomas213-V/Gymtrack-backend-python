import math
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Set, Tuple
from fastapi import APIRouter, HTTPException, status, Depends, Query, Path, Body
from supabase import Client

from app.core.dependencies import (
    get_current_user,
    _switch_profile_context_if_available,
)
from app.services.utils import get_data
from app.models.asistencia import (
    AsistenciaCreate,
    AsistenciaCheckout,
    AsistenciaAnularRequest,
)
from app.routers.gimnasios import (
    ensure_active_organizational_assignments,
    verify_can_read_gimnasio,
)
from app.routers.socios import (
    _resolve_socio_profile_context,
    _fetch_socio_gimnasios_for_gym,
    _fetch_socio_gimnasios_for_socio,
)

router = APIRouter(prefix="/api/asistencias", tags=["Asistencias"])

ASISTENCIA_SELECT = (
    "id_asistencia_socio, id_socio, id_sucursal, hora_ingreso, hora_salida, estado, "
    "anulada_por, anulada_en, motivo_anulacion, "
    "socio(id_socio, nombre, apellido, dni, email, telefono, estado), "
    "sucursal(id_sucursal, descripcion, direccion, telefono, estado, id_gimnasio, "
    "gimnasio(id_gimnasio, nombre, estado))"
)

ESTADOS_ASISTENCIA_VALIDOS = {"presente", "finalizada", "anulada"}


def _map_asistencia_rpc_error(exc: Exception) -> int:
    msg = str(exc).lower()
    if any(
        k in msg
        for k in (
            "límite máximo",
            "limite maximo",
            "ya existe un ingreso en curso",
            "asistencia abierta",
            "ya fue anulada",
            "transición inválida",
            "transicion invalida",
        )
    ):
        return status.HTTP_409_CONFLICT
    if any(k in msg for k in ("no tienes autorización", "sin permisos", "no autorizado", "no tienes permisos")):
        return status.HTTP_403_FORBIDDEN
    if "no existe" in msg or "no encontrado" in msg or "no se encontró" in msg:
        return status.HTTP_404_NOT_FOUND
    return status.HTTP_400_BAD_REQUEST


def _fetch_sucursal_or_404(db: Client, id_sucursal: int) -> Dict[str, Any]:
    res = (
        db.from_("sucursal")
        .select("id_sucursal, descripcion, direccion, telefono, estado, id_gimnasio")
        .eq("id_sucursal", id_sucursal)
        .maybe_single()
        .execute()
    )
    suc = get_data(res)
    if not isinstance(suc, dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sucursal no encontrada.",
        )
    return suc


def _fetch_asistencia_or_404(db: Client, id_asistencia_socio: int) -> Dict[str, Any]:
    res = (
        db.from_("asistencia_socio")
        .select(ASISTENCIA_SELECT)
        .eq("id_asistencia_socio", id_asistencia_socio)
        .maybe_single()
        .execute()
    )
    asist = get_data(res)
    if not isinstance(asist, dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Asistencia no encontrada.",
        )
    return asist


def _extract_gym_id_from_asistencia(asist: Dict[str, Any]) -> Optional[int]:
    suc = asist.get("sucursal")
    if isinstance(suc, dict) and suc.get("id_gimnasio") is not None:
        return int(suc["id_gimnasio"])
    return None


def _get_staff_authorized_sucursal_ids_for_gym(
    ctx: Dict[str, Any],
    gym_id: int,
    allowed_emp_roles: Optional[Set[str]] = None,
) -> Optional[Set[int]]:
    """
    Devuelve:
      - `None` si el actor tiene alcance organizacional completo sobre `gym_id` (dueño o administrador org activo).
      - Conjunto de `id_sucursal` autorizadas si es empleado operativo con rol permitido en ese gimnasio.
      - Conjunto vacío si no tiene permisos sobre `gym_id`.
    """
    rol = ctx.get("rol")
    gym_ids = {
        int(g["id_gimnasio"])
        for g in (ctx.get("gimnasios") or [])
        if isinstance(g, dict) and g.get("id_gimnasio") is not None
    }
    if gym_id not in gym_ids:
        return set()
    if rol in ("dueño", "administrador"):
        return None
    if rol == "empleado":
        allowed = {r.lower() for r in (allowed_emp_roles or {"administrador", "recepcionista", "entrenador"})}
        suc_ids: Set[int] = set()
        for s in (ctx.get("sucursales") or []):
            if not isinstance(s, dict):
                continue
            if int(s.get("id_gimnasio") or 0) != gym_id:
                continue
            rol_suc = str(s.get("rol_en_sucursal") or s.get("rol") or "").strip().lower()
            if not rol_suc or rol_suc in allowed:
                if s.get("id_sucursal") is not None:
                    suc_ids.add(int(s["id_sucursal"]))
        return suc_ids
    return set()


def _verify_staff_can_read_asistencias(
    current_user: Dict[str, Any],
    id_gimnasio: Optional[int] = None,
    id_sucursal: Optional[int] = None,
) -> Tuple[Dict[str, Any], int, Optional[Set[int]]]:
    """
    Valida permisos de lectura de asistencias según `asistencia_select_authorized`:
      - Dueño o administrador organizacional activo: todas las sucursales de su gimnasio.
      - Empleado activo (`administrador`, `recepcionista`, `entrenador`): únicamente las sucursales asignadas.
    """
    ensure_active_organizational_assignments(current_user)
    db: Client = current_user["supabase"]

    suc_gym_id: Optional[int] = None
    if id_sucursal is not None:
        suc = _fetch_sucursal_or_404(db, id_sucursal)
        suc_gym_id = int(suc["id_gimnasio"])
        if id_gimnasio is not None and int(id_gimnasio) != suc_gym_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="La sucursal indicada no pertenece al gimnasio especificado.",
            )

    req_gym = current_user.get("requested_gym_id")
    target_gid = suc_gym_id or id_gimnasio or current_user.get("id_gimnasio")
    if target_gid is not None and req_gym is not None and int(target_gid) != int(req_gym):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No se permite cruzar gimnasios al consultar asistencias.",
        )

    req_suc = current_user.get("requested_sucursal_id")
    if id_sucursal is not None and req_suc is not None and int(id_sucursal) != int(req_suc):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="La sucursal solicitada no coincide con la sucursal del contexto activo.",
        )

    def _try(ctx: Dict[str, Any]) -> Optional[Tuple[Dict[str, Any], int, Optional[Set[int]]]]:
        gid = int(target_gid) if target_gid is not None else (
            int(ctx["id_gimnasio"]) if ctx.get("id_gimnasio") is not None else None
        )
        if gid is None:
            return None
        scope = _get_staff_authorized_sucursal_ids_for_gym(
            ctx, gid, {"administrador", "recepcionista", "entrenador"}
        )
        if scope is None:
            return ctx, gid, ({int(id_sucursal)} if id_sucursal is not None else None)
        if not scope:
            return None
        if id_sucursal is not None:
            if int(id_sucursal) not in scope:
                return None
            return ctx, gid, {int(id_sucursal)}
        return ctx, gid, scope

    matched = _try(current_user)
    if matched is not None:
        return matched

    if current_user.get("requested_role") is None:
        for candidate in ("dueño", "administrador", "empleado"):
            if candidate in (current_user.get("perfiles_disponibles") or []):
                switched = _switch_profile_context_if_available(current_user, candidate)
                matched = _try(switched)
                if matched is not None:
                    return matched

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="No tienes permisos para consultar asistencias en este gimnasio o sucursal.",
    )


def _verify_staff_can_manage_sucursal_asistencia(
    current_user: Dict[str, Any],
    id_sucursal: int,
    gym_id: int,
) -> Dict[str, Any]:
    """
    Valida permisos de escritura/control de asistencias sobre una sucursal concreta en correspondencia
    con `rpc_registrar_ingreso_asistencia`, `rpc_registrar_salida_asistencia` y `rpc_anular_asistencia`:
      - Dueño del gimnasio.
      - Administrador organizacional activo del gimnasio.
      - Empleado activo asignado a ESA sucursal con rol `'administrador'` o `'recepcionista'`.
    Rechaza estrictamente a `entrenador`, empleados de otra sucursal y terceros con `403 Forbidden`.
    """
    ensure_active_organizational_assignments(current_user)

    req_gym = current_user.get("requested_gym_id")
    if req_gym is not None and int(req_gym) != gym_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="La sucursal no pertenece al gimnasio del contexto activo.",
        )

    req_suc = current_user.get("requested_sucursal_id")
    if req_suc is not None and int(req_suc) != id_sucursal:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="La sucursal no coincide con la sucursal del contexto activo.",
        )

    def _matches(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        scope = _get_staff_authorized_sucursal_ids_for_gym(
            ctx, gym_id, {"administrador", "recepcionista"}
        )
        if scope is None:
            return ctx
        if id_sucursal in scope:
            return ctx
        return None

    matched = _matches(current_user)
    if matched is not None:
        return matched

    if current_user.get("requested_role") is None:
        for candidate in ("dueño", "administrador", "empleado"):
            if candidate in (current_user.get("perfiles_disponibles") or []):
                switched = _switch_profile_context_if_available(current_user, candidate)
                matched = _matches(switched)
                if matched is not None:
                    return matched

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="No tienes autorización para gestionar asistencias en esta sucursal (requiere dueño, administrador organizacional o administrador/recepcionista asignado a la sucursal).",
    )


def _filter_asistencias_by_scope(
    rows: List[Dict[str, Any]],
    *,
    gym_id: Optional[int] = None,
    allowed_sucursal_ids: Optional[Set[int]] = None,
    id_socio: Optional[int] = None,
    estado: Optional[str] = None,
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    search: Optional[str] = None,
) -> List[Dict[str, Any]]:
    estado_norm = estado.strip().lower() if estado and estado.strip().lower() != "todos" else None
    if estado_norm and estado_norm not in ESTADOS_ASISTENCIA_VALIDOS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Estado de asistencia inválido. Permitidos: presente, finalizada, anulada.",
        )

    search_norm = search.strip().lower() if search and search.strip() else None
    f_desde = (desde if "T" in desde else f"{desde}T00:00:00") if desde else None
    f_hasta = (hasta if "T" in hasta else f"{hasta}T23:59:59.999999") if hasta else None

    out: List[Dict[str, Any]] = []
    for a in rows:
        if not isinstance(a, dict):
            continue
        if gym_id is not None and _extract_gym_id_from_asistencia(a) != gym_id:
            continue
        if allowed_sucursal_ids is not None and int(a.get("id_sucursal") or 0) not in allowed_sucursal_ids:
            continue
        if id_socio is not None and int(a.get("id_socio") or 0) != id_socio:
            continue
        if estado_norm and str(a.get("estado") or "").strip().lower() != estado_norm:
            continue
        h_ing = str(a.get("hora_ingreso") or "")
        if f_desde and h_ing and h_ing < f_desde:
            continue
        if f_hasta and h_ing and h_ing > f_hasta:
            continue
        if search_norm:
            soc = a.get("socio") if isinstance(a.get("socio"), dict) else {}
            haystack = " ".join(
                str(soc.get(k) or "").lower() for k in ("nombre", "apellido", "dni", "email")
            )
            if search_norm not in haystack:
                continue
        out.append(a)
    return out


@router.get("/estadisticas", status_code=status.HTTP_200_OK)
async def get_estadisticas(
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    id_sucursal: Optional[int] = Query(default=None, gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Obtiene estadísticas comparativas de asistencias válidas (`estado != 'anulada'`) en
    `public.asistencia_socio` filtradas por gimnasio y/o sucursal autorizada.
    """
    staff_ctx, gym_id, allowed_suc_ids = _verify_staff_can_read_asistencias(
        current_user, id_gimnasio=id_gimnasio, id_sucursal=id_sucursal
    )
    db: Client = staff_ctx["supabase"]

    ahora = datetime.now(timezone.utc)
    year_actual = ahora.year
    mes_actual = ahora.month

    inicio_mes_actual = datetime(year_actual, mes_actual, 1, tzinfo=timezone.utc).isoformat()
    fin_mes_actual = (
        datetime(year_actual + 1, 1, 1, tzinfo=timezone.utc).isoformat()
        if mes_actual == 12
        else datetime(year_actual, mes_actual + 1, 1, tzinfo=timezone.utc).isoformat()
    )
    inicio_mes_anterior = (
        datetime(year_actual - 1, 12, 1, tzinfo=timezone.utc).isoformat()
        if mes_actual == 1
        else datetime(year_actual, mes_actual - 1, 1, tzinfo=timezone.utc).isoformat()
    )

    res = (
        db.from_("asistencia_socio")
        .select(ASISTENCIA_SELECT)
        .order("hora_ingreso", desc=True)
        .execute()
    )
    rows = get_data(res)
    scoped = _filter_asistencias_by_scope(
        rows if isinstance(rows, list) else [],
        gym_id=gym_id,
        allowed_sucursal_ids=allowed_suc_ids,
    )

    validas = [a for a in scoped if str(a.get("estado") or "").lower() != "anulada"]
    total_mes_actual = sum(
        1 for a in validas
        if inicio_mes_actual <= str(a.get("hora_ingreso") or "") < fin_mes_actual
    )
    total_mes_anterior = sum(
        1 for a in validas
        if inicio_mes_anterior <= str(a.get("hora_ingreso") or "") < inicio_mes_actual
    )

    porcentaje_cambio = 0
    tendencia = "neutral"
    if total_mes_anterior > 0:
        porcentaje_cambio = round(((total_mes_actual - total_mes_anterior) / total_mes_anterior) * 100)
        tendencia = "up" if porcentaje_cambio >= 0 else "down"
    elif total_mes_actual > 0:
        porcentaje_cambio = 100
        tendencia = "up"

    meses_nombres = [
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ]

    return {
        "id_gimnasio": gym_id,
        "id_sucursal": id_sucursal,
        "totalMesActual": total_mes_actual,
        "totalMesAnterior": total_mes_anterior,
        "porcentajeCambio": abs(porcentaje_cambio),
        "tendencia": tendencia,
        "nombreMes": meses_nombres[mes_actual - 1],
    }


@router.get("/mis-asistencias", status_code=status.HTTP_200_OK)
async def get_mis_asistencias(
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    id_sucursal: Optional[int] = Query(default=None, gt=0),
    estado: Optional[str] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Devuelve el historial completo de asistencias (`public.asistencia_socio`) del socio autenticado."""
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]

    if id_gimnasio is not None:
        asocs = _fetch_socio_gimnasios_for_socio(db, own_socio_id)
        if id_gimnasio not in {int(a["id_gimnasio"]) for a in asocs if a.get("id_gimnasio") is not None}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No posees asociación con el gimnasio solicitado.",
            )

    res = (
        db.from_("asistencia_socio")
        .select(ASISTENCIA_SELECT)
        .eq("id_socio", own_socio_id)
        .order("hora_ingreso", desc=True)
        .execute()
    )
    rows = get_data(res)
    filtradas = _filter_asistencias_by_scope(
        rows if isinstance(rows, list) else [],
        gym_id=id_gimnasio,
        allowed_sucursal_ids={id_sucursal} if id_sucursal is not None else None,
        id_socio=own_socio_id,
        estado=estado,
    )
    return {
        "id_socio": own_socio_id,
        "total": len(filtradas),
        "asistencias": filtradas,
    }


@router.get("/socio/{id_socio}", status_code=status.HTTP_200_OK)
async def get_asistencias_por_socio(
    id_socio: int = Path(..., gt=0),
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    id_sucursal: Optional[int] = Query(default=None, gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Obtiene el historial de asistencias de un socio respetando el aislamiento multi-gimnasio y sucursal."""
    if current_user.get("rol") == "socio":
        own_id = current_user.get("id_socio")
        if not own_id or int(own_id) != id_socio:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes autorización para consultar asistencias de otro socio.",
            )
        return await get_mis_asistencias(
            id_gimnasio=id_gimnasio, id_sucursal=id_sucursal, current_user=current_user
        )

    staff_ctx, gym_id, allowed_suc_ids = _verify_staff_can_read_asistencias(
        current_user, id_gimnasio=id_gimnasio, id_sucursal=id_sucursal
    )
    db: Client = staff_ctx["supabase"]
    asocs_gym = _fetch_socio_gimnasios_for_gym(db, gym_id)
    if id_socio not in {int(a["id_socio"]) for a in asocs_gym if a.get("id_socio") is not None}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El socio solicitado no está asociado a tu gimnasio.",
        )

    res = (
        db.from_("asistencia_socio")
        .select(ASISTENCIA_SELECT)
        .eq("id_socio", id_socio)
        .order("hora_ingreso", desc=True)
        .execute()
    )
    rows = get_data(res)
    filtradas = _filter_asistencias_by_scope(
        rows if isinstance(rows, list) else [],
        gym_id=gym_id,
        allowed_sucursal_ids=allowed_suc_ids,
        id_socio=id_socio,
    )
    return {
        "id_socio": id_socio,
        "id_gimnasio": gym_id,
        "total": len(filtradas),
        "asistencias": filtradas,
    }


@router.get("", status_code=status.HTTP_200_OK)
async def get_asistencias(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=200),
    search: Optional[str] = Query(None),
    desde: Optional[str] = Query(None),
    hasta: Optional[str] = Query(None),
    estado: Optional[str] = Query(None),
    id_socio: Optional[str] = Query(None),
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    id_sucursal: Optional[int] = Query(default=None, gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Listado paginado de `public.asistencia_socio` aislado por gimnasio, sucursal y perfil."""
    socio_filter: Optional[int] = None
    if id_socio and id_socio.strip().lower() != "todos":
        try:
            socio_filter = int(id_socio)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="id_socio inválido.")

    if current_user.get("rol") == "socio":
        own_id = current_user.get("id_socio")
        if socio_filter is not None and (not own_id or socio_filter != int(own_id)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes autorización para consultar asistencias de otro socio.",
            )
        mis = await get_mis_asistencias(
            id_gimnasio=id_gimnasio,
            id_sucursal=id_sucursal,
            estado=estado,
            current_user=current_user,
        )
        filtradas = _filter_asistencias_by_scope(
            mis["asistencias"], desde=desde, hasta=hasta, search=search
        )
        total = len(filtradas)
        offset = (page - 1) * limit
        return {
            "total": total,
            "page": page,
            "totalPages": math.ceil(total / limit) if limit else 1,
            "limit": limit,
            "asistencias": filtradas[offset : offset + limit],
        }

    staff_ctx, gym_id, allowed_suc_ids = _verify_staff_can_read_asistencias(
        current_user, id_gimnasio=id_gimnasio, id_sucursal=id_sucursal
    )
    db: Client = staff_ctx["supabase"]
    res = (
        db.from_("asistencia_socio")
        .select(ASISTENCIA_SELECT)
        .order("hora_ingreso", desc=True)
        .execute()
    )
    rows = get_data(res)
    filtradas = _filter_asistencias_by_scope(
        rows if isinstance(rows, list) else [],
        gym_id=gym_id,
        allowed_sucursal_ids=allowed_suc_ids,
        id_socio=socio_filter,
        estado=estado,
        desde=desde,
        hasta=hasta,
        search=search,
    )
    filtradas.sort(key=lambda r: str(r.get("hora_ingreso") or ""), reverse=True)
    total = len(filtradas)
    offset = (page - 1) * limit

    return {
        "id_gimnasio": gym_id,
        "total": total,
        "page": page,
        "totalPages": math.ceil(total / limit) if limit else 1,
        "limit": limit,
        "asistencias": filtradas[offset : offset + limit],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def registrar_asistencia(
    req: AsistenciaCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Registra un ingreso en `public.asistencia_socio` delegando todas las reglas de negocio
    (membresía vigente, máximo 1 asistencia abierta por gimnasio, máximo 3/día por gimnasio
    sumando sucursales, concurrencia con advisory lock) en `public.rpc_registrar_ingreso_asistencia`.
    """
    if (
        req.id_gimnasio is not None
        or req.id_membresia_socio is not None
        or req.hora_ingreso is not None
        or req.hora_salida is not None
        or req.estado is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No está permitido imponer id_gimnasio, id_membresia_socio, horarios ni estado desde el cliente; la RPC determina y valida el ingreso.",
        )

    target_sucursal_id = req.id_sucursal or current_user.get("id_sucursal")
    if not target_sucursal_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes indicar el id_sucursal donde se registra el ingreso.",
        )

    db_initial: Client = current_user["supabase"]
    sucursal = _fetch_sucursal_or_404(db_initial, int(target_sucursal_id))
    sucursal_gym_id = int(sucursal["id_gimnasio"])
    if sucursal.get("estado") is not True:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La sucursal seleccionada se encuentra inactiva.",
        )

    target_socio_id: Optional[int] = req.id_socio

    if current_user.get("rol") == "socio":
        socio_ctx = _resolve_socio_profile_context(current_user)
        own_socio_id = int(socio_ctx["id_socio"])
        if req.id_socio is not None and int(req.id_socio) != own_socio_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Un socio no puede registrar asistencias de otro socio.",
            )
        if req.dni is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Un socio solo puede registrar su propio ingreso.",
            )
        req_gym = current_user.get("requested_gym_id")
        if req_gym is not None and int(req_gym) != sucursal_gym_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="La sucursal indicada no pertenece al gimnasio del contexto activo.",
            )
        target_socio_id = own_socio_id
        actor_db = socio_ctx["supabase"]
    else:
        staff_ctx = _verify_staff_can_manage_sucursal_asistencia(
            current_user, id_sucursal=int(target_sucursal_id), gym_id=sucursal_gym_id
        )
        actor_db = staff_ctx["supabase"]

        if target_socio_id is None and req.dni and req.dni.strip():
            clean_dni = req.dni.strip()
            asocs_gym = _fetch_socio_gimnasios_for_gym(actor_db, sucursal_gym_id)
            matched_sg = next(
                (
                    a
                    for a in asocs_gym
                    if isinstance(a.get("socio"), dict)
                    and str(a["socio"].get("dni") or "").strip() == clean_dni
                ),
                None,
            )
            if not matched_sg:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No se encontró un socio asociado a este gimnasio con el DNI ingresado.",
                )
            target_socio_id = int(matched_sg["id_socio"])

        if target_socio_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Debes proporcionar id_socio o dni para registrar la asistencia.",
            )

    try:
        res_rpc = actor_db.rpc(
            "rpc_registrar_ingreso_asistencia",
            {
                "p_id_sucursal": int(target_sucursal_id),
                "p_id_socio": int(target_socio_id),
            },
        ).execute()
        nueva = get_data(res_rpc)
    except Exception as exc:
        raise HTTPException(
            status_code=_map_asistencia_rpc_error(exc),
            detail=f"No fue posible registrar el ingreso: {exc}",
        ) from exc

    return {
        "mensaje": "Ingreso registrado exitosamente.",
        "asistencia": nueva,
    }


@router.post("/salida", status_code=status.HTTP_200_OK)
async def registrar_salida_abierta(
    req: Optional[AsistenciaCheckout] = Body(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Cierra la asistencia abierta del socio autenticado o indicado por staff mediante `rpc_registrar_salida_asistencia`."""
    req_data = req or AsistenciaCheckout()
    if req_data.hora_salida is not None or req_data.estado is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No está permitido imponer hora_salida ni estado manualmente; la RPC los determina.",
        )

    target_socio_id = req_data.id_socio
    if current_user.get("rol") == "socio":
        socio_ctx = _resolve_socio_profile_context(current_user)
        own_id = int(socio_ctx["id_socio"])
        if target_socio_id is not None and int(target_socio_id) != own_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No puedes cerrar la asistencia de otro socio.",
            )
        target_socio_id = own_id
        actor_db = socio_ctx["supabase"]
    else:
        ensure_active_organizational_assignments(current_user)
        if target_socio_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Debes indicar id_socio o utilizar /api/asistencias/{id}/checkout.",
            )

        # Validar rol y sucursal autorizada antes de invocar la RPC (bloquea entrenadores y otras sucursales)
        db_initial: Client = current_user["supabase"]
        res_open = (
            db_initial.from_("asistencia_socio")
            .select(ASISTENCIA_SELECT)
            .eq("id_socio", int(target_socio_id))
            .eq("estado", "presente")
            .order("hora_ingreso", desc=True)
            .execute()
        )
        open_rows = [
            a for a in (get_data(res_open) or [])
            if isinstance(a, dict)
            and int(a.get("id_socio") or 0) == int(target_socio_id)
            and str(a.get("estado") or "").lower() == "presente"
            and a.get("hora_salida") is None
        ]
        if not open_rows:
            # Validar igualmente que el actor tenga al menos rol de gestión de asistencias
            if current_user.get("rol") == "empleado":
                all_roles = {r.lower() for r in (current_user.get("roles_empleado") or [])}
                if not all_roles.intersection({"administrador", "recepcionista"}):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="No tienes autorización para registrar salidas de asistencias (requiere dueño, administrador o recepcionista).",
                    )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No se encontró una asistencia abierta para el socio indicado.",
            )

        # Buscar una asistencia abierta en una sucursal autorizada para el staff
        authorized_ctx: Optional[Dict[str, Any]] = None
        authorized_asist_id: Optional[int] = None
        for cand in open_rows:
            cand_gym_id = _extract_gym_id_from_asistencia(cand)
            cand_suc_id = cand.get("id_sucursal")
            if cand_gym_id is None or cand_suc_id is None:
                continue
            try:
                authorized_ctx = _verify_staff_can_manage_sucursal_asistencia(
                    current_user, id_sucursal=int(cand_suc_id), gym_id=int(cand_gym_id)
                )
                authorized_asist_id = int(cand["id_asistencia_socio"])
                break
            except HTTPException:
                continue

        if authorized_ctx is None or authorized_asist_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes autorización para registrar la salida en la sucursal de la asistencia abierta.",
            )
        actor_db = authorized_ctx["supabase"]

    try:
        res_rpc = actor_db.rpc(
            "rpc_registrar_salida_asistencia",
            {
                "p_id_asistencia_socio": None if current_user.get("rol") == "socio" else authorized_asist_id,
                "p_id_socio": int(target_socio_id) if current_user.get("rol") == "socio" else None,
            },
        ).execute()
        cerrada = get_data(res_rpc)
    except Exception as exc:
        raise HTTPException(
            status_code=_map_asistencia_rpc_error(exc),
            detail=f"No fue posible registrar la salida: {exc}",
        ) from exc

    return {
        "mensaje": "Salida registrada correctamente.",
        "asistencia": cerrada,
    }


@router.patch("/{id_asistencia_socio}/checkout", status_code=status.HTTP_200_OK)
@router.patch("/{id_asistencia_socio}/salida", status_code=status.HTTP_200_OK)
async def checkout_asistencia(
    id_asistencia_socio: int = Path(..., gt=0),
    req: Optional[AsistenciaCheckout] = Body(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Registra la salida de una asistencia específica mediante `public.rpc_registrar_salida_asistencia`."""
    req_data = req or AsistenciaCheckout()
    if req_data.hora_salida is not None or req_data.estado is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No está permitido imponer hora_salida ni estado manualmente; la RPC los determina.",
        )

    db_initial: Client = current_user["supabase"]
    asistencia = _fetch_asistencia_or_404(db_initial, id_asistencia_socio)

    if current_user.get("rol") == "socio":
        socio_ctx = _resolve_socio_profile_context(current_user)
        if int(asistencia["id_socio"]) != int(socio_ctx["id_socio"]):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes autorización para cerrar una asistencia ajena.",
            )
        actor_db = socio_ctx["supabase"]
    else:
        gym_id = _extract_gym_id_from_asistencia(asistencia)
        if gym_id is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sucursal de asistencia no encontrada.")
        staff_ctx = _verify_staff_can_manage_sucursal_asistencia(
            current_user,
            id_sucursal=int(asistencia["id_sucursal"]),
            gym_id=gym_id,
        )
        actor_db = staff_ctx["supabase"]

    try:
        res_rpc = actor_db.rpc(
            "rpc_registrar_salida_asistencia",
            {
                "p_id_asistencia_socio": id_asistencia_socio,
                "p_id_socio": None,
            },
        ).execute()
        cerrada = get_data(res_rpc)
    except Exception as exc:
        raise HTTPException(
            status_code=_map_asistencia_rpc_error(exc),
            detail=f"No fue posible registrar la salida: {exc}",
        ) from exc

    return {
        "mensaje": "Salida registrada correctamente.",
        "asistencia": cerrada,
    }


@router.patch("/{id_asistencia_socio}/anular", status_code=status.HTTP_200_OK)
@router.delete("/{id_asistencia_socio}", status_code=status.HTTP_200_OK)
async def anular_asistencia(
    id_asistencia_socio: int = Path(..., gt=0),
    req: Optional[AsistenciaAnularRequest] = Body(default=None),
    motivo: Optional[str] = Query(default=None, max_length=500),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Anula lógicamente una asistencia (`estado = 'anulada'`) mediante `public.rpc_anular_asistencia`.
    Conserva el historial completo y trazabilidad (`anulada_por`, `anulada_en`, `motivo_anulacion`).
    Prohibido el borrado físico y bloqueado para socios y entrenadores (`403 Forbidden`).
    """
    if current_user.get("rol") == "socio":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Los socios no tienen autorización para anular asistencias.",
        )

    db_initial: Client = current_user["supabase"]
    asistencia = _fetch_asistencia_or_404(db_initial, id_asistencia_socio)
    gym_id = _extract_gym_id_from_asistencia(asistencia)
    if gym_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sucursal de asistencia no encontrada.")

    staff_ctx = _verify_staff_can_manage_sucursal_asistencia(
        current_user,
        id_sucursal=int(asistencia["id_sucursal"]),
        gym_id=gym_id,
    )
    actor_db: Client = staff_ctx["supabase"]
    motivo_final = (req.motivo if req and req.motivo else motivo)
    motivo_clean = motivo_final.strip() if motivo_final and motivo_final.strip() else None

    try:
        res_rpc = actor_db.rpc(
            "rpc_anular_asistencia",
            {
                "p_id_asistencia_socio": id_asistencia_socio,
                "p_motivo": motivo_clean,
            },
        ).execute()
        anulada = get_data(res_rpc)
    except Exception as exc:
        raise HTTPException(
            status_code=_map_asistencia_rpc_error(exc),
            detail=f"No fue posible anular la asistencia: {exc}",
        ) from exc

    return {
        "mensaje": "Asistencia anulada lógicamente conservando el historial.",
        "anulada": True,
        "asistencia": anulada,
    }
