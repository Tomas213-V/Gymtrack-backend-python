from typing import Dict, Any, Optional, List
from fastapi import APIRouter, HTTPException, status, Depends, Query, Path
from supabase import Client

from app.core.dependencies import (
    get_current_user,
    require_administrador,
    _switch_profile_context_if_available,
)
from app.services.utils import get_data, raise_http_from_postgrest_error
from app.models.administrador import (
    RegistrarAdministradorGimnasioRequest,
    AdministradorUpdate,
    AsignacionAdministradorGimnasioUpdate,
)
from app.routers.gimnasios import (
    verify_dueno_owns_gimnasio,
    verify_dueno_or_org_admin_of_gimnasio,
)

router = APIRouter(prefix="/api/administradores", tags=["Administradores Organizacionales"])

ADMIN_GIMNASIO_SELECT = (
    "id_administrador_gimnasio, id_administrador, id_gimnasio, fecha_asignacion, estado, "
    "administrador(id_administrador, auth_user_id, nombre, apellido, dni, email, telefono, estado, fecha_creacion), "
    "gimnasio(id_gimnasio, nombre, telefono, email, estado, id_dueno)"
)


async def listar_administradores_logic(
    current_user: Dict[str, Any],
    id_gimnasio: Optional[int] = None,
) -> Dict[str, Any]:
    target_gym_id = id_gimnasio or current_user.get("id_gimnasio")
    if target_gym_id is not None:
        manager_ctx = verify_dueno_or_org_admin_of_gimnasio(current_user, int(target_gym_id))
        gym_ids = [int(target_gym_id)]
    else:
        # Sin gimnasio específico: validar que sea dueño o administrador organizacional activo
        rol = current_user.get("rol")
        if rol not in ("dueño", "administrador"):
            if current_user.get("requested_role") is None:
                if "dueño" in (current_user.get("perfiles_disponibles") or []):
                    manager_ctx = _switch_profile_context_if_available(current_user, "dueño")
                elif "administrador" in (current_user.get("perfiles_disponibles") or []):
                    manager_ctx = _switch_profile_context_if_available(current_user, "administrador")
                else:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Solo el dueño o un administrador organizacional activo pueden consultar administradores.",
                    )
            else:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Solo el dueño o un administrador organizacional activo pueden consultar administradores.",
                )
        else:
            manager_ctx = current_user

        if manager_ctx.get("rol") == "administrador" and not manager_ctx.get("gimnasios"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No posees asignaciones activas como administrador de gimnasio.",
            )

        gym_ids = [
            int(g["id_gimnasio"])
            for g in (manager_ctx.get("gimnasios") or [])
            if isinstance(g, dict) and g.get("id_gimnasio") is not None
        ]

    if not gym_ids:
        return {"total": 0, "asignaciones": [], "administradores": []}

    db: Client = manager_ctx["supabase"]
    query = db.from_("administrador_gimnasio").select(ADMIN_GIMNASIO_SELECT)
    if len(gym_ids) == 1:
        query = query.eq("id_gimnasio", gym_ids[0])
    else:
        query = query.in_("id_gimnasio", gym_ids)

    res = query.order("id_administrador_gimnasio", desc=False).execute()
    rows = get_data(res)
    asignaciones = [
        r for r in (rows if isinstance(rows, list) else [])
        if isinstance(r, dict) and int(r.get("id_gimnasio") or 0) in set(gym_ids)
    ]

    admins_map: Dict[int, Dict[str, Any]] = {}
    for ag in asignaciones:
        adm = ag.get("administrador")
        if isinstance(adm, dict) and adm.get("id_administrador") is not None:
            aid = int(adm["id_administrador"])
            if aid not in admins_map:
                admins_map[aid] = {**adm, "asignaciones": []}
            admins_map[aid]["asignaciones"].append(
                {
                    "id_administrador_gimnasio": ag.get("id_administrador_gimnasio"),
                    "id_gimnasio": ag.get("id_gimnasio"),
                    "estado": ag.get("estado"),
                    "fecha_asignacion": ag.get("fecha_asignacion"),
                    "gimnasio": ag.get("gimnasio"),
                }
            )

    return {
        "total": len(asignaciones),
        "asignaciones": asignaciones,
        "administradores": list(admins_map.values()),
    }


