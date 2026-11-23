from datetime import date
from typing import Dict, Any, Optional, List, Set
from fastapi import APIRouter, HTTPException, status, Depends, Query, Path
from supabase import Client

from app.core.dependencies import (
    get_current_user,
    _switch_profile_context_if_available,
)
from app.services.utils import get_data, insert_gimnasio_with_rls
from app.models.gimnasio import GimnasioCreate, GimnasioUpdate, GimnasioEstadoUpdate
from app.models.sucursal import SucursalCreate
from app.models.administrador import RegistrarAdministradorGimnasioRequest

router = APIRouter(prefix="/api/gimnasios", tags=["Gimnasios"])

GIMNASIO_COLUMNS = "id_gimnasio, nombre, telefono, email, fecha_registro, estado, id_dueno"
SUCURSAL_COLUMNS = "id_sucursal, direccion, telefono, descripcion, fecha_registro, estado, id_gimnasio"


def ensure_active_organizational_assignments(current_user: Dict[str, Any]) -> None:
    """
    Verifica que un administrador organizacional o un empleado de sucursal posea
    al menos una asignación activa en base de datos antes de operar.
    """
    rol = current_user.get("rol")
    if rol == "administrador" and not current_user.get("gimnasios"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No posees asignaciones activas como administrador de gimnasio.",
        )
    if rol == "empleado" and not current_user.get("sucursales_asignadas"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No posees asignaciones operativas activas en ninguna sucursal.",
        )


def get_authorized_gym_ids_for_active_context(current_user: Dict[str, Any]) -> Set[int]:
    """Devuelve el conjunto de `id_gimnasio` autorizados para el perfil activo del usuario."""
    ensure_active_organizational_assignments(current_user)
    return {
        int(g["id_gimnasio"])
        for g in (current_user.get("gimnasios") or [])
        if isinstance(g, dict) and g.get("id_gimnasio") is not None
    }


def verify_can_read_gimnasio(current_user: Dict[str, Any], id_gimnasio: int) -> Dict[str, Any]:
    """
    Verifica que el usuario autenticado tenga acceso activo al `id_gimnasio` solicitado.
    Si el usuario tiene múltiples perfiles y no fijó `X-Active-Role`, busca en sus perfiles activos.
    """
    ensure_active_organizational_assignments(current_user)
    allowed_active = get_authorized_gym_ids_for_active_context(current_user)
    if id_gimnasio in allowed_active:
        return current_user

    if current_user.get("requested_role") is None:
        for candidate_profile in ("dueño", "administrador", "empleado", "socio"):
            if candidate_profile in (current_user.get("perfiles_disponibles") or []):
                p_ctx = (current_user.get("contexto_por_perfil") or {}).get(candidate_profile, {})
                if id_gimnasio in p_ctx.get("gym_ids", set()):
                    return _switch_profile_context_if_available(current_user, candidate_profile)

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="No tienes autorización para acceder u operar en el gimnasio solicitado.",
    )


def verify_dueno_owns_gimnasio(current_user: Dict[str, Any], id_gimnasio: int) -> Dict[str, Any]:
    """
    Exige que el usuario autenticado tenga perfil de Dueño (`public.dueno`) y sea
    el propietario titular (`gimnasio.id_dueno`) del `id_gimnasio` indicado.
    """
    dueno_ctx = _switch_profile_context_if_available(current_user, "dueño")
    id_dueno = dueno_ctx.get("id_dueno")
    if not id_dueno:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acción exclusiva para el dueño del gimnasio.",
        )

    dueno_gym_ids = {
        int(g["id_gimnasio"])
        for g in (dueno_ctx.get("gimnasios") or [])
        if isinstance(g, dict) and g.get("id_gimnasio") is not None
    }
    if id_gimnasio not in dueno_gym_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes autorización para administrar un gimnasio que no pertenece a tu cuenta de dueño.",
        )
    return dueno_ctx


def verify_dueno_or_org_admin_of_gimnasio(current_user: Dict[str, Any], id_gimnasio: int) -> Dict[str, Any]:
    """
    Exige que el usuario autenticado sea el Dueño titular (`public.dueno`) o un
    Administrador Organizacional activo (`public.administrador_gimnasio` con `estado = true`)
    del `id_gimnasio` indicado.
    Rechaza con 403 a empleados operativos de sucursal (incluso con rol 'administrador' de sucursal),
    administradores sin asignación activa, dueños de otros gimnasios y socios.
    """
    rol = current_user.get("rol")
    ctx_map = current_user.get("contexto_por_perfil") or {}

    if rol in ("dueño", "administrador"):
        ensure_active_organizational_assignments(current_user)
        active_gym_ids = get_authorized_gym_ids_for_active_context(current_user)
        if id_gimnasio in active_gym_ids:
            return current_user

    if current_user.get("requested_role") is None:
        for candidate in ("dueño", "administrador"):
            if candidate in (current_user.get("perfiles_disponibles") or []):
                p_ctx = ctx_map.get(candidate, {})
                if id_gimnasio in p_ctx.get("gym_ids", set()):
                    return _switch_profile_context_if_available(current_user, candidate)

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Solo el dueño o un administrador organizacional activo de este gimnasio pueden realizar esta operación.",
    )


