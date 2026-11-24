from typing import Dict, Any, Optional, List, Set
from fastapi import APIRouter, HTTPException, status, Depends, Query, Path
from supabase import Client

from app.core.dependencies import (
    get_current_user,
    require_empleado,
    _switch_profile_context_if_available,
)
from app.services.utils import get_data, raise_http_from_postgrest_error
from app.services.account_provisioning import (
    detectar_identidad_para_incorporacion,
    validate_initial_password_policy,
    aprovisionar_o_vincular_cuenta_auth,
)
from app.models.empleado import (
    RegistrarEmpleadoSucursalRequest,
    AsignacionEmpleadoSucursalCreate,
    AsignacionEmpleadoSucursalUpdate,
    AsignacionEmpleadoSucursalEstadoUpdate,
    EmpleadoUpdate,
)
from app.routers.gimnasios import (
    ensure_active_organizational_assignments,
    verify_dueno_or_org_admin_of_gimnasio,
)
from app.routers.sucursales import fetch_sucursal_or_404

router = APIRouter(prefix="/api/empleados", tags=["Empleados y Asignaciones por Sucursal"])

EMPLEADO_COLUMNS = "id_empleado, auth_user_id, nombre, apellido, dni, email, telefono, estado, fecha_creacion"
EMPLEADO_SUCURSAL_SELECT = (
    "id_empleado_sucursal, rol, id_empleado, id_sucursal, estado, fecha_asignacion, "
    "empleado(id_empleado, auth_user_id, nombre, apellido, dni, email, telefono, estado, fecha_creacion), "
    "sucursal(id_sucursal, direccion, telefono, descripcion, estado, id_gimnasio)"
)


def _verify_can_list_empleados(
    current_user: Dict[str, Any],
    id_gimnasio: Optional[int] = None,
    id_sucursal: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Valida quién puede consultar el padrón de empleados y asignaciones de un gimnasio/sucursal,
    en correspondencia exacta con las políticas SQL `empleado_select_authorized` y
    `empleado_sucursal_select_authorized`:
      - Dueño del gimnasio (`fn_dueno_posee_gimnasio`).
      - Administrador organizacional activo del gimnasio (`fn_admin_posee_gimnasio`).
      - Empleado operativo con asignación activa y rol `'administrador'` en ese gimnasio (`fn_empleado_en_gimnasio(id_gimnasio, array['administrador'])`).
    Rechaza con 403 a recepcionistas, entrenadores, socios y usuarios de otros gimnasios.
    """
    ensure_active_organizational_assignments(current_user)
    db: Client = current_user["supabase"]

    target_gym_id = id_gimnasio
    if id_sucursal is not None:
        suc = fetch_sucursal_or_404(db, int(id_sucursal))
        suc_gym_id = int(suc["id_gimnasio"])
        if target_gym_id is not None and int(target_gym_id) != suc_gym_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="La sucursal indicada no pertenece al gimnasio solicitado.",
            )
        target_gym_id = suc_gym_id

    if target_gym_id is None:
        target_gym_id = current_user.get("id_gimnasio")

    def _profile_allows_read(ctx: Dict[str, Any], gid: Optional[int]) -> bool:
        rol = ctx.get("rol")
        gym_ids = {
            int(g["id_gimnasio"])
            for g in (ctx.get("gimnasios") or [])
            if isinstance(g, dict) and g.get("id_gimnasio") is not None
        }
        if rol in ("dueño", "administrador"):
            if gid is not None:
                return gid in gym_ids
            return bool(gym_ids)
        if rol == "empleado":
            roles_por_gym = ctx.get("roles_empleado_por_gimnasio") or {}
            if gid is not None:
                return "administrador" in (roles_por_gym.get(gid) or [])
            return any("administrador" in (rlist or []) for rlist in roles_por_gym.values())
        return False

    if _profile_allows_read(current_user, target_gym_id):
        return {**current_user, "_resolved_target_gym_id": target_gym_id}

    if current_user.get("requested_role") is None:
        for candidate in ("dueño", "administrador", "empleado"):
            if candidate in (current_user.get("perfiles_disponibles") or []):
                switched = _switch_profile_context_if_available(current_user, candidate)
                if _profile_allows_read(switched, target_gym_id):
                    return {**switched, "_resolved_target_gym_id": target_gym_id}

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="No tienes permisos para consultar empleados en este gimnasio o sucursal (requiere dueño, administrador organizacional o administrador de sucursal).",
    )


def _verify_can_manage_empleados_of_sucursal(
    current_user: Dict[str, Any],
    id_sucursal: int,
    expected_gym_id: Optional[int] = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Valida que el usuario autenticado sea el Dueño o un Administrador Organizacional activo
    del gimnasio al que pertenece `id_sucursal` (en correspondencia con `rpc_registrar_empleado_sucursal`
    y la política `empleado_sucursal_write_dueno`), e impide cruzar `id_gimnasio` con una `id_sucursal`
    de otra organización.
    Rechaza con 403 a empleados de sucursal (recepcionistas, entrenadores, administradores de sucursal),
    socios y administradores/dueños de otros gimnasios.
    """
    ensure_active_organizational_assignments(current_user)
    db: Client = current_user["supabase"]
    sucursal = fetch_sucursal_or_404(db, int(id_sucursal))
    gym_id = int(sucursal["id_gimnasio"])

    req_gym = expected_gym_id or current_user.get("requested_gym_id")
    if req_gym is not None and int(req_gym) != gym_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No se permite cruzar gimnasio y sucursal: la sucursal seleccionada no pertenece al gimnasio indicado.",
        )

    manager_ctx = verify_dueno_or_org_admin_of_gimnasio(current_user, gym_id)
    return manager_ctx, sucursal


