import math
import re
from datetime import date
from typing import Dict, Any, Optional, List, Set
from fastapi import APIRouter, HTTPException, status, Depends, Query, Path
from supabase import Client

from app.core.dependencies import (
    get_current_user,
    _switch_profile_context_if_available,
)
from app.services.utils import get_data, raise_http_from_postgrest_error
from app.models.socio import (
    SocioCreate,
    AsociarSocioGimnasioRequest,
    SocioUpdate,
    SocioChangeEstado,
    SocioUpdatePerfil,
)
from app.routers.gimnasios import ensure_active_organizational_assignments

router = APIRouter(prefix="/api/socios", tags=["Socios y Asociaciones Multi-Gimnasio"])

SOCIO_COLUMNS = "id_socio, auth_user_id, dni, telefono, fecha_alta, estado, nombre, apellido, email"
SOCIO_GIMNASIO_SELECT = (
    "id_socio_gimnasio, id_socio, id_gimnasio, fecha_alta, estado, creado_en, "
    "socio(id_socio, auth_user_id, dni, telefono, fecha_alta, estado, nombre, apellido, email), "
    "gimnasio(id_gimnasio, nombre, telefono, email, fecha_registro, estado, id_dueno)"
)


def _clean_socio_dict(socio: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normaliza la representación de `public.socio` excluyendo campos legacy obsoletos."""
    if not isinstance(socio, dict):
        return {}
    return {
        k: v
        for k, v in socio.items()
        if k not in ("contrasena", "id_gimnasio", "socio_gimnasio", "progreso")
    }


def _resolve_socio_profile_context(current_user: Dict[str, Any]) -> Dict[str, Any]:
    """
    Exige que el usuario autenticado posea un perfil activo de Socio (`public.socio`).
    Si el usuario tiene múltiples perfiles y no fijó otro rol vía `X-Active-Role`,
    selecciona el contexto de `socio`.
    """
    socio_ctx = _switch_profile_context_if_available(current_user, "socio")
    if not socio_ctx.get("id_socio") or not socio_ctx.get("socio"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tu cuenta autenticada no posee un perfil de socio activo vinculado.",
        )
    return socio_ctx


def _resolve_staff_context_for_gym(
    current_user: Dict[str, Any],
    id_gimnasio: Optional[int],
    allowed_employee_subroles: Set[str],
    denied_message: str,
) -> tuple[Dict[str, Any], int]:
    """
    Verifica permisos de personal del gimnasio sobre `id_gimnasio` en correspondencia con
    las funciones SQL `fn_staff_puede_ver_socio`, `fn_staff_puede_gestionar_socio`,
    `trg_proteger_campos_socio` y las RPC de `socio_gimnasio`:
      - Dueño titular del gimnasio (`public.dueno`).
      - Administrador organizacional activo del gimnasio (`public.administrador_gimnasio`).
      - Empleado operativo con asignación activa en el gimnasio y subrol permitido (`public.empleado_sucursal`).
    Rechaza cualquier cruce de gimnasio o rol no autorizado con `403 Forbidden`.
    """
    ensure_active_organizational_assignments(current_user)

    req_gym_header = current_user.get("requested_gym_id")
    if id_gimnasio is not None and req_gym_header is not None and int(id_gimnasio) != int(req_gym_header):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No se permite cruzar gimnasios: el id_gimnasio indicado no coincide con el contexto activo.",
        )

    target_gym_id = id_gimnasio if id_gimnasio is not None else current_user.get("id_gimnasio")

    def _matches_staff(ctx: Dict[str, Any], gid: Optional[int]) -> Optional[int]:
        rol = ctx.get("rol")
        gyms = ctx.get("gimnasios") or []
        gym_ids = {
            int(g["id_gimnasio"])
            for g in gyms
            if isinstance(g, dict) and g.get("id_gimnasio") is not None
        }
        resolved_gid = int(gid) if gid is not None else (
            int(ctx["id_gimnasio"]) if ctx.get("id_gimnasio") is not None else None
        )
        if resolved_gid is None:
            return None

        if rol in ("dueño", "administrador"):
            return resolved_gid if resolved_gid in gym_ids else None

        if rol == "empleado":
            roles_por_gym = ctx.get("roles_empleado_por_gimnasio") or {}
            roles_en_gym = {r.lower() for r in (roles_por_gym.get(resolved_gid) or [])}
            if resolved_gid in gym_ids and bool(roles_en_gym & allowed_employee_subroles):
                return resolved_gid
            return None

        return None

    matched_gid = _matches_staff(current_user, target_gym_id)
    if matched_gid is not None:
        return current_user, matched_gid

    if current_user.get("requested_role") is None:
        for candidate in ("dueño", "administrador", "empleado"):
            if candidate in (current_user.get("perfiles_disponibles") or []):
                switched = _switch_profile_context_if_available(current_user, candidate)
                matched_gid = _matches_staff(switched, target_gym_id)
                if matched_gid is not None:
                    return switched, matched_gid

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=denied_message,
    )


def _verify_staff_can_view_socios(
    current_user: Dict[str, Any],
    id_gimnasio: Optional[int] = None,
) -> tuple[Dict[str, Any], int]:
    return _resolve_staff_context_for_gym(
        current_user,
        id_gimnasio=id_gimnasio,
        allowed_employee_subroles={"administrador", "recepcionista", "entrenador"},
        denied_message="No tienes permisos para consultar socios en este gimnasio.",
    )


def _verify_staff_can_manage_socios(
    current_user: Dict[str, Any],
    id_gimnasio: Optional[int] = None,
) -> tuple[Dict[str, Any], int]:
    return _resolve_staff_context_for_gym(
        current_user,
        id_gimnasio=id_gimnasio,
        allowed_employee_subroles={"administrador", "recepcionista"},
        denied_message=(
            "No tienes permisos para registrar, asociar o gestionar socios en este gimnasio "
            "(requiere dueño, administrador organizacional, administrador de sucursal o recepcionista)."
        ),
    )


def _verify_staff_can_change_socio_global_estado(
    current_user: Dict[str, Any],
    id_gimnasio: int,
) -> Dict[str, Any]:
    ctx, _ = _resolve_staff_context_for_gym(
        current_user,
        id_gimnasio=id_gimnasio,
        allowed_employee_subroles={"administrador"},
        denied_message=(
            "Solo el dueño, administrador organizacional o administrador de sucursal "
            "puede cambiar el estado global del socio."
        ),
    )
    return ctx


def _fetch_socio_gimnasios_for_socio(db: Client, id_socio: int) -> List[Dict[str, Any]]:
    res = (
        db.from_("socio_gimnasio")
        .select(SOCIO_GIMNASIO_SELECT)
        .eq("id_socio", id_socio)
        .order("id_socio_gimnasio", desc=False)
        .execute()
    )
    rows = get_data(res)
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and int(r.get("id_socio") or 0) == id_socio]


def _fetch_socio_gimnasios_for_gym(db: Client, id_gimnasio: int) -> List[Dict[str, Any]]:
    res = (
        db.from_("socio_gimnasio")
        .select(SOCIO_GIMNASIO_SELECT)
        .eq("id_gimnasio", id_gimnasio)
        .order("id_socio_gimnasio", desc=False)
        .execute()
    )
    rows = get_data(res)
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and int(r.get("id_gimnasio") or 0) == id_gimnasio]


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
        detail="Filtro de estado inválido. Valores permitidos: true, false, activo, inactivo, todos.",
    )


def _format_socio_con_asociacion(sg_row: Dict[str, Any]) -> Dict[str, Any]:
    socio_clean = _clean_socio_dict(sg_row.get("socio"))
    if not socio_clean and sg_row.get("id_socio") is not None:
        socio_clean = {"id_socio": sg_row.get("id_socio")}
    return {
        **socio_clean,
        "id_socio": socio_clean.get("id_socio") or sg_row.get("id_socio"),
        "id_socio_gimnasio": sg_row.get("id_socio_gimnasio"),
        "id_gimnasio_asociacion": sg_row.get("id_gimnasio"),
        "estado_asociacion": sg_row.get("estado"),
        "fecha_alta_gimnasio": sg_row.get("fecha_alta"),
        "creado_en_asociacion": sg_row.get("creado_en"),
        "gimnasio": sg_row.get("gimnasio"),
    }


# ==============================================================================
# A. PERFIL PERSONAL DEL SOCIO Y ASOCIACIONES MULTI-GIMNASIO
# ==============================================================================

@router.get("/perfil", status_code=status.HTTP_200_OK)
@router.get("/me", status_code=status.HTTP_200_OK)
async def get_perfil_socio(
    id: Optional[int] = Query(default=None),
    id_socio: Optional[int] = Query(default=None),
    id_gimnasio: Optional[int] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Consulta el perfil personal de un socio y sus asociaciones `public.socio_gimnasio`.
    - Un socio solo puede consultar su propio perfil; intentar leer otro `id_socio` devuelve 403.
    - Personal autorizado de un gimnasio puede consultar el perfil de un socio asociado a su gimnasio.
    """
    requested_socio_id = id_socio or id
    rol = current_user.get("rol")

    # 1. Caso consulta de perfil propio por Socio autenticado
    if rol == "socio":
        own_socio_id = current_user.get("id_socio")
        if not own_socio_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Tu cuenta no posee un perfil de socio activo vinculado.",
            )
        if requested_socio_id is not None and int(requested_socio_id) != int(own_socio_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes autorización para consultar el perfil de otro socio.",
            )

        db: Client = current_user["supabase"]
        res_s = (
            db.from_("socio")
            .select(SOCIO_COLUMNS)
            .eq("id_socio", int(own_socio_id))
            .maybe_single()
            .execute()
        )
        socio_db = get_data(res_s) or current_user.get("socio")
        if not isinstance(socio_db, dict):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Socio no encontrado.")

        asociaciones = _fetch_socio_gimnasios_for_socio(db, int(own_socio_id))
        if not asociaciones and isinstance(current_user.get("socio_gimnasios"), list):
            asociaciones = current_user["socio_gimnasios"]

        if id_gimnasio is not None:
            assoc_target = next(
                (a for a in asociaciones if int(a.get("id_gimnasio") or 0) == int(id_gimnasio)),
                None,
            )
            if not assoc_target or assoc_target.get("estado") is False:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No posees una asociación activa con el gimnasio solicitado.",
                )

        asociaciones_activas = [a for a in asociaciones if a.get("estado") is not False]
        gimnasios_activos = [
            {
                **(a.get("gimnasio") if isinstance(a.get("gimnasio"), dict) else {"id_gimnasio": a.get("id_gimnasio")}),
                "id_socio_gimnasio": a.get("id_socio_gimnasio"),
                "estado_asociacion": a.get("estado"),
                "fecha_alta_gimnasio": a.get("fecha_alta"),
            }
            for a in asociaciones_activas
        ]

        return {
            "tipo": "socio",
            "socio": _clean_socio_dict(socio_db),
            "asociaciones": asociaciones,
            "gimnasios": gimnasios_activos,
            "gimnasio_activo": current_user.get("gimnasio"),
            "total_gimnasios_activos": len(gimnasios_activos),
        }

    # 2. Caso usuario sin perfil de socio que NO especifica id_socio para consulta de staff
    if requested_socio_id is None:
        socio_ctx = _resolve_socio_profile_context(current_user)
        return await get_perfil_socio(
            id=None,
            id_socio=None,
            id_gimnasio=id_gimnasio,
            current_user=socio_ctx,
        )

    # 3. Caso personal autorizado del gimnasio consultando el perfil de un socio de su gimnasio
    staff_ctx, gym_id = _verify_staff_can_view_socios(current_user, id_gimnasio=id_gimnasio)
    db = staff_ctx["supabase"]
    asociaciones_gym = _fetch_socio_gimnasios_for_gym(db, gym_id)
    assoc = next(
        (a for a in asociaciones_gym if int(a.get("id_socio") or 0) == int(requested_socio_id)),
        None,
    )
    if not assoc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El socio consultado no está asociado a tu gimnasio autorizado.",
        )

    socio_data = _clean_socio_dict(assoc.get("socio"))
    if not socio_data:
        res_s = (
            db.from_("socio")
            .select(SOCIO_COLUMNS)
            .eq("id_socio", int(requested_socio_id))
            .maybe_single()
            .execute()
        )
        socio_data = _clean_socio_dict(get_data(res_s))

    return {
        "tipo": "socio",
        "socio": socio_data,
        "socio_gimnasio": {
            "id_socio_gimnasio": assoc.get("id_socio_gimnasio"),
            "id_socio": assoc.get("id_socio"),
            "id_gimnasio": assoc.get("id_gimnasio"),
            "fecha_alta": assoc.get("fecha_alta"),
            "estado": assoc.get("estado"),
            "creado_en": assoc.get("creado_en"),
        },
        "gimnasio": assoc.get("gimnasio"),
    }