def _filter_sucursales_for_user_and_gym(user_ctx: Dict[str, Any], id_gimnasio: int) -> List[Dict[str, Any]]:
    """
    Filtra las sucursales de `id_gimnasio` según el alcance real del usuario:
    - Dueño y Administrador Organizacional: todas las sucursales del gimnasio.
    - Empleado: únicamente las sucursales donde tiene asignación activa en `empleado_sucursal`.
    - Socio: sucursales activas del gimnasio donde está asociado.
    """
    db: Client = user_ctx["supabase"]
    rol = user_ctx.get("rol")

    if rol == "empleado":
        allowed_suc_ids = {
            int(s["id_sucursal"])
            for s in (user_ctx.get("sucursales_asignadas") or [])
            if isinstance(s, dict)
            and s.get("id_sucursal") is not None
            and s.get("estado") is not False
            and int(s.get("id_gimnasio") or 0) == id_gimnasio
        }
        if not allowed_suc_ids:
            return []
        res = (
            db.from_("sucursal")
            .select(SUCURSAL_COLUMNS)
            .eq("id_gimnasio", id_gimnasio)
            .in_("id_sucursal", sorted(allowed_suc_ids))
            .order("id_sucursal", desc=False)
            .execute()
        )
        rows = get_data(res)
        if isinstance(rows, list) and rows:
            return [r for r in rows if int(r.get("id_sucursal") or 0) in allowed_suc_ids]
        return [
            s for s in (user_ctx.get("sucursales_asignadas") or [])
            if int(s.get("id_sucursal") or 0) in allowed_suc_ids
        ]

    res = (
        db.from_("sucursal")
        .select(SUCURSAL_COLUMNS)
        .eq("id_gimnasio", id_gimnasio)
        .order("id_sucursal", desc=False)
        .execute()
    )
    rows = get_data(res)
    sucursales = rows if isinstance(rows, list) else []
    if rol == "socio":
        sucursales = [s for s in sucursales if s.get("estado") is not False]
    return sucursales