async def listar_empleados_logic(
    current_user: Dict[str, Any],
    id_gimnasio: Optional[int] = None,
    id_sucursal: Optional[int] = None,
    rol: Optional[str] = None,
    solo_activos: bool = False,
) -> Dict[str, Any]:
    reader_ctx = _verify_can_list_empleados(
        current_user,
        id_gimnasio=id_gimnasio,
        id_sucursal=id_sucursal,
    )
    target_gym_id = reader_ctx.get("_resolved_target_gym_id")
    db: Client = reader_ctx["supabase"]

    allowed_gym_ids: Set[int] = {
        int(g["id_gimnasio"])
        for g in (reader_ctx.get("gimnasios") or [])
        if isinstance(g, dict) and g.get("id_gimnasio") is not None
    }
    if reader_ctx.get("rol") == "empleado":
        roles_por_gym = reader_ctx.get("roles_empleado_por_gimnasio") or {}
        allowed_gym_ids = {gid for gid, rlist in roles_por_gym.items() if "administrador" in (rlist or [])}

    gym_filter_ids = [int(target_gym_id)] if target_gym_id is not None else sorted(allowed_gym_ids)
    if not gym_filter_ids:
        return {"total": 0, "asignaciones": [], "empleados": []}

    # Obtener las sucursales del alcance autorizado
    res_sucs = (
        db.from_("sucursal")
        .select("id_sucursal, direccion, telefono, descripcion, estado, id_gimnasio")
        .in_("id_gimnasio", gym_filter_ids)
        .order("id_sucursal", desc=False)
        .execute()
    )
    sucs_rows = get_data(res_sucs) or []
    sucs_by_id: Dict[int, Dict[str, Any]] = {
        int(s["id_sucursal"]): s
        for s in (sucs_rows if isinstance(sucs_rows, list) else [])
        if isinstance(s, dict) and s.get("id_sucursal") is not None and int(s.get("id_gimnasio") or 0) in set(gym_filter_ids)
    }

    if id_sucursal is not None:
        if int(id_sucursal) not in sucs_by_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes autorización sobre la sucursal solicitada.",
            )
        target_suc_ids = [int(id_sucursal)]
    else:
        target_suc_ids = sorted(sucs_by_id.keys())

    if not target_suc_ids:
        return {"total": 0, "asignaciones": [], "empleados": []}

    query = db.from_("empleado_sucursal").select(EMPLEADO_SUCURSAL_SELECT)
    if len(target_suc_ids) == 1:
        query = query.eq("id_sucursal", target_suc_ids[0])
    else:
        query = query.in_("id_sucursal", target_suc_ids)

    res_es = query.order("id_empleado_sucursal", desc=False).execute()
    raw_es = get_data(res_es) or []
    asignaciones: List[Dict[str, Any]] = []
    rol_filter = rol.strip().lower() if rol and rol.strip() else None

    for row in (raw_es if isinstance(raw_es, list) else []):
        if not isinstance(row, dict):
            continue
        sid = int(row.get("id_sucursal") or 0)
        if sid not in sucs_by_id:
            continue
        if solo_activos and row.get("estado") is False:
            continue
        if rol_filter and str(row.get("rol") or "").strip().lower() != rol_filter:
            continue
        asignaciones.append(row)

    empleados_map: Dict[int, Dict[str, Any]] = {}
    for asig in asignaciones:
        emp_obj = asig.get("empleado") if isinstance(asig.get("empleado"), dict) else None
        eid = int(asig.get("id_empleado") or (emp_obj.get("id_empleado") if emp_obj else 0))
        if not eid:
            continue
        if eid not in empleados_map:
            base_emp = {
                k: v
                for k, v in (emp_obj or {"id_empleado": eid}).items()
                if k != "id_gimnasio" and k != "empleado_sucursal"
            }
            empleados_map[eid] = {
                **base_emp,
                "asignaciones": [],
            }
        suc_obj = asig.get("sucursal") if isinstance(asig.get("sucursal"), dict) else sucs_by_id.get(int(asig["id_sucursal"]), {})
        empleados_map[eid]["asignaciones"].append(
            {
                "id_empleado_sucursal": asig.get("id_empleado_sucursal"),
                "id_empleado": eid,
                "id_sucursal": asig.get("id_sucursal"),
                "id_gimnasio": suc_obj.get("id_gimnasio"),
                "rol": asig.get("rol"),
                "estado": asig.get("estado"),
                "fecha_asignacion": asig.get("fecha_asignacion"),
                "sucursal": suc_obj,
            }
        )

    return {
        "total": len(asignaciones),
        "asignaciones": asignaciones,
        "empleados": list(empleados_map.values()),
    }


