from datetime import date
from typing import Dict, Any, Optional, List
from fastapi import APIRouter, HTTPException, status, Depends, Query, Path
from supabase import Client

from app.core.dependencies import get_current_user, run_db_parallel
from app.services.utils import get_data
from app.models.membresia import (
    MembresiaCreate,
    MembresiaRenovarRequest,
    MembresiaUpdate,
)
from app.routers.socios import (
    _verify_staff_can_view_socios,
    _verify_staff_can_manage_socios,
    _resolve_socio_profile_context,
    _fetch_socio_gimnasios_for_gym,
    _fetch_socio_gimnasios_for_socio,
)
from app.routers.planes_membresia import PLAN_COLUMNS

router = APIRouter(prefix="/api/membresias", tags=["Membresías y Renovaciones"])

MEMBRESIA_SOCIO_SELECT = (
    "id_membresia_socio, id_socio_gimnasio, id_membresia_gimnasio, id_gimnasio, "
    "precio, duracion_dias_contratados, limite_asistencias_semanales, limite_asistencias_totales, "
    "fecha_inicio, fecha_vencimiento, estado, creado_en, "
    "socio_gimnasio(id_socio_gimnasio, id_socio, id_gimnasio, fecha_alta, estado, "
    "socio(id_socio, nombre, apellido, dni, email, telefono, estado)), "
    "membresia_gimnasio(id_membresia_gimnasio, nombre, descripcion, precio, duracion_dias, estado, id_gimnasio)"
)

ESTADOS_MEMBRESIA_VALIDOS = {"pendiente", "activa", "vencida", "cancelada"}


def _map_rpc_error_status(exc: Exception) -> int:
    msg = str(exc).lower()
    if any(k in msg for k in ("ya existe", "superpuest", "duplicad", "pendiente", "rango", "conflict")):
        return status.HTTP_409_CONFLICT
    if any(k in msg for k in ("no tienes autorización", "sin permisos", "no autorizado")):
        return status.HTTP_403_FORBIDDEN
    if "no existe" in msg or "no encontrad" in msg:
        return status.HTTP_404_NOT_FOUND
    return status.HTTP_400_BAD_REQUEST


def _fetch_membresia_or_404(db: Client, id_membresia_socio: int) -> Dict[str, Any]:
    res = (
        db.from_("membresia_socio")
        .select(MEMBRESIA_SOCIO_SELECT)
        .eq("id_membresia_socio", id_membresia_socio)
        .maybe_single()
        .execute()
    )
    memb = get_data(res)
    if not isinstance(memb, dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Contratación de membresía no encontrada.",
        )
    return memb


def _extract_socio_id_from_membresia(memb: Dict[str, Any]) -> Optional[int]:
    sg = memb.get("socio_gimnasio")
    if isinstance(sg, dict) and sg.get("id_socio") is not None:
        return int(sg["id_socio"])
    return None