@router.get("/mis-gimnasios", status_code=status.HTTP_200_OK)
@router.get("/asociaciones", status_code=status.HTTP_200_OK)
async def listar_mis_asociaciones_gimnasios(
    solo_activas: bool = Query(default=False),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Lista las asociaciones `public.socio_gimnasio` del socio autenticado,
    permitiendo consultar múltiples gimnasios y el estado de cada asociación.
    """
    socio_ctx = _resolve_socio_profile_context(current_user)
    id_socio = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]

    asociaciones = _fetch_socio_gimnasios_for_socio(db, id_socio)
    if not asociaciones and isinstance(socio_ctx.get("socio_gimnasios"), list):
        asociaciones = socio_ctx["socio_gimnasios"]

    if solo_activas:
        asociaciones = [a for a in asociaciones if a.get("estado") is not False]

    activas = [a for a in asociaciones if a.get("estado") is not False]
    return {
        "id_socio": id_socio,
        "total": len(asociaciones),
        "total_activas": len(activas),
        "asociaciones": asociaciones,
        "gimnasios_activos": [
            {
                **(a.get("gimnasio") if isinstance(a.get("gimnasio"), dict) else {"id_gimnasio": a.get("id_gimnasio")}),
                "id_socio_gimnasio": a.get("id_socio_gimnasio"),
                "estado_asociacion": a.get("estado"),
                "fecha_alta_gimnasio": a.get("fecha_alta"),
            }
            for a in activas
        ],
    }


@router.get("/mis-gimnasios/{id_gimnasio}", status_code=status.HTTP_200_OK)
async def obtener_mi_asociacion_gimnasio(
    id_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Valida y devuelve la asociación activa (`socio_gimnasio.estado = true`) del socio autenticado
    con un gimnasio específico. Rechaza con 403 si la asociación no existe o está inactiva.
    """
    socio_ctx = _resolve_socio_profile_context(current_user)
    id_socio = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]

    asociaciones = _fetch_socio_gimnasios_for_socio(db, id_socio)
    assoc = next(
        (a for a in asociaciones if int(a.get("id_gimnasio") or 0) == id_gimnasio),
        None,
    )
    if not assoc or assoc.get("estado") is False:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No puedes operar en este gimnasio porque tu asociación socio_gimnasio no existe o está inactiva.",
        )

    return {
        "socio_gimnasio": assoc,
        "gimnasio": assoc.get("gimnasio"),
    }