async def registrar_empleado_sucursal_logic(
    req: RegistrarEmpleadoSucursalRequest,
    current_user: Dict[str, Any],
    explicit_sucursal_id: Optional[int] = None,
) -> Dict[str, Any]:
    target_sucursal_id = explicit_sucursal_id or req.id_sucursal or current_user.get("id_sucursal")
    if not target_sucursal_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes indicar el id_sucursal donde asignar al empleado.",
        )

    manager_ctx, sucursal = _verify_can_manage_empleados_of_sucursal(
        current_user,
        int(target_sucursal_id),
        expected_gym_id=req.id_gimnasio,
    )
    db: Client = manager_ctx["supabase"]

    clean_dni = req.dni.strip() if req.dni and req.dni.strip() else None
    clean_email = str(req.email).strip().lower() if req.email else None
    target_emp_id = int(req.id_empleado) if req.id_empleado is not None else None
    raw_password = req.contrasena_inicial or req.contrasena or req.password

    # Comprobar si el empleado ya existe para:
    # 1. Impedir que un empleado desactivado (empleado.estado = False) reciba nuevas asignaciones.
    # 2. Prevenir asociaciones activas duplicadas en la misma sucursal.
    # 3. Proteger credenciales globales: jamás sobrescribir contraseña de cuenta existente.
    existing_emp: Optional[Dict[str, Any]] = None
    if target_emp_id is not None:
        res_e = db.from_("empleado").select(EMPLEADO_COLUMNS).eq("id_empleado", target_emp_id).maybe_single().execute()
        data_e = get_data(res_e)
        if isinstance(data_e, dict):
            existing_emp = data_e
    elif clean_email:
        res_e = db.from_("empleado").select(EMPLEADO_COLUMNS).eq("email", clean_email).maybe_single().execute()
        data_e = get_data(res_e)
        if isinstance(data_e, dict):
            existing_emp = data_e
    elif clean_dni:
        res_e = db.from_("empleado").select(EMPLEADO_COLUMNS).eq("dni", clean_dni).maybe_single().execute()
        data_e = get_data(res_e)
        if isinstance(data_e, dict):
            existing_emp = data_e

    if existing_emp is not None:
        if existing_emp.get("estado") is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No se puede asignar a una sucursal un empleado cuya identidad se encuentra desactivada.",
            )
        emp_id_resolved = int(existing_emp["id_empleado"])
        res_es_dup = (
            db.from_("empleado_sucursal")
            .select("id_empleado_sucursal, id_empleado, id_sucursal, rol, estado")
            .eq("id_sucursal", int(target_sucursal_id))
            .eq("id_empleado", emp_id_resolved)
            .maybe_single()
            .execute()
        )
        dup_asig = get_data(res_es_dup)
        if isinstance(dup_asig, dict) and dup_asig.get("estado") is not False:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="El empleado ya posee una asignación activa en esta sucursal.",
            )

    detection: Dict[str, Any] = {}
    if clean_dni or clean_email:
        detection = detectar_identidad_para_incorporacion(
            db=db,
            actor_auth_user_id=manager_ctx.get("auth_user_id"),
            tipo_perfil="empleado",
            gym_id=int(sucursal["id_gimnasio"]),
            sucursal_id=int(target_sucursal_id),
            dni=clean_dni,
            email=clean_email,
            skip_rate_limit=True,
        )
        if detection.get("conflicto_identidad"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=detection["mensaje"],
            )
        if detection.get("ya_asignado_en_esta_sucursal"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="El empleado ya posee una asignación activa en esta sucursal.",
            )
        if detection.get("_internal_perfil_estado") is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No se puede asignar a una sucursal un empleado cuya identidad se encuentra desactivada.",
            )

    tiene_auth_previa = bool(
        (existing_emp and existing_emp.get("auth_user_id"))
        or detection.get("tiene_cuenta_auth")
    )
    perfil_previo = bool(existing_emp is not None or detection.get("existe_perfil_mismo_tipo"))
    validated_password = validate_initial_password_policy(
        password_inicial=raw_password,
        email=clean_email,
        tiene_cuenta_auth_existente=tiene_auth_previa,
        perfil_ya_existia=perfil_previo,
    )

    rpc_params = {
        "p_id_sucursal": int(target_sucursal_id),
        "p_rol": req.rol.strip().lower(),
        "p_nombre": req.nombre.strip() if req.nombre and req.nombre.strip() else None,
        "p_apellido": req.apellido.strip() if req.apellido and req.apellido.strip() else None,
        "p_dni": clean_dni,
        "p_email": clean_email,
        "p_telefono": req.telefono.strip() if req.telefono and req.telefono.strip() else None,
        "p_id_empleado": target_emp_id,
    }

    try:
        res = db.rpc("rpc_registrar_empleado_sucursal", rpc_params).execute()
        resultado = get_data(res)
    except Exception as exc:
        raise_http_from_postgrest_error(exc, "No fue posible registrar o asignar al empleado en la sucursal")

    if not isinstance(resultado, dict):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Respuesta inválida de rpc_registrar_empleado_sucursal.",
        )

    creado = bool(resultado.get("creado"))
    empleado_data = resultado.get("empleado")
    if isinstance(empleado_data, dict):
        empleado_data = {
            k: v
            for k, v in empleado_data.items()
            if k not in ("id_gimnasio", "contrasena", "password")
        }

    auth_info = {"cuenta_auth_creada": False, "cuenta_auth_reutilizada": False, "requiere_confirmacion_email": False}
    if isinstance(empleado_data, dict) and empleado_data.get("id_empleado"):
        if not creado and validated_password:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Esta persona ya tiene un registro en GymTrack. "
                    "No está permitido modificar su contraseña al incorporarla."
                ),
            )
        auth_info = aprovisionar_o_vincular_cuenta_auth(
            tipo_perfil="empleado",
            pk_val=int(empleado_data["id_empleado"]),
            email=empleado_data.get("email") or clean_email,
            nombre=empleado_data.get("nombre") or req.nombre,
            apellido=empleado_data.get("apellido") or req.apellido,
            password_inicial=validated_password if creado else None,
            existing_profile_auth_uid=empleado_data.get("auth_user_id"),
            existing_global_auth_uid=detection.get("_internal_global_auth_uid"),
        )

    return {
        "mensaje": (
            "Empleado creado y asignado a la sucursal exitosamente."
            if creado
            else "Identidad de empleado existente reutilizada y asignada a la sucursal exitosamente."
        ),
        "creado": creado,
        "id_gimnasio": sucursal.get("id_gimnasio"),
        "empleado": empleado_data,
        "empleado_sucursal": resultado.get("empleado_sucursal"),
        **auth_info,
    }


