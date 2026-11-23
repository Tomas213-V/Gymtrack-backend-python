from datetime import date
from typing import Dict, Any, Optional, List, Set
from fastapi import APIRouter, HTTPException, status, Depends, Query, Path
from supabase import Client

from app.core.dependencies import (
    get_current_user,
    _switch_profile_context_if_available,
    can_access_sucursal,
)
from app.services.utils import get_data
from app.models.sucursal import SucursalCreate, SucursalUpdate, SucursalEstadoUpdate
from app.models.empleado import RegistrarEmpleadoSucursalRequest
from app.routers.gimnasios import (
    SUCURSAL_COLUMNS,
    ensure_active_organizational_assignments,
    verify_can_read_gimnasio,
    verify_dueno_or_org_admin_of_gimnasio,
    _filter_sucursales_for_user_and_gym,
)

router = APIRouter(prefix="/api/sucursales", tags=["Sucursales"])


def verify_can_read_sucursal(current_user: Dict[str, Any], id_sucursal: int) -> Dict[str, Any]:
    """
    Verifica que el usuario autenticado tenga acceso activo a la sucursal `id_sucursal`.
    Un empleado solo puede acceder a sucursales donde tenga una asignación activa (`estado = true`).
    """
    ensure_active_organizational_assignments(current_user)
    if can_access_sucursal(current_user, id_sucursal):
        return current_user

    if current_user.get("requested_role") is None:
        for candidate in ("dueño", "administrador", "empleado", "socio"):
            if candidate in (current_user.get("perfiles_disponibles") or []):
                p_ctx = (current_user.get("contexto_por_perfil") or {}).get(candidate, {})
                if id_sucursal in p_ctx.get("sucursal_ids", set()):
                    return _switch_profile_context_if_available(current_user, candidate)

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="No tienes autorización para acceder u operar en la sucursal solicitada.",
    )


def fetch_sucursal_or_404(db: Client, id_sucursal: int) -> Dict[str, Any]:
    res = (
        db.from_("sucursal")
        .select(SUCURSAL_COLUMNS)
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


async def crear_sucursal_logic(
    req: SucursalCreate,
    current_user: Dict[str, Any],
    explicit_gym_id: Optional[int] = None,
) -> Dict[str, Any]:
    target_gym_id = explicit_gym_id or req.id_gimnasio or current_user.get("id_gimnasio")
    if not target_gym_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes indicar el id_gimnasio para registrar la sucursal.",
        )

    manager_ctx = verify_dueno_or_org_admin_of_gimnasio(current_user, int(target_gym_id))
    direccion_limpia = (req.direccion or "").strip()
    if not direccion_limpia:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La dirección de la sucursal es obligatoria.",
        )

    nueva_sucursal = {
        "direccion": direccion_limpia,
        "telefono": req.telefono.strip() if req.telefono and req.telefono.strip() else None,
        "descripcion": req.descripcion.strip() if req.descripcion and req.descripcion.strip() else None,
        "fecha_registro": date.today().isoformat(),
        "estado": bool(req.estado),
        "id_gimnasio": int(target_gym_id),
    }

    db: Client = manager_ctx["supabase"]
    res_ins = db.from_("sucursal").insert([nueva_sucursal]).execute()
    rows = get_data(res_ins) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al registrar la sucursal.",
        )

    return {
        "mensaje": "Sucursal registrada exitosamente.",
        "sucursal": rows[0],
    }