@router.get("/mis-membresias", status_code=status.HTTP_200_OK)
async def get_mis_membresias(
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    estado: Optional[str] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Devuelve el historial completo de contrataciones (`public.membresia_socio`) del socio autenticado
    en todos sus gimnasios asociados (o filtrado por un `id_gimnasio` autorizado).
    """
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]
    access_token = socio_ctx.get("access_token")

    [asociaciones] = await run_db_parallel(
        db,
        access_token,
        [lambda c: _fetch_socio_gimnasios_for_socio(c, own_socio_id)],
    )
    sg_ids_by_gym = {
        int(a["id_gimnasio"]): int(a["id_socio_gimnasio"])
        for a in asociaciones
        if a.get("id_socio_gimnasio") is not None and a.get("id_gimnasio") is not None
    }
    if id_gimnasio is not None and id_gimnasio not in sg_ids_by_gym:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No posees asociación con el gimnasio solicitado.",
        )

    target_sg_ids = [sg_ids_by_gym[id_gimnasio]] if id_gimnasio is not None else list(sg_ids_by_gym.values())
    if not target_sg_ids:
        return {"id_socio": own_socio_id, "total": 0, "membresias": []}

    def _q_mis_membs(c: Client):
        q = c.from_("membresia_socio").select(MEMBRESIA_SOCIO_SELECT)
        if len(target_sg_ids) == 1:
            q = q.eq("id_socio_gimnasio", target_sg_ids[0])
        else:
            q = q.in_("id_socio_gimnasio", target_sg_ids)
        return get_data(q.order("fecha_vencimiento", desc=True).execute())

    [rows] = await run_db_parallel(db, access_token, [_q_mis_membs])
    estado_norm = estado.strip().lower() if estado and estado.strip().lower() != "todos" else None
    if estado_norm and estado_norm not in ESTADOS_MEMBRESIA_VALIDOS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Estado de membresía inválido. Permitidos: pendiente, activa, vencida, cancelada.",
        )

    membresias = [
        m for m in (rows if isinstance(rows, list) else [])
        if isinstance(m, dict)
        and int(m.get("id_socio_gimnasio") or 0) in set(target_sg_ids)
        and (estado_norm is None or str(m.get("estado") or "").lower() == estado_norm)
    ]
    return {
        "id_socio": own_socio_id,
        "total": len(membresias),
        "membresias": membresias,
    }


@router.get("", status_code=status.HTTP_200_OK)
async def get_membresias(
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    id_socio: Optional[int] = Query(default=None, gt=0),
    estado: Optional[str] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Lista el historial de contrataciones (`public.membresia_socio`):
    - Si es socio: solo sus propias contrataciones (rechaza con 403 si intenta consultar otro `id_socio`).
    - Si es staff autorizado: todas las contrataciones del gimnasio autorizado (filtrables por `id_socio` y `estado`).
    """
    if current_user.get("rol") == "socio":
        own_id = current_user.get("id_socio")
        if id_socio is not None and (not own_id or int(id_socio) != int(own_id)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes autorización para consultar las contrataciones de otro socio.",
            )
        return await get_mis_membresias(id_gimnasio=id_gimnasio, estado=estado, current_user=current_user)

    staff_ctx, gym_id = _verify_staff_can_view_socios(current_user, id_gimnasio=id_gimnasio)
    estado_norm = estado.strip().lower() if estado and estado.strip().lower() != "todos" else None
    if estado_norm and estado_norm not in ESTADOS_MEMBRESIA_VALIDOS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Estado de membresía inválido. Permitidos: pendiente, activa, vencida, cancelada.",
        )

    db: Client = staff_ctx["supabase"]
    [rows] = await run_db_parallel(
        db,
        staff_ctx.get("access_token"),
        [
            lambda c: get_data(
                c.from_("membresia_socio")
                .select(MEMBRESIA_SOCIO_SELECT)
                .eq("id_gimnasio", gym_id)
                .order("fecha_vencimiento", desc=True)
                .execute()
            )
        ],
    )
    membresias: List[Dict[str, Any]] = []
    for m in (rows if isinstance(rows, list) else []):
        if not isinstance(m, dict) or int(m.get("id_gimnasio") or 0) != gym_id:
            continue
        if id_socio is not None and _extract_socio_id_from_membresia(m) != id_socio:
            continue
        if estado_norm and str(m.get("estado") or "").lower() != estado_norm:
            continue
        membresias.append(m)

    return {
        "id_gimnasio": gym_id,
        "total": len(membresias),
        "membresias": membresias,
    }