@router.get("", status_code=status.HTTP_200_OK)
async def listar_empleados(
    id_gimnasio: Optional[int] = Query(default=None),
    id_sucursal: Optional[int] = Query(default=None),
    rol: Optional[str] = Query(default=None),
    solo_activos: bool = Query(default=False),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Lista los empleados y sus asignaciones a sucursales (`public.empleado_sucursal`)
    dentro del gimnasio o sucursal autorizada.
    """
    return await listar_empleados_logic(
        current_user,
        id_gimnasio=id_gimnasio,
        id_sucursal=id_sucursal,
        rol=rol,
        solo_activos=solo_activos,
    )


@router.get("/mis-asignaciones", status_code=status.HTTP_200_OK)
async def obtener_mis_asignaciones_empleado(
    current_user: Dict[str, Any] = Depends(require_empleado()),
):
    """
    Devuelve la identidad personal del empleado autenticado (`public.empleado`)
    y sus asignaciones activas por sucursal y gimnasio (`public.empleado_sucursal`).
    """
    emp = dict(current_user.get("empleado") or {})
    emp.pop("id_gimnasio", None)
    return {
        "empleado": emp,
        "sucursales_asignadas": current_user.get("sucursales_asignadas") or [],
        "gimnasios": current_user.get("gimnasios") or [],
        "roles_empleado": current_user.get("roles_empleado") or [],
        "roles_empleado_por_gimnasio": current_user.get("roles_empleado_por_gimnasio") or {},
        "roles_empleado_por_sucursal": current_user.get("roles_empleado_por_sucursal") or {},
    }


@router.get("/{id_empleado}", status_code=status.HTTP_200_OK)
async def obtener_empleado_por_id(
    id_empleado: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Obtiene los datos personales de un empleado (`public.empleado`, sin `id_gimnasio`)
    y sus asignaciones en las sucursales autorizadas para el usuario consultante.
    """
    ensure_active_organizational_assignments(current_user)
    es_propio = (
        current_user.get("rol") == "empleado"
        and current_user.get("id_empleado") == id_empleado
    )

    if es_propio:
        emp = dict(current_user.get("empleado") or {})
        emp.pop("id_gimnasio", None)
        return {
            "empleado": emp,
            "asignaciones": current_user.get("sucursales_asignadas") or [],
        }

    listado = await listar_empleados_logic(current_user)
    empleado = next(
        (e for e in (listado.get("empleados") or []) if int(e.get("id_empleado") or 0) == id_empleado),
        None,
    )
    if not empleado:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Empleado no encontrado en tus gimnasios o sucursales autorizadas.",
        )

    return {
        "empleado": empleado,
        "asignaciones": empleado.get("asignaciones") or [],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def registrar_o_reutilizar_empleado(
    req: RegistrarEmpleadoSucursalRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Registra un nuevo empleado o reutiliza un `id_empleado` existente (por `id_empleado`, `dni` o `email`)
    y lo asigna a una sucursal autorizada mediante la RPC oficial `public.rpc_registrar_empleado_sucursal`.
    """
    return await registrar_empleado_sucursal_logic(req, current_user)


@router.post("/{id_empleado}/asignaciones", status_code=status.HTTP_201_CREATED)
async def asignar_empleado_existente_a_sucursal(
    req: AsignacionEmpleadoSucursalCreate,
    id_empleado: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Incorpora o reasigna un `id_empleado` existente a otra sucursal (del mismo o de otro gimnasio)
    reutilizando su identidad personal mediante `public.rpc_registrar_empleado_sucursal`.
    """
    rpc_req = RegistrarEmpleadoSucursalRequest(
        id_sucursal=req.id_sucursal,
        rol=req.rol,
        id_empleado=id_empleado,
    )
    return await registrar_empleado_sucursal_logic(rpc_req, current_user, explicit_sucursal_id=req.id_sucursal)


async def _modificar_asignacion_empleado_logic(
    id_empleado_sucursal: int,
    req: AsignacionEmpleadoSucursalUpdate,
    current_user: Dict[str, Any],
) -> Dict[str, Any]:
    ensure_active_organizational_assignments(current_user)
    db: Client = current_user["supabase"]

    res_es = (
        db.from_("empleado_sucursal")
        .select(EMPLEADO_SUCURSAL_SELECT)
        .eq("id_empleado_sucursal", id_empleado_sucursal)
        .maybe_single()
        .execute()
    )
    asignacion = get_data(res_es)
    if not isinstance(asignacion, dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Asignación empleado_sucursal no encontrada.",
        )

    id_sucursal = int(asignacion["id_sucursal"])
    manager_ctx, _ = _verify_can_manage_empleados_of_sucursal(current_user, id_sucursal)

    if req.id_empleado is not None and int(req.id_empleado) != int(asignacion["id_empleado"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No está permitido transferir una asignación empleado_sucursal a otro empleado.",
        )
    if req.id_sucursal is not None and int(req.id_sucursal) != id_sucursal:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No está permitido transferir una asignación empleado_sucursal a otra sucursal.",
        )

    emp_obj = asignacion.get("empleado") if isinstance(asignacion.get("empleado"), dict) else None
    if emp_obj and emp_obj.get("estado") is False and req.estado is not False:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se puede activar o modificar una asignación en sucursal para un empleado desactivado.",
        )

    update_fields: Dict[str, Any] = {}
    if req.rol is not None:
        update_fields["rol"] = req.rol.strip().lower()
    if req.estado is not None:
        update_fields["estado"] = bool(req.estado)

    if not update_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes indicar el rol y/o estado a actualizar en la asignación.",
        )

    manager_db: Client = manager_ctx["supabase"]
    res_upd = (
        manager_db.from_("empleado_sucursal")
        .update(update_fields)
        .eq("id_empleado_sucursal", id_empleado_sucursal)
        .execute()
    )
    rows = get_data(res_upd) or []
    updated = rows[0] if rows else {**asignacion, **update_fields}

    return {
        "mensaje": (
            "Asignación de empleado desactivada exitosamente sin eliminar su identidad."
            if update_fields.get("estado") is False
            else "Asignación de empleado actualizada exitosamente."
        ),
        "empleado_sucursal": updated,
    }