@router.put("/perfil", status_code=status.HTTP_200_OK)
@router.put("/me", status_code=status.HTTP_200_OK)
async def update_perfil_socio(
    req: SocioUpdatePerfil,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Permite al socio autenticado actualizar únicamente sus propios datos personales permitidos
    (`nombre`, `apellido`, `telefono`, `email`, `dni`) en `public.socio`.
    Bloquea intentos de modificar `auth_user_id`, `id_socio`, `fecha_alta`, `estado`, asociaciones o contraseñas.
    """
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])

    if req.contrasena is not None or req.password is not None or req.nueva_contrasena is not None or req.contrasena_actual is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Las contraseñas se administran exclusivamente en Supabase Auth (/api/auth/me), no en la tabla socio.",
        )

    if req.id_socio is not None and str(req.id_socio).strip() != str(own_socio_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes autorización para modificar el perfil o el identificador de otro socio.",
        )

    if (
        req.auth_user_id is not None
        or req.fecha_alta is not None
        or req.estado is not None
        or req.id_gimnasio is not None
        or req.id_socio_gimnasio is not None
        or req.rol is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No está permitido modificar campos protegidos (auth_user_id, fecha_alta, estado, rol o asociaciones) desde la actualización del perfil.",
        )

    update_payload: Dict[str, Any] = {}
    if req.nombre is not None:
        clean_nombre = req.nombre.strip()
        if not clean_nombre:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre no puede estar vacío.")
        update_payload["nombre"] = clean_nombre

    if req.apellido is not None:
        clean_apellido = req.apellido.strip()
        if not clean_apellido:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El apellido no puede estar vacío.")
        update_payload["apellido"] = clean_apellido

    if req.telefono is not None:
        update_payload["telefono"] = req.telefono.strip() if req.telefono.strip() else None

    if req.email is not None:
        update_payload["email"] = str(req.email).strip().lower()

    if req.dni is not None:
        update_payload["dni"] = str(req.dni).strip()

    if not update_payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se enviaron datos personales válidos para actualizar.",
        )

    db: Client = socio_ctx["supabase"]
    res_upd = (
        db.from_("socio")
        .update(update_payload)
        .eq("id_socio", own_socio_id)
        .execute()
    )
    rows = get_data(res_upd) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No se encontró el perfil del socio para actualizar.",
        )

    return {
        "mensaje": "Perfil de socio actualizado exitosamente.",
        "socio": _clean_socio_dict(rows[0]),
    }


# ==============================================================================
# B. GESTIÓN DE SOCIOS Y ASOCIACIONES POR GIMNASIO (RPCs OBLIGATORIAS + RLS)
# ==============================================================================

@router.get("/verificar-dni", status_code=status.HTTP_200_OK)
async def verificar_dni_socio(
    dni: str = Query(..., min_length=1),
    id_gimnasio: Optional[int] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Verifica si un DNI ya cuenta con una asociación `public.socio_gimnasio` en el gimnasio autorizado.
    Bajo el modelo multi-gimnasio definitivo, un socio puede pertenecer simultáneamente a otros gimnasios.
    """
    staff_ctx, gym_id = _verify_staff_can_manage_socios(current_user, id_gimnasio=id_gimnasio)
    clean_dni = str(dni).strip()
    dni_sin_sep = re.sub(r"[\.\s-]", "", clean_dni)
    dnis_buscados = {clean_dni, dni_sin_sep}

    db: Client = staff_ctx["supabase"]
    asociaciones = _fetch_socio_gimnasios_for_gym(db, gym_id)
    match_sg = None
    for sg in asociaciones:
        s_obj = sg.get("socio") if isinstance(sg.get("socio"), dict) else {}
        s_dni = str(s_obj.get("dni") or "").strip()
        s_dni_clean = re.sub(r"[\.\s-]", "", s_dni)
        if s_dni in dnis_buscados or (s_dni_clean and s_dni_clean in dnis_buscados):
            match_sg = sg
            break

    if match_sg:
        return {
            "id_gimnasio": gym_id,
            "existeEnEsteGimnasio": True,
            "asociacionActiva": match_sg.get("estado") is not False,
            "id_socio_gimnasio": match_sg.get("id_socio_gimnasio"),
            "socio": _clean_socio_dict(match_sg.get("socio")),
            "mensaje": (
                "El socio ya posee una asociación activa en este gimnasio."
                if match_sg.get("estado") is not False
                else "El socio posee una asociación inactiva en este gimnasio y puede reactivarse."
            ),
        }

    return {
        "id_gimnasio": gym_id,
        "existeEnEsteGimnasio": False,
        "asociacionActiva": False,
        "id_socio_gimnasio": None,
        "socio": None,
    }


@router.get("", status_code=status.HTTP_200_OK)
async def get_socios(
    id_gimnasio: Optional[int] = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: str = Query(default="20"),
    estado: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Lista los socios asociados al gimnasio autorizado consultando `public.socio_gimnasio` + `public.socio`
    a través del cliente RLS del usuario autenticado.
    """
    staff_ctx, gym_id = _verify_staff_can_view_socios(current_user, id_gimnasio=id_gimnasio)
    estado_bool = _parse_optional_bool_estado(estado)

    db: Client = staff_ctx["supabase"]
    asociaciones = _fetch_socio_gimnasios_for_gym(db, gym_id)

    filtradas: List[Dict[str, Any]] = []
    term = search.strip().lower() if search and search.strip() else None

    for sg in asociaciones:
        if estado_bool is not None and bool(sg.get("estado")) is not estado_bool:
            continue
        if term:
            s_obj = sg.get("socio") if isinstance(sg.get("socio"), dict) else {}
            haystack = " ".join(
                str(s_obj.get(col) or "").lower()
                for col in ("nombre", "apellido", "dni", "email", "telefono")
            )
            if term not in haystack:
                continue
        filtradas.append(sg)

    filtradas.sort(key=lambda r: int(r.get("id_socio_gimnasio") or 0), reverse=True)
    total = len(filtradas)

    is_all = limit.strip().lower() in ("all", "0")
    parsed_limit = int(limit) if not is_all and limit.strip().isdigit() and int(limit.strip()) > 0 else 20
    parsed_page = 1 if is_all else page

    if is_all:
        page_rows = filtradas
    else:
        offset = (parsed_page - 1) * parsed_limit
        page_rows = filtradas[offset : offset + parsed_limit]

    socios_formateados = [_format_socio_con_asociacion(sg) for sg in page_rows]

    return {
        "id_gimnasio": gym_id,
        "total": total,
        "page": parsed_page,
        "totalPages": 1 if is_all else (math.ceil(total / parsed_limit) if total > 0 else 1),
        "limit": total if is_all else parsed_limit,
        "socios": socios_formateados,
        "asociaciones": page_rows,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_socio(
    req: SocioCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Registra un socio nuevo o reutiliza una identidad existente en `public.socio`
    y crea su asociación en `public.socio_gimnasio` mediante la RPC oficial
    `public.rpc_registrar_socio_gimnasio`.
    - Nunca duplica identidades de socio ni cuentas de Supabase Auth.
    - Devuelve 409 Conflict si ya existe una asociación activa para ese socio en este gimnasio.
    """
    if req.auth_user_id is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No está permitido asignar manualmente auth_user_id al registrar un socio.",
        )
    if req.contrasena is not None or req.password is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se almacenan contraseñas en la tabla socio; el acceso se gestiona vía Supabase Auth.",
        )

    staff_ctx, gym_id = _verify_staff_can_manage_socios(current_user, id_gimnasio=req.id_gimnasio)
    db: Client = staff_ctx["supabase"]

    clean_dni = str(req.dni).strip() if req.dni else None
    clean_email = str(req.email).strip().lower() if req.email else None

    # Prevenir asociaciones activas duplicadas en este gimnasio
    asociaciones_gym = _fetch_socio_gimnasios_for_gym(db, gym_id)
    for sg in asociaciones_gym:
        if sg.get("estado") is False:
            continue
        s_obj = sg.get("socio") if isinstance(sg.get("socio"), dict) else {}
        s_dni = str(s_obj.get("dni") or "").strip() if s_obj.get("dni") else None
        s_email = str(s_obj.get("email") or "").strip().lower() if s_obj.get("email") else None
        if (clean_dni and s_dni == clean_dni) or (clean_email and s_email == clean_email):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="El socio ya posee una asociación activa en este gimnasio.",
            )

    rpc_params = {
        "p_id_gimnasio": gym_id,
        "p_id_membresia_gimnasio": None,
        "p_nombre": req.nombre.strip() if req.nombre and req.nombre.strip() else None,
        "p_apellido": req.apellido.strip() if req.apellido and req.apellido.strip() else None,
        "p_dni": clean_dni,
        "p_email": clean_email,
        "p_telefono": req.telefono.strip() if req.telefono and req.telefono.strip() else None,
        "p_fecha_inicio": date.today().isoformat(),
        "p_estado_membresia": "activa",
        "p_registrar_pago": False,
        "p_metodo_pago": "efectivo",
        "p_monto_override": None,
    }

    try:
        res_rpc = db.rpc("rpc_registrar_socio_gimnasio", rpc_params).execute()
        resultado = get_data(res_rpc)
    except Exception as exc:
        raise_http_from_postgrest_error(exc, "No fue posible registrar o asociar el socio al gimnasio")

    if not isinstance(resultado, dict):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Respuesta inválida de rpc_registrar_socio_gimnasio.",
        )

    creado = bool(resultado.get("creado"))
    socio_clean = _clean_socio_dict(resultado.get("socio"))
    socio_gimnasio = resultado.get("socio_gimnasio")

    return {
        "mensaje": (
            "Socio creado y asociado al gimnasio exitosamente."
            if creado
            else "Identidad de socio existente reutilizada y asociada al gimnasio exitosamente."
        ),
        "creado": creado,
        "id_gimnasio": gym_id,
        "socio": socio_clean,
        "socio_gimnasio": socio_gimnasio,
    }