@router.get("/{id_membresia_socio}", status_code=status.HTTP_200_OK)
async def get_membresia_by_id(
    id_membresia_socio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Obtiene una contratación específica validando que pertenezca al socio o al gimnasio autorizado."""
    db: Client = current_user["supabase"]
    memb = _fetch_membresia_or_404(db, id_membresia_socio)

    if current_user.get("rol") == "socio":
        own_id = current_user.get("id_socio")
        contract_socio_id = _extract_socio_id_from_membresia(memb)
        if not own_id or contract_socio_id != int(own_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes autorización para consultar una contratación ajena.",
            )
        return {"membresia": memb}

    _verify_staff_can_view_socios(current_user, id_gimnasio=int(memb["id_gimnasio"]))
    return {"membresia": memb}


async def _contratar_o_renovar_logic(
    *,
    id_socio_req: Optional[int],
    id_membresia_gimnasio: int,
    id_gimnasio_req: Optional[int],
    fecha_inicio: Optional[str],
    estado: str,
    registrar_pago: bool,
    metodo_pago: str,
    estado_pago: str,
    comprobante: Optional[str],
    precio_tamper: Any,
    monto_override_tamper: Any,
    id_socio_gimnasio_tamper: Any = None,
    fecha_vencimiento_tamper: Any = None,
    current_user: Dict[str, Any],
    es_renovacion: bool = False,
) -> Dict[str, Any]:
    """
    Orquesta contrataciones y renovaciones en `public.membresia_socio` delegando la operación
    transaccional y el encadenamiento de fechas exclusivamente en `public.rpc_contratar_membresia`.
    """
    if precio_tamper is not None or monto_override_tamper is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No está permitido imponer un precio o descuento desde el cliente; el importe proviene del plan de membresía.",
        )
    if id_socio_gimnasio_tamper is not None or fecha_vencimiento_tamper is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No está permitido enviar id_socio_gimnasio ni fecha_vencimiento manualmente; el backend y la RPC los resuelven.",
        )

    db_initial: Client = current_user["supabase"]
    res_plan = (
        db_initial.from_("membresia_gimnasio")
        .select(PLAN_COLUMNS)
        .eq("id_membresia_gimnasio", id_membresia_gimnasio)
        .maybe_single()
        .execute()
    )
    plan = get_data(res_plan)
    if not isinstance(plan, dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El plan de membresía especificado no existe.",
        )

    plan_gym_id = int(plan["id_gimnasio"])
    if id_gimnasio_req is not None and int(id_gimnasio_req) != plan_gym_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No se permite cruzar gimnasios: el plan no pertenece al gimnasio de la contratación.",
        )

    req_gym_header = current_user.get("requested_gym_id")
    if req_gym_header is not None and int(req_gym_header) != plan_gym_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No se permite cruzar gimnasios: el plan no pertenece al gimnasio del contexto activo.",
        )

    if plan.get("estado") is not True:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se puede contratar ni renovar un plan de membresía inactivo.",
        )

    rol = current_user.get("rol")
    if rol == "socio":
        own_socio_id = int(current_user["id_socio"])
        if id_socio_req is not None and int(id_socio_req) != own_socio_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Un socio no puede contratar membresías para otro socio.",
            )
        if estado != "pendiente":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Un socio solo puede solicitar contrataciones en estado 'pendiente' hasta la acreditación del pago.",
            )
        if registrar_pago:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Un socio no puede registrar pagos administrativos aprobados directamente.",
            )
        target_socio_id = own_socio_id
        actor_db = db_initial
        asociaciones_socio = _fetch_socio_gimnasios_for_socio(actor_db, own_socio_id)
        assoc = next(
            (a for a in asociaciones_socio if int(a.get("id_gimnasio") or 0) == plan_gym_id),
            None,
        )
    else:
        if id_socio_req is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Debes indicar el id_socio para registrar la contratación.",
            )
        staff_ctx, _ = _verify_staff_can_manage_socios(current_user, id_gimnasio=plan_gym_id)
        target_socio_id = int(id_socio_req)
        actor_db = staff_ctx["supabase"]
        asociaciones_gym = _fetch_socio_gimnasios_for_gym(actor_db, plan_gym_id)
        assoc = next(
            (a for a in asociaciones_gym if int(a.get("id_socio") or 0) == target_socio_id),
            None,
        )

    # Validar explícitamente que exista una asociación socio_gimnasio ACTIVA en este gimnasio
    if not isinstance(assoc, dict) or assoc.get("estado") is not True:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El socio no posee una asociación socio_gimnasio activa en este gimnasio.",
        )

    rpc_params = {
        "p_id_socio": target_socio_id,
        "p_id_membresia_gimnasio": id_membresia_gimnasio,
        "p_fecha_inicio": fecha_inicio or date.today().isoformat(),
        "p_estado": estado,
        "p_registrar_pago": bool(registrar_pago),
        "p_metodo_pago": metodo_pago,
        "p_estado_pago": estado_pago,
        "p_comprobante": comprobante.strip() if comprobante and comprobante.strip() else None,
        "p_monto_override": None,
    }

    try:
        res_rpc = actor_db.rpc("rpc_contratar_membresia", rpc_params).execute()
        resultado = get_data(res_rpc)
    except Exception as exc:
        raise HTTPException(
            status_code=_map_rpc_error_status(exc),
            detail=f"No fue posible completar la contratación de membresía: {exc}",
        ) from exc

    if not isinstance(resultado, dict):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Respuesta inválida de rpc_contratar_membresia.",
        )

    return {
        "mensaje": (
            "Renovación de membresía registrada exitosamente (nueva contratación histórica creada)."
            if es_renovacion
            else "Membresía contratada exitosamente."
        ),
        "socio_gimnasio": resultado.get("socio_gimnasio"),
        "membresia": resultado.get("membresia_socio"),
        "membresia_socio": resultado.get("membresia_socio"),
        "pago": resultado.get("pago"),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_membresia(
    req: MembresiaCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Registra una contratación nueva en `public.membresia_socio` vía `public.rpc_contratar_membresia`.
    """
    return await _contratar_o_renovar_logic(
        id_socio_req=req.id_socio,
        id_membresia_gimnasio=req.id_membresia_gimnasio,
        id_gimnasio_req=req.id_gimnasio,
        fecha_inicio=req.fecha_inicio,
        estado=req.estado,
        registrar_pago=req.registrar_pago,
        metodo_pago=req.metodo_pago,
        estado_pago=req.estado_pago,
        comprobante=req.comprobante,
        precio_tamper=req.precio,
        monto_override_tamper=req.monto_override,
        id_socio_gimnasio_tamper=req.id_socio_gimnasio,
        fecha_vencimiento_tamper=req.fecha_vencimiento,
        current_user=current_user,
        es_renovacion=False,
    )


@router.post("/renovar", status_code=status.HTTP_201_CREATED)
async def renovar_membresia(
    req: MembresiaRenovarRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Renueva la membresía de un socio creando una NUEVA fila en `public.membresia_socio`
    vía `public.rpc_contratar_membresia` sin sobrescribir ni alterar contrataciones anteriores.
    """
    return await _contratar_o_renovar_logic(
        id_socio_req=req.id_socio,
        id_membresia_gimnasio=req.id_membresia_gimnasio,
        id_gimnasio_req=req.id_gimnasio,
        fecha_inicio=req.fecha_inicio,
        estado=req.estado,
        registrar_pago=req.registrar_pago,
        metodo_pago=req.metodo_pago,
        estado_pago=req.estado_pago,
        comprobante=req.comprobante,
        precio_tamper=req.precio,
        monto_override_tamper=req.monto_override,
        current_user=current_user,
        es_renovacion=True,
    )


@router.patch("/{id_membresia_socio}", status_code=status.HTTP_200_OK)
async def update_membresia(
    req: MembresiaUpdate,
    id_membresia_socio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Actualiza el estado o fecha de vencimiento de una contratación en `public.membresia_socio`
    utilizando exclusivamente la RPC `public.rpc_actualizar_estado_membresia`.
    Bloquea modificaciones de condiciones históricas (`precio`, `fecha_inicio`, plan o titulares).
    """
    if (
        req.id_membresia_gimnasio is not None
        or req.id_socio_gimnasio is not None
        or req.id_gimnasio is not None
        or req.fecha_inicio is not None
        or req.precio is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No está permitido modificar las condiciones históricas ni titulares de una contratación existente.",
        )

    if req.estado is None and req.fecha_vencimiento is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes indicar el estado y/o fecha_vencimiento a actualizar.",
        )

    db: Client = current_user["supabase"]
    memb_actual = _fetch_membresia_or_404(db, id_membresia_socio)
    staff_ctx, _ = _verify_staff_can_manage_socios(current_user, id_gimnasio=int(memb_actual["id_gimnasio"]))

    staff_db: Client = staff_ctx["supabase"]
    try:
        res_rpc = staff_db.rpc(
            "rpc_actualizar_estado_membresia",
            {
                "p_id_membresia_socio": id_membresia_socio,
                "p_estado": req.estado,
                "p_fecha_vencimiento": req.fecha_vencimiento,
            },
        ).execute()
        updated = get_data(res_rpc)
    except Exception as exc:
        raise HTTPException(
            status_code=_map_rpc_error_status(exc),
            detail=f"No fue posible actualizar la membresía: {exc}",
        ) from exc

    return {
        "mensaje": "Membresía actualizada exitosamente.",
        "membresia": updated,
        "membresia_socio": updated,
    }


@router.delete("/{id_membresia_socio}", status_code=status.HTTP_200_OK)
async def cancelar_membresia(
    id_membresia_socio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Cancela lógicamente (`estado = 'cancelada'`) una contratación mediante
    `public.rpc_actualizar_estado_membresia`, preservando todo el historial contable.
    """
    db: Client = current_user["supabase"]
    memb_actual = _fetch_membresia_or_404(db, id_membresia_socio)
    staff_ctx, _ = _verify_staff_can_manage_socios(current_user, id_gimnasio=int(memb_actual["id_gimnasio"]))

    staff_db: Client = staff_ctx["supabase"]
    try:
        res_rpc = staff_db.rpc(
            "rpc_actualizar_estado_membresia",
            {
                "p_id_membresia_socio": id_membresia_socio,
                "p_estado": "cancelada",
                "p_fecha_vencimiento": None,
            },
        ).execute()
        updated = get_data(res_rpc)
    except Exception as exc:
        raise HTTPException(
            status_code=_map_rpc_error_status(exc),
            detail=f"No fue posible cancelar la membresía: {exc}",
        ) from exc

    return {
        "mensaje": "Membresía cancelada exitosamente conservando el historial contable.",
        "cancelada": True,
        "membresia": updated,
        "membresia_socio": updated,
    }