@router.put("/asignaciones/{id_empleado_sucursal}", status_code=status.HTTP_200_OK)
async def actualizar_asignacion_empleado_sucursal(
    req: AsignacionEmpleadoSucursalUpdate,
    id_empleado_sucursal: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Actualiza el rol operativo (`administrador`, `recepcionista`, `entrenador`) o estado
    de una asignación `public.empleado_sucursal` (exclusivo para dueño o administrador organizacional).
    """
    return await _modificar_asignacion_empleado_logic(id_empleado_sucursal, req, current_user)


@router.patch("/asignaciones/{id_empleado_sucursal}/estado", status_code=status.HTTP_200_OK)
async def cambiar_estado_asignacion_empleado_sucursal(
    req: AsignacionEmpleadoSucursalEstadoUpdate,
    id_empleado_sucursal: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Activa o desactiva lógicamente una asignación `public.empleado_sucursal` sin eliminar la identidad del empleado.
    """
    return await _modificar_asignacion_empleado_logic(
        id_empleado_sucursal,
        AsignacionEmpleadoSucursalUpdate(estado=req.estado),
        current_user,
    )


@router.delete("/asignaciones/{id_empleado_sucursal}", status_code=status.HTTP_200_OK)
async def desactivar_asignacion_empleado_sucursal(
    id_empleado_sucursal: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Da de baja una relación laboral en una sucursal desactivando su asignación (`estado = false`)
    en `public.empleado_sucursal`, conservando intacta la identidad reutilizable en `public.empleado`.
    """
    return await _modificar_asignacion_empleado_logic(
        id_empleado_sucursal,
        AsignacionEmpleadoSucursalUpdate(estado=False),
        current_user,
    )


@router.put("/{id_empleado}", status_code=status.HTTP_200_OK)
async def actualizar_datos_personales_empleado(
    req: EmpleadoUpdate,
    id_empleado: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Actualiza los datos personales de un empleado en `public.empleado`.
    Respeta `trg_fn_proteger_campos_empleado`: un dueño o administrador organizacional de un gimnasio
    no puede desactivar globalmente `empleado.estado = false` si el empleado posee asignaciones activas en otro gimnasio.
    """
    ensure_active_organizational_assignments(current_user)
    db: Client = current_user["supabase"]

    res_es = (
        db.from_("empleado_sucursal")
        .select("id_empleado_sucursal, id_empleado, id_sucursal, estado, sucursal(id_sucursal, id_gimnasio)")
        .eq("id_empleado", id_empleado)
        .execute()
    )
    es_rows = get_data(res_es) or []
    asignaciones = [r for r in (es_rows if isinstance(es_rows, list) else []) if isinstance(r, dict)]

    # Determinar los gimnasios que el usuario gestiona como dueño o administrador organizacional
    rol = current_user.get("rol")
    manager_ctx = current_user
    if rol not in ("dueño", "administrador"):
        if current_user.get("requested_role") is None:
            if "dueño" in (current_user.get("perfiles_disponibles") or []):
                manager_ctx = _switch_profile_context_if_available(current_user, "dueño")
            elif "administrador" in (current_user.get("perfiles_disponibles") or []):
                manager_ctx = _switch_profile_context_if_available(current_user, "administrador")
            else:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Solo el dueño o un administrador organizacional pueden actualizar fichas de empleados.",
                )
        else:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Solo el dueño o un administrador organizacional pueden actualizar fichas de empleados.",
            )

    managed_gym_ids = {
        int(g["id_gimnasio"])
        for g in (manager_ctx.get("gimnasios") or [])
        if isinstance(g, dict) and g.get("id_gimnasio") is not None
    }

    tiene_relacion_en_gym_gestionado = False
    tiene_activa_en_otro_gym = False
    for asig in asignaciones:
        suc = asig.get("sucursal") if isinstance(asig.get("sucursal"), dict) else {}
        gid = int(suc.get("id_gimnasio") or 0)
        if gid in managed_gym_ids:
            tiene_relacion_en_gym_gestionado = True
        elif asig.get("estado") is not False and gid > 0:
            tiene_activa_en_otro_gym = True

    if not tiene_relacion_en_gym_gestionado:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes autorización para modificar un empleado sin asignaciones en tus gimnasios.",
        )

    if req.estado is False and tiene_activa_en_otro_gym:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No puedes desactivar globalmente a un empleado con asignaciones activas en otro gimnasio; desactiva sus asignaciones en tus sucursales.",
        )

    update_fields: Dict[str, Any] = {}
    if req.nombre is not None:
        if not req.nombre.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre no puede estar vacío.")
        update_fields["nombre"] = req.nombre.strip()
    if req.apellido is not None:
        if not req.apellido.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El apellido no puede estar vacío.")
        update_fields["apellido"] = req.apellido.strip()
    if req.dni is not None:
        update_fields["dni"] = req.dni.strip() if req.dni.strip() else None
    if req.telefono is not None:
        update_fields["telefono"] = req.telefono.strip() if req.telefono.strip() else None
    if req.email is not None:
        update_fields["email"] = str(req.email).strip().lower()
    if req.estado is not None:
        update_fields["estado"] = bool(req.estado)

    if not update_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se enviaron datos válidos para actualizar.",
        )

    manager_db: Client = manager_ctx["supabase"]
    res_upd = (
        manager_db.from_("empleado")
        .update(update_fields)
        .eq("id_empleado", id_empleado)
        .execute()
    )
    rows = get_data(res_upd) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Empleado no encontrado.",
        )

    emp_updated = {k: v for k, v in rows[0].items() if k != "id_gimnasio"}
    return {
        "mensaje": "Datos del empleado actualizados exitosamente.",
        "empleado": emp_updated,
    }