async def _asociar_socio_existente_logic(
    id_socio: int,
    id_gimnasio: Optional[int],
    current_user: Dict[str, Any],
) -> Dict[str, Any]:
    staff_ctx, gym_id = _verify_staff_can_manage_socios(current_user, id_gimnasio=id_gimnasio)
    db: Client = staff_ctx["supabase"]

    asociaciones_gym = _fetch_socio_gimnasios_for_gym(db, gym_id)
    for sg in asociaciones_gym:
        if int(sg.get("id_socio") or 0) == id_socio and sg.get("estado") is not False:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="El socio ya posee una asociación activa en este gimnasio.",
            )

    try:
        res_rpc = db.rpc(
            "rpc_asociar_socio_gimnasio",
            {
                "p_id_gimnasio": gym_id,
                "p_id_socio": id_socio,
            },
        ).execute()
        assoc = get_data(res_rpc)
    except Exception as exc:
        raise_http_from_postgrest_error(exc, "No fue posible asociar el socio al gimnasio")

    return {
        "mensaje": "Socio asociado al gimnasio exitosamente.",
        "id_gimnasio": gym_id,
        "socio_gimnasio": assoc,
    }


@router.post("/asociar", status_code=status.HTTP_201_CREATED)
async def asociar_socio_gimnasio(
    req: AsociarSocioGimnasioRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Asocia una identidad de socio existente (`id_socio`) a un gimnasio autorizado
    utilizando exclusivamente `public.rpc_asociar_socio_gimnasio`.
    """
    return await _asociar_socio_existente_logic(
        id_socio=req.id_socio,
        id_gimnasio=req.id_gimnasio,
        current_user=current_user,
    )


@router.post("/{id_socio}/asociaciones", status_code=status.HTTP_201_CREATED)
async def asociar_socio_por_ruta(
    id_socio: int = Path(..., gt=0),
    id_gimnasio: Optional[int] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Asocia un `id_socio` existente al gimnasio autorizado vía `public.rpc_asociar_socio_gimnasio`."""
    return await _asociar_socio_existente_logic(
        id_socio=id_socio,
        id_gimnasio=id_gimnasio,
        current_user=current_user,
    )


@router.get("/asociaciones/{id_socio_gimnasio}", status_code=status.HTTP_200_OK)
async def obtener_asociacion_socio_gimnasio(
    id_socio_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Consulta el estado y detalle de una asociación `public.socio_gimnasio` autorizada."""
    db: Client = current_user["supabase"]
    res = (
        db.from_("socio_gimnasio")
        .select(SOCIO_GIMNASIO_SELECT)
        .eq("id_socio_gimnasio", id_socio_gimnasio)
        .maybe_single()
        .execute()
    )
    assoc = get_data(res)
    if not isinstance(assoc, dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Asociación socio_gimnasio no encontrada.",
        )

    es_propio_socio = (
        current_user.get("rol") == "socio"
        and current_user.get("id_socio") is not None
        and int(assoc.get("id_socio") or 0) == int(current_user["id_socio"])
    )
    if not es_propio_socio:
        _verify_staff_can_view_socios(current_user, id_gimnasio=int(assoc["id_gimnasio"]))

    return {
        "socio_gimnasio": assoc,
    }


async def _cambiar_estado_asociacion_logic(
    id_socio_gimnasio: int,
    nuevo_estado: bool,
    current_user: Dict[str, Any],
    req: Optional[SocioChangeEstado] = None,
) -> Dict[str, Any]:
    db: Client = current_user["supabase"]
    res_sg = (
        db.from_("socio_gimnasio")
        .select(SOCIO_GIMNASIO_SELECT)
        .eq("id_socio_gimnasio", id_socio_gimnasio)
        .maybe_single()
        .execute()
    )
    assoc = get_data(res_sg)
    if not isinstance(assoc, dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Asociación socio_gimnasio no encontrada.",
        )

    gym_id = int(assoc["id_gimnasio"])
    staff_ctx, _ = _verify_staff_can_manage_socios(current_user, id_gimnasio=gym_id)

    if req is not None:
        if req.id_socio is not None and int(req.id_socio) != int(assoc["id_socio"]):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No está permitido transferir una asociación socio_gimnasio a otro socio.",
            )
        if req.id_gimnasio is not None and int(req.id_gimnasio) != gym_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No está permitido transferir una asociación socio_gimnasio a otro gimnasio.",
            )

    staff_db: Client = staff_ctx["supabase"]
    try:
        res_rpc = staff_db.rpc(
            "rpc_cambiar_estado_socio_gimnasio",
            {
                "p_id_socio_gimnasio": id_socio_gimnasio,
                "p_estado": bool(nuevo_estado),
            },
        ).execute()
        updated = get_data(res_rpc)
    except Exception as exc:
        raise_http_from_postgrest_error(exc, "No fue posible actualizar el estado de la asociación socio_gimnasio")

    return {
        "mensaje": (
            "Asociación socio-gimnasio activada exitosamente."
            if nuevo_estado
            else "Asociación socio-gimnasio desactivada exitosamente conservando la identidad del socio."
        ),
        "socio_gimnasio": updated,
    }


@router.patch("/asociaciones/{id_socio_gimnasio}/estado", status_code=status.HTTP_200_OK)
async def cambiar_estado_asociacion_socio_gimnasio(
    req: SocioChangeEstado,
    id_socio_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Activa o desactiva una asociación `public.socio_gimnasio` utilizando exclusivamente
    la RPC `public.rpc_cambiar_estado_socio_gimnasio`, sin eliminar la identidad del socio.
    """
    return await _cambiar_estado_asociacion_logic(
        id_socio_gimnasio=id_socio_gimnasio,
        nuevo_estado=req.estado,
        current_user=current_user,
        req=req,
    )


@router.delete("/asociaciones/{id_socio_gimnasio}", status_code=status.HTTP_200_OK)
async def desactivar_asociacion_socio_gimnasio(
    id_socio_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Desactiva lógicamente (`estado = false`) la asociación `public.socio_gimnasio`
    vía `public.rpc_cambiar_estado_socio_gimnasio`, conservando intacta la identidad global del socio.
    """
    return await _cambiar_estado_asociacion_logic(
        id_socio_gimnasio=id_socio_gimnasio,
        nuevo_estado=False,
        current_user=current_user,
    )


@router.get("/{id_socio}", status_code=status.HTTP_200_OK)
async def get_socio_by_id(
    id_socio: int = Path(..., gt=0),
    id_gimnasio: Optional[int] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Obtiene el detalle de un socio autorizado:
    - Un socio solo puede consultar su propio `id_socio` (403 ante otro socio).
    - Personal autorizado solo puede consultar socios asociados a su gimnasio (403 ante socios ajenos).
    """
    if current_user.get("rol") == "socio":
        own_id = current_user.get("id_socio")
        if not own_id or int(own_id) != id_socio:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes autorización para consultar los datos de otro socio.",
            )
        return await get_perfil_socio(id=None, id_socio=None, id_gimnasio=id_gimnasio, current_user=current_user)

    staff_ctx, gym_id = _verify_staff_can_view_socios(current_user, id_gimnasio=id_gimnasio)
    db: Client = staff_ctx["supabase"]
    asociaciones_gym = _fetch_socio_gimnasios_for_gym(db, gym_id)
    assoc = next((a for a in asociaciones_gym if int(a.get("id_socio") or 0) == id_socio), None)
    if not assoc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Socio no encontrado o no pertenece a tu gimnasio autorizado.",
        )

    socio_clean = _clean_socio_dict(assoc.get("socio"))
    if not socio_clean:
        res_s = db.from_("socio").select(SOCIO_COLUMNS).eq("id_socio", id_socio).maybe_single().execute()
        socio_clean = _clean_socio_dict(get_data(res_s))

    return {
        "socio": socio_clean,
        "socio_gimnasio": {
            "id_socio_gimnasio": assoc.get("id_socio_gimnasio"),
            "id_socio": assoc.get("id_socio"),
            "id_gimnasio": assoc.get("id_gimnasio"),
            "fecha_alta": assoc.get("fecha_alta"),
            "estado": assoc.get("estado"),
            "creado_en": assoc.get("creado_en"),
        },
        "gimnasio": assoc.get("gimnasio"),
    }


@router.put("/{id_socio}", status_code=status.HTTP_200_OK)
async def update_socio(
    req: SocioUpdate,
    id_socio: int = Path(..., gt=0),
    id_gimnasio: Optional[int] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Actualiza datos personales de un socio asociado al gimnasio autorizado (`public.socio`).
    - Bloquea intentos de modificar `auth_user_id`, `id_socio`, `fecha_alta`, `id_gimnasio` o contraseñas.
    - Modificar `socio.estado` global exige dueño, administrador organizacional o administrador de sucursal
      (`trg_proteger_campos_socio`).
    """
    if req.contrasena is not None or req.password is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Las contraseñas se administran exclusivamente en Supabase Auth, no en public.socio.",
        )
    if (
        req.auth_user_id is not None
        or req.fecha_alta is not None
        or req.id_gimnasio is not None
        or (req.id_socio is not None and str(req.id_socio).strip() != str(id_socio))
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No está permitido modificar campos protegidos (id_socio, auth_user_id, fecha_alta o id_gimnasio).",
        )

    staff_ctx, gym_id = _verify_staff_can_manage_socios(current_user, id_gimnasio=id_gimnasio)
    db: Client = staff_ctx["supabase"]

    asociaciones_gym = _fetch_socio_gimnasios_for_gym(db, gym_id)
    assoc = next((a for a in asociaciones_gym if int(a.get("id_socio") or 0) == id_socio), None)
    if not assoc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes autorización para modificar un socio que no está asociado a tu gimnasio.",
        )

    if req.estado is not None:
        staff_ctx = _verify_staff_can_change_socio_global_estado(current_user, gym_id)
        db = staff_ctx["supabase"]

    update_payload: Dict[str, Any] = {}
    if req.nombre is not None:
        if not req.nombre.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre no puede estar vacío.")
        update_payload["nombre"] = req.nombre.strip()
    if req.apellido is not None:
        if not req.apellido.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El apellido no puede estar vacío.")
        update_payload["apellido"] = req.apellido.strip()
    if req.dni is not None:
        update_payload["dni"] = str(req.dni).strip()
    if req.telefono is not None:
        update_payload["telefono"] = req.telefono.strip() if req.telefono.strip() else None
    if req.email is not None:
        update_payload["email"] = str(req.email).strip().lower()
    if req.estado is not None:
        update_payload["estado"] = bool(req.estado)

    if not update_payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se enviaron datos válidos para actualizar.",
        )

    res_upd = db.from_("socio").update(update_payload).eq("id_socio", id_socio).execute()
    rows = get_data(res_upd) or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Socio no encontrado.")

    return {
        "mensaje": "Socio actualizado correctamente.",
        "socio": _clean_socio_dict(rows[0]),
    }


@router.patch("/{id_socio}/estado", status_code=status.HTTP_200_OK)
async def change_estado_socio_en_gimnasio(
    req: SocioChangeEstado,
    id_socio: int = Path(..., gt=0),
    id_gimnasio: Optional[int] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Activa o desactiva la asociación `public.socio_gimnasio` del socio en el gimnasio autorizado
    mediante `public.rpc_cambiar_estado_socio_gimnasio`, conservando la identidad global del socio.
    """
    target_gym_id = req.id_gimnasio if req.id_gimnasio is not None else id_gimnasio
    staff_ctx, gym_id = _verify_staff_can_manage_socios(current_user, id_gimnasio=target_gym_id)
    db: Client = staff_ctx["supabase"]

    asociaciones_gym = _fetch_socio_gimnasios_for_gym(db, gym_id)
    assoc = next((a for a in asociaciones_gym if int(a.get("id_socio") or 0) == id_socio), None)
    if not assoc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El socio no posee una asociación en tu gimnasio autorizado.",
        )

    return await _cambiar_estado_asociacion_logic(
        id_socio_gimnasio=int(assoc["id_socio_gimnasio"]),
        nuevo_estado=req.estado,
        current_user=staff_ctx,
        req=req,
    )


@router.delete("/{id_socio}", status_code=status.HTTP_200_OK)
async def desactivar_socio_en_gimnasio(
    id_socio: int = Path(..., gt=0),
    id_gimnasio: Optional[int] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Baja lógica del socio en el gimnasio autorizado: desactiva su asociación `public.socio_gimnasio`
    vía `public.rpc_cambiar_estado_socio_gimnasio(id_socio_gimnasio, false)`.
    Nunca elimina físicamente `public.socio`, su cuenta Auth ni su historial contable/operativo.
    """
    staff_ctx, gym_id = _verify_staff_can_manage_socios(current_user, id_gimnasio=id_gimnasio)
    db: Client = staff_ctx["supabase"]

    asociaciones_gym = _fetch_socio_gimnasios_for_gym(db, gym_id)
    assoc = next((a for a in asociaciones_gym if int(a.get("id_socio") or 0) == id_socio), None)
    if not assoc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El socio no posee una asociación en tu gimnasio autorizado.",
        )

    return await _cambiar_estado_asociacion_logic(
        id_socio_gimnasio=int(assoc["id_socio_gimnasio"]),
        nuevo_estado=False,
        current_user=staff_ctx,
    )