async def registrar_administrador_logic(
    req: RegistrarAdministradorGimnasioRequest,
    current_user: Dict[str, Any],
    explicit_gym_id: Optional[int] = None,
) -> Dict[str, Any]:
    target_gym_id = explicit_gym_id or req.id_gimnasio or current_user.get("id_gimnasio")
    if not target_gym_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes indicar el id_gimnasio donde asignar al administrador.",
        )

    dueno_ctx = verify_dueno_owns_gimnasio(current_user, int(target_gym_id))
    db: Client = dueno_ctx["supabase"]

    clean_email = str(req.email).strip().lower()
    clean_dni = req.dni.strip() if req.dni and req.dni.strip() else None

    # Prevenir asociaciones activas duplicadas del mismo administrador en el mismo gimnasio
    res_existing_ag = (
        db.from_("administrador_gimnasio")
        .select(ADMIN_GIMNASIO_SELECT)
        .eq("id_gimnasio", int(target_gym_id))
        .execute()
    )
    existing_ags = get_data(res_existing_ag) or []
    for ag_row in (existing_ags if isinstance(existing_ags, list) else []):
        if not isinstance(ag_row, dict) or ag_row.get("estado") is False:
            continue
        adm_obj = ag_row.get("administrador")
        if isinstance(adm_obj, dict):
            adm_email = str(adm_obj.get("email") or "").strip().lower()
            adm_dni = str(adm_obj.get("dni") or "").strip() if adm_obj.get("dni") else None
            if adm_email == clean_email or (clean_dni and adm_dni == clean_dni):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="El administrador ya posee una asignación activa en este gimnasio.",
                )

    rpc_params = {
        "p_id_gimnasio": int(target_gym_id),
        "p_nombre": req.nombre.strip(),
        "p_apellido": req.apellido.strip(),
        "p_email": clean_email,
        "p_dni": clean_dni,
        "p_telefono": req.telefono.strip() if req.telefono and req.telefono.strip() else None,
    }

    try:
        res = db.rpc("rpc_registrar_administrador_gimnasio", rpc_params).execute()
        resultado = get_data(res)
    except Exception as exc:
        raise_http_from_postgrest_error(exc, "No fue posible registrar o asignar al administrador")

    if not isinstance(resultado, dict):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Respuesta inválida de rpc_registrar_administrador_gimnasio.",
        )

    creado = bool(resultado.get("creado"))
    return {
        "mensaje": (
            "Administrador creado y asignado al gimnasio exitosamente."
            if creado
            else "Administrador existente vinculado al gimnasio exitosamente."
        ),
        "creado": creado,
        "administrador": resultado.get("administrador"),
        "administrador_gimnasio": resultado.get("administrador_gimnasio"),
    }