@router.get("", status_code=status.HTTP_200_OK)
async def listar_sucursales(
    id_gimnasio: Optional[int] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Lista las sucursales autorizadas para el usuario autenticado.
    - Si se indica `id_gimnasio`, valida acceso activo a ese gimnasio.
    - Para un empleado, devuelve únicamente las sucursales donde tiene asignación activa.
    """
    ensure_active_organizational_assignments(current_user)
    user_ctx = current_user
    if id_gimnasio is not None:
        user_ctx = verify_can_read_gimnasio(current_user, id_gimnasio)
        sucursales = _filter_sucursales_for_user_and_gym(user_ctx, id_gimnasio)
        return {
            "id_gimnasio": id_gimnasio,
            "total": len(sucursales),
            "sucursales": sucursales,
        }

    # Sin filtro explícito: devolver todas las sucursales autorizadas en los gimnasios activos del perfil
    gym_ids = [
        int(g["id_gimnasio"])
        for g in (user_ctx.get("gimnasios") or [])
        if isinstance(g, dict) and g.get("id_gimnasio") is not None
    ]
    todas: List[Dict[str, Any]] = []
    seen_ids: Set[int] = set()
    for gid in gym_ids:
        for s in _filter_sucursales_for_user_and_gym(user_ctx, gid):
            sid = int(s.get("id_sucursal") or 0)
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                todas.append(s)

    return {
        "total": len(todas),
        "sucursales": todas,
    }


@router.get("/{id_sucursal}", status_code=status.HTTP_200_OK)
async def obtener_sucursal(
    id_sucursal: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Obtiene el detalle de una sucursal autorizada.
    Rechaza con 403 si el usuario no tiene relación activa con esa sucursal o si hay cruce con el gimnasio solicitado.
    """
    user_ctx = verify_can_read_sucursal(current_user, id_sucursal)
    db: Client = user_ctx["supabase"]
    suc = fetch_sucursal_or_404(db, id_sucursal)
    req_gym = current_user.get("requested_gym_id")
    if req_gym is not None and int(suc.get("id_gimnasio") or 0) != int(req_gym):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No se permite cruzar gimnasio y sucursal: la sucursal no pertenece al gimnasio indicado.",
        )
    return {
        "sucursal": suc,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def crear_sucursal(
    req: SucursalCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Crea una nueva sucursal en un gimnasio autorizado (exclusivo para dueño o
    administrador organizacional activo del gimnasio).
    """
    return await crear_sucursal_logic(req, current_user)


async def _actualizar_sucursal_logic(
    id_sucursal: int,
    req: SucursalUpdate,
    current_user: Dict[str, Any],
) -> Dict[str, Any]:
    ensure_active_organizational_assignments(current_user)
    user_ctx = verify_can_read_sucursal(current_user, id_sucursal)
    db: Client = user_ctx["supabase"]
    suc_actual = fetch_sucursal_or_404(db, id_sucursal)

    gym_id = int(suc_actual["id_gimnasio"])
    manager_ctx = verify_dueno_or_org_admin_of_gimnasio(current_user, gym_id)

    if req.id_gimnasio is not None and int(req.id_gimnasio) != gym_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No está permitido reasignar una sucursal a otro gimnasio.",
        )

    update_fields: Dict[str, Any] = {}
    if req.direccion is not None:
        clean_dir = req.direccion.strip()
        if not clean_dir:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La dirección de la sucursal no puede estar vacía.",
            )
        update_fields["direccion"] = clean_dir

    if req.telefono is not None:
        update_fields["telefono"] = req.telefono.strip() if req.telefono.strip() else None
    if req.descripcion is not None:
        update_fields["descripcion"] = req.descripcion.strip() if req.descripcion.strip() else None
    if req.estado is not None:
        update_fields["estado"] = bool(req.estado)

    if not update_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se enviaron datos válidos para actualizar la sucursal.",
        )

    manager_db: Client = manager_ctx["supabase"]
    res_upd = (
        manager_db.from_("sucursal")
        .update(update_fields)
        .eq("id_sucursal", id_sucursal)
        .execute()
    )
    rows = get_data(res_upd) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No fue posible actualizar la sucursal.",
        )

    return {
        "mensaje": "Sucursal actualizada exitosamente.",
        "sucursal": rows[0],
    }


@router.put("/{id_sucursal}", status_code=status.HTTP_200_OK)
async def actualizar_sucursal(
    req: SucursalUpdate,
    id_sucursal: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Actualiza los datos de ubicación o estado de una sucursal (dueño o administrador organizacional)."""
    return await _actualizar_sucursal_logic(id_sucursal, req, current_user)


@router.patch("/{id_sucursal}/estado", status_code=status.HTTP_200_OK)
async def cambiar_estado_sucursal(
    req: SucursalEstadoUpdate,
    id_sucursal: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Activa o desactiva lógicamente una sucursal sin eliminar su historial."""
    return await _actualizar_sucursal_logic(
        id_sucursal,
        SucursalUpdate(estado=req.estado),
        current_user,
    )


@router.get("/{id_sucursal}/empleados", status_code=status.HTTP_200_OK)
async def listar_empleados_de_sucursal(
    id_sucursal: int = Path(..., gt=0),
    rol: Optional[str] = Query(default=None),
    solo_activos: bool = Query(default=False),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Lista los empleados asignados a la sucursal indicada."""
    from app.routers.empleados import listar_empleados_logic

    return await listar_empleados_logic(
        current_user,
        id_sucursal=id_sucursal,
        rol=rol,
        solo_activos=solo_activos,
    )


@router.post("/{id_sucursal}/empleados", status_code=status.HTTP_201_CREATED)
async def registrar_empleado_en_sucursal(
    req: RegistrarEmpleadoSucursalRequest,
    id_sucursal: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Registra o reutiliza un empleado en la sucursal mediante `rpc_registrar_empleado_sucursal`."""
    from app.routers.empleados import registrar_empleado_sucursal_logic

    if req.id_sucursal is not None and int(req.id_sucursal) != id_sucursal:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El id_sucursal del cuerpo no coincide con la sucursal de la ruta.",
        )
    return await registrar_empleado_sucursal_logic(req, current_user, explicit_sucursal_id=id_sucursal)