@router.get("", status_code=status.HTTP_200_OK)
async def listar_gimnasios_autorizados(
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Lista todos los gimnasios a los que el usuario autenticado tiene acceso activo
    (un dueño puede tener varios gimnasios; un administrador varios gimnasios asignados;
    un empleado varios gimnasios a través de sus sucursales activas; un socio varios gimnasios).
    """
    ensure_active_organizational_assignments(current_user)
    gimnasios = current_user.get("gimnasios") or []
    return {
        "total": len(gimnasios),
        "tieneGimnasio": bool(gimnasios),
        "gimnasio": current_user.get("gimnasio"),
        "gimnasios": gimnasios,
    }


@router.get("/catalogo", status_code=status.HTTP_200_OK)
async def listar_catalogo_gimnasios(
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Consulta el catálogo controlado `public.vw_catalogo_gimnasios` (security_barrier = true)
    para usuarios autenticados.
    """
    db: Client = current_user["supabase"]
    res = (
        db.from_("vw_catalogo_gimnasios")
        .select("id_gimnasio, nombre, telefono, estado")
        .order("id_gimnasio", desc=False)
        .execute()
    )
    rows = get_data(res)
    gimnasios = rows if isinstance(rows, list) else []
    return {
        "total": len(gimnasios),
        "gimnasios": gimnasios,
    }


@router.get("/mi-gimnasio", status_code=status.HTTP_200_OK)
async def obtener_mi_gimnasio(
    id_gimnasio: Optional[int] = Query(default=None),
    id_dueno: Optional[int] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Obtiene el gimnasio activo y la lista de gimnasios autorizados del usuario autenticado.
    Rechaza intentos de consultar gimnasios de otro dueño o no autorizados.
    """
    ensure_active_organizational_assignments(current_user)

    if id_dueno is not None and id_dueno != current_user.get("id_dueno"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes autorización para consultar gimnasios de otro dueño.",
        )

    user_ctx = current_user
    if id_gimnasio is not None:
        user_ctx = verify_can_read_gimnasio(current_user, id_gimnasio)

    gimnasios = user_ctx.get("gimnasios") or []
    active_gym_id = id_gimnasio or user_ctx.get("id_gimnasio")
    gimnasio = next(
        (g for g in gimnasios if int(g.get("id_gimnasio") or 0) == active_gym_id),
        user_ctx.get("gimnasio"),
    )
    sucursales = (
        _filter_sucursales_for_user_and_gym(user_ctx, int(gimnasio["id_gimnasio"]))
        if isinstance(gimnasio, dict) and gimnasio.get("id_gimnasio") is not None
        else []
    )

    return {
        "tieneGimnasio": bool(gimnasio),
        "gimnasio": gimnasio or None,
        "gimnasios": gimnasios,
        "sucursales": sucursales,
    }


@router.get("/{id_gimnasio}", status_code=status.HTTP_200_OK)
async def obtener_gimnasio_por_id(
    id_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Obtiene el detalle de un gimnasio autorizado y sus sucursales visibles para el usuario.
    """
    user_ctx = verify_can_read_gimnasio(current_user, id_gimnasio)
    db: Client = user_ctx["supabase"]

    res = (
        db.from_("gimnasio")
        .select(GIMNASIO_COLUMNS)
        .eq("id_gimnasio", id_gimnasio)
        .maybe_single()
        .execute()
    )
    gym = get_data(res)
    if not isinstance(gym, dict):
        gym = next(
            (g for g in (user_ctx.get("gimnasios") or []) if int(g.get("id_gimnasio") or 0) == id_gimnasio),
            None,
        )
    if not gym:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Gimnasio no encontrado.",
        )

    sucursales = _filter_sucursales_for_user_and_gym(user_ctx, id_gimnasio)
    return {
        "gimnasio": gym,
        "sucursales": sucursales,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def crear_gimnasio(
    req: GimnasioCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Crea un nuevo gimnasio (`public.gimnasio`) para el dueño autenticado (`id_dueno`).
    Un dueño puede registrar varios gimnasios; no incluye `direccion` (que pertenece a `public.sucursal`).
    """
    dueno_ctx = _switch_profile_context_if_available(current_user, "dueño")
    id_dueno = dueno_ctx.get("id_dueno")
    if not id_dueno:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo un usuario con perfil de dueño puede crear gimnasios.",
        )

    if req.id_dueno is not None and int(req.id_dueno) != int(id_dueno):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No puedes crear un gimnasio asignando el id_dueno de otro propietario.",
        )

    nombre_limpio = (req.nombre or "").strip()
    if not nombre_limpio:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El nombre del gimnasio es obligatorio.",
        )

    nuevo_gym = {
        "nombre": nombre_limpio,
        "telefono": req.telefono.strip() if req.telefono and req.telefono.strip() else None,
        "email": str(req.email).strip().lower() if req.email else None,
        "fecha_registro": date.today().isoformat(),
        "estado": bool(req.estado),
        "id_dueno": int(id_dueno),
    }

    db: Client = dueno_ctx["supabase"]
    gym_creado = insert_gimnasio_with_rls(db, nuevo_gym, columns=GIMNASIO_COLUMNS)
    if not gym_creado:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al registrar el gimnasio.",
        )

    return {
        "mensaje": "Gimnasio registrado exitosamente.",
        "gimnasio": gym_creado,
    }


async def _actualizar_gimnasio_logic(
    req: GimnasioUpdate,
    current_user: Dict[str, Any],
    gym_id: Optional[int] = None,
) -> Dict[str, Any]:
    dueno_ctx = _switch_profile_context_if_available(current_user, "dueño")
    id_dueno = dueno_ctx.get("id_dueno")
    if not id_dueno:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo el dueño titular puede modificar los datos del gimnasio.",
        )

    if req.id_dueno is not None and int(req.id_dueno) != int(id_dueno):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No está permitido transferir el propietario (id_dueno) del gimnasio.",
        )

    target_gym_id = gym_id or dueno_ctx.get("id_gimnasio")
    if not target_gym_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes indicar el gimnasio a actualizar.",
        )

    verify_dueno_owns_gimnasio(dueno_ctx, int(target_gym_id))

    update_fields: Dict[str, Any] = {}
    if req.nombre is not None:
        clean_name = req.nombre.strip()
        if not clean_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El nombre del gimnasio no puede estar vacío.",
            )
        update_fields["nombre"] = clean_name

    if req.telefono is not None:
        update_fields["telefono"] = req.telefono.strip() if req.telefono.strip() else None
    if req.email is not None:
        update_fields["email"] = str(req.email).strip().lower() if req.email else None
    if req.estado is not None:
        update_fields["estado"] = bool(req.estado)

    if not update_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se enviaron datos válidos para actualizar.",
        )

    db: Client = dueno_ctx["supabase"]
    res_upd = (
        db.from_("gimnasio")
        .update(update_fields)
        .eq("id_gimnasio", int(target_gym_id))
        .execute()
    )
    rows = get_data(res_upd) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Gimnasio no encontrado o sin autorización para actualizar.",
        )

    return {
        "mensaje": "Datos del gimnasio actualizados exitosamente.",
        "gimnasio": rows[0],
    }


@router.put("/mi-gimnasio", status_code=status.HTTP_200_OK)
async def actualizar_mi_gimnasio(
    req: GimnasioUpdate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Actualiza los datos del gimnasio activo del dueño autenticado."""
    return await _actualizar_gimnasio_logic(req, current_user)


@router.put("/{id_gimnasio}", status_code=status.HTTP_200_OK)
async def actualizar_gimnasio_por_id(
    req: GimnasioUpdate,
    id_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Actualiza los datos de un gimnasio específico perteneciente al dueño autenticado."""
    return await _actualizar_gimnasio_logic(req, current_user, gym_id=id_gimnasio)


@router.patch("/{id_gimnasio}/estado", status_code=status.HTTP_200_OK)
async def cambiar_estado_gimnasio(
    req: GimnasioEstadoUpdate,
    id_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Activa o desactiva lógicamente un gimnasio perteneciente al dueño autenticado."""
    return await _actualizar_gimnasio_logic(
        GimnasioUpdate(estado=req.estado),
        current_user,
        gym_id=id_gimnasio,
    )


@router.get("/{id_gimnasio}/sucursales", status_code=status.HTTP_200_OK)
async def listar_sucursales_de_gimnasio(
    id_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Lista las sucursales autorizadas pertenecientes al gimnasio indicado."""
    user_ctx = verify_can_read_gimnasio(current_user, id_gimnasio)
    sucursales = _filter_sucursales_for_user_and_gym(user_ctx, id_gimnasio)
    return {
        "id_gimnasio": id_gimnasio,
        "total": len(sucursales),
        "sucursales": sucursales,
    }


@router.post("/{id_gimnasio}/sucursales", status_code=status.HTTP_201_CREATED)
async def crear_sucursal_en_gimnasio(
    req: SucursalCreate,
    id_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Crea una sucursal dentro del gimnasio indicado (dueño o administrador organizacional activo)."""
    from app.routers.sucursales import crear_sucursal_logic

    if req.id_gimnasio is not None and int(req.id_gimnasio) != id_gimnasio:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El id_gimnasio del cuerpo no coincide con el gimnasio de la ruta.",
        )
    return await crear_sucursal_logic(req, current_user, explicit_gym_id=id_gimnasio)


@router.get("/{id_gimnasio}/administradores", status_code=status.HTTP_200_OK)
async def listar_administradores_de_gimnasio(
    id_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Lista los administradores organizacionales asignados al gimnasio indicado."""
    from app.routers.administradores import listar_administradores_logic

    return await listar_administradores_logic(current_user, id_gimnasio=id_gimnasio)


@router.post("/{id_gimnasio}/administradores", status_code=status.HTTP_201_CREATED)
async def registrar_administrador_en_gimnasio(
    req: RegistrarAdministradorGimnasioRequest,
    id_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Registra o asigna un administrador organizacional al gimnasio mediante RPC oficial."""
    from app.routers.administradores import registrar_administrador_logic

    if req.id_gimnasio is not None and int(req.id_gimnasio) != id_gimnasio:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El id_gimnasio del cuerpo no coincide con el gimnasio de la ruta.",
        )
    return await registrar_administrador_logic(req, current_user, explicit_gym_id=id_gimnasio)


@router.get("/{id_gimnasio}/empleados", status_code=status.HTTP_200_OK)
async def listar_empleados_de_gimnasio(
    id_gimnasio: int = Path(..., gt=0),
    id_sucursal: Optional[int] = Query(default=None),
    rol: Optional[str] = Query(default=None),
    solo_activos: bool = Query(default=False),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Lista los empleados y sus asignaciones en las sucursales del gimnasio indicado."""
    from app.routers.empleados import listar_empleados_logic

    return await listar_empleados_logic(
        current_user,
        id_gimnasio=id_gimnasio,
        id_sucursal=id_sucursal,
        rol=rol,
        solo_activos=solo_activos,
    )