@router.get("", status_code=status.HTTP_200_OK)
async def listar_administradores(
    id_gimnasio: Optional[int] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Lista los administradores organizacionales y sus asignaciones (`public.administrador_gimnasio`)
    para el gimnasio autorizado (exclusivo para dueño o administrador organizacional activo).
    """
    return await listar_administradores_logic(current_user, id_gimnasio=id_gimnasio)


@router.get("/mis-asignaciones", status_code=status.HTTP_200_OK)
async def obtener_mis_asignaciones_administrador(
    current_user: Dict[str, Any] = Depends(require_administrador),
):
    """
    Devuelve el perfil del administrador organizacional autenticado y sus gimnasios asignados.
    """
    return {
        "administrador": current_user.get("administrador"),
        "asignaciones": current_user.get("administrador_gimnasios") or [],
        "gimnasios": current_user.get("gimnasios") or [],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def registrar_administrador_gimnasio(
    req: RegistrarAdministradorGimnasioRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Registra o vincula un administrador organizacional a un gimnasio del dueño autenticado
    utilizando exclusivamente la RPC `public.rpc_registrar_administrador_gimnasio`.
    """
    return await registrar_administrador_logic(req, current_user)


@router.put("/{id_administrador}", status_code=status.HTTP_200_OK)
async def actualizar_administrador(
    req: AdministradorUpdate,
    id_administrador: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Actualiza datos personales de un administrador organizacional (`public.administrador`).
    - El propio administrador puede actualizar sus datos personales, pero NO su `estado`.
    - El dueño de un gimnasio vinculado puede actualizar datos y activar/desactivar `estado`.
    """
    db: Client = current_user["supabase"]
    es_propio = (
        current_user.get("rol") == "administrador"
        and current_user.get("id_administrador") == id_administrador
    )

    es_dueno_autorizado = False
    if not es_propio or req.estado is not None:
        dueno_ctx = _switch_profile_context_if_available(current_user, "dueño")
        dueno_gym_ids = {
            int(g["id_gimnasio"])
            for g in (dueno_ctx.get("gimnasios") or [])
            if isinstance(g, dict) and g.get("id_gimnasio") is not None
        }
        res_ag = (
            dueno_ctx["supabase"]
            .from_("administrador_gimnasio")
            .select("id_administrador_gimnasio, id_administrador, id_gimnasio, estado")
            .eq("id_administrador", id_administrador)
            .execute()
        )
        ags = get_data(res_ag) or []
        es_dueno_autorizado = any(
            isinstance(r, dict) and int(r.get("id_gimnasio") or 0) in dueno_gym_ids
            for r in (ags if isinstance(ags, list) else [])
        )
        if not es_dueno_autorizado:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Solo el dueño del gimnasio vinculado puede modificar este perfil de administrador.",
            )
        db = dueno_ctx["supabase"]

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
            detail="No se enviaron campos válidos para actualizar.",
        )

    res_upd = (
        db.from_("administrador")
        .update(update_fields)
        .eq("id_administrador", id_administrador)
        .execute()
    )
    rows = get_data(res_upd) or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Administrador no encontrado.",
        )

    return {
        "mensaje": "Administrador actualizado exitosamente.",
        "administrador": rows[0],
    }


async def _cambiar_estado_asignacion_admin_logic(
    id_administrador_gimnasio: int,
    nuevo_estado: bool,
    current_user: Dict[str, Any],
    req: Optional[AsignacionAdministradorGimnasioUpdate] = None,
) -> Dict[str, Any]:
    dueno_ctx = _switch_profile_context_if_available(current_user, "dueño")
    db: Client = dueno_ctx["supabase"]

    res_ag = (
        db.from_("administrador_gimnasio")
        .select("id_administrador_gimnasio, id_administrador, id_gimnasio, fecha_asignacion, estado")
        .eq("id_administrador_gimnasio", id_administrador_gimnasio)
        .maybe_single()
        .execute()
    )
    asignacion = get_data(res_ag)
    if not isinstance(asignacion, dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Asignación administrador_gimnasio no encontrada.",
        )

    verify_dueno_owns_gimnasio(dueno_ctx, int(asignacion["id_gimnasio"]))

    if req is not None:
        if req.id_administrador is not None and int(req.id_administrador) != int(asignacion["id_administrador"]):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No se puede transferir una asignación administrador_gimnasio a otro administrador.",
            )
        if req.id_gimnasio is not None and int(req.id_gimnasio) != int(asignacion["id_gimnasio"]):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No se puede transferir una asignación administrador_gimnasio a otro gimnasio.",
            )

    res_upd = (
        db.from_("administrador_gimnasio")
        .update({"estado": bool(nuevo_estado)})
        .eq("id_administrador_gimnasio", id_administrador_gimnasio)
        .execute()
    )
    rows = get_data(res_upd) or []
    updated = rows[0] if rows else {**asignacion, "estado": bool(nuevo_estado)}

    return {
        "mensaje": (
            "Asignación de administrador activada exitosamente."
            if nuevo_estado
            else "Asignación de administrador desactivada exitosamente sin eliminar su identidad."
        ),
        "administrador_gimnasio": updated,
    }


@router.patch("/asignaciones/{id_administrador_gimnasio}/estado", status_code=status.HTTP_200_OK)
async def cambiar_estado_asignacion_administrador(
    req: AsignacionAdministradorGimnasioUpdate,
    id_administrador_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Activa o desactiva la asignación de un administrador a un gimnasio (`public.administrador_gimnasio`)
    sin eliminar la identidad del administrador (exclusivo para el dueño del gimnasio).
    """
    return await _cambiar_estado_asignacion_admin_logic(
        id_administrador_gimnasio,
        req.estado,
        current_user,
        req=req,
    )


@router.delete("/asignaciones/{id_administrador_gimnasio}", status_code=status.HTTP_200_OK)
async def desactivar_asignacion_administrador(
    id_administrador_gimnasio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Desactiva lógicamente (`estado = false`) la asignación de un administrador a un gimnasio,
    conservando su identidad en `public.administrador`.
    """
    return await _cambiar_estado_asignacion_admin_logic(
        id_administrador_gimnasio,
        False,
        current_user,
    )
