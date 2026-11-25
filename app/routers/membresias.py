from datetime import date, datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
from fastapi import APIRouter, HTTPException, status, Depends, Query, Path
from supabase import Client

from app.core.dependencies import get_current_user, run_db_parallel
from app.services.utils import get_data
from app.services.membresia_lifecycle import (
    ESTADOS_MEMBRESIA_EFECTIVOS,
    ESTADOS_VIGENCIA_BLOQUEANTES,
    buscar_conflicto_superposicion,
    calcular_estado_efectivo_membresia,
    calcular_inicio_encadenado,
    enriquecer_membresia_ciclo_vida,
    parse_iso_date,
)
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

ESTADOS_MEMBRESIA_VALIDOS = ESTADOS_MEMBRESIA_EFECTIVOS


def _fetch_contratos_asociacion(
    db: Client,
    id_socio_gimnasio: int,
    id_gimnasio: int,
) -> List[Dict[str, Any]]:
    res = (
        db.from_("membresia_socio")
        .select("id_membresia_socio, id_socio_gimnasio, id_gimnasio, fecha_inicio, fecha_vencimiento, estado")
        .eq("id_socio_gimnasio", id_socio_gimnasio)
        .eq("id_gimnasio", id_gimnasio)
        .execute()
    )
    rows = get_data(res)
    if not isinstance(rows, list):
        return []
    return [
        r for r in rows
        if isinstance(r, dict)
        and int(r.get("id_socio_gimnasio") or 0) == int(id_socio_gimnasio)
        and int(r.get("id_gimnasio") or 0) == int(id_gimnasio)
    ]


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


def _cancelar_membresias_activas_previas(
    db: Client,
    gym_id: int,
    id_socio_gimnasio: int,
    excluir_id_membresia: Optional[int] = None,
) -> List[int]:
    """
    Cancela lógicamente (`estado = 'cancelada'`) cualquier membresía activa previa del mismo
    `id_socio_gimnasio` en el gimnasio, conservando su historial intacto.
    """
    try:
        res = (
            db.from_("membresia_socio")
            .select("id_membresia_socio, estado")
            .eq("id_gimnasio", gym_id)
            .eq("id_socio_gimnasio", id_socio_gimnasio)
            .eq("estado", "activa")
            .execute()
        )
        rows = get_data(res) or []
    except Exception:
        return []

    canceladas: List[int] = []
    for row in (rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        mid = row.get("id_membresia_socio")
        if mid is None:
            continue
        mid_int = int(mid)
        if excluir_id_membresia is not None and mid_int == int(excluir_id_membresia):
            continue
        if str(row.get("estado") or "").strip().lower() != "activa":
            continue
        db.rpc(
            "rpc_actualizar_estado_membresia",
            {
                "p_id_membresia_socio": mid_int,
                "p_estado": "cancelada",
                "p_fecha_vencimiento": None,
            },
        ).execute()
        canceladas.append(mid_int)
    return canceladas


def _aprobar_o_registrar_pago_para_activar(
    db: Client,
    memb_actual: Dict[str, Any],
    metodo_pago: str = "efectivo",
    comprobante: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Asegura que existan pagos aprobados que cubran el importe acordado de la membresía antes de
    activarla, evitando el rechazo del trigger/RPC de Supabase cuando una membresía pendiente
    se pasa a 'activa' desde el panel administrativo.
    """
    memb_id = int(memb_actual["id_membresia_socio"])
    precio_contrato = round(float(memb_actual.get("precio") or 0), 2)
    if precio_contrato <= 0:
        return None

    res_pagos = (
        db.from_("pago")
        .select("id_pago, monto, estado")
        .eq("id_membresia_socio", memb_id)
        .execute()
    )
    pagos_list = get_data(res_pagos) or []
    if not isinstance(pagos_list, list):
        pagos_list = []

    total_aprobado = round(
        sum(
            float(p.get("monto") or 0)
            for p in pagos_list
            if isinstance(p, dict) and str(p.get("estado") or "").strip().lower() == "aprobado"
        ),
        2,
    )
    if total_aprobado >= precio_contrato:
        return None

    ultimo_pago = None
    # 1. Aprobar primero pagos pendientes existentes que cubran parte o la totalidad del saldo
    for p in pagos_list:
        if not isinstance(p, dict) or str(p.get("estado") or "").strip().lower() != "pendiente":
            continue
        monto_p = round(float(p.get("monto") or 0), 2)
        if monto_p > 0 and round(total_aprobado + monto_p, 2) <= precio_contrato:
            res_upd_p = db.rpc(
                "rpc_actualizar_estado_pago",
                {
                    "p_id_pago": int(p["id_pago"]),
                    "p_estado": "aprobado",
                },
            ).execute()
            ultimo_pago = get_data(res_upd_p)
            total_aprobado = round(total_aprobado + monto_p, 2)
            if total_aprobado >= precio_contrato:
                return ultimo_pago

    # 2. Si aún queda saldo pendiente, registrar un pago aprobado por la diferencia
    faltante = round(max(0.0, precio_contrato - total_aprobado), 2)
    if faltante > 0:
        res_reg = db.rpc(
            "rpc_registrar_pago",
            {
                "p_id_membresia_socio": memb_id,
                "p_monto": faltante,
                "p_metodo_pago": metodo_pago or "efectivo",
                "p_estado": "aprobado",
                "p_comprobante": comprobante.strip() if comprobante and comprobante.strip() else None,
                "p_fecha_pago": datetime.now(timezone.utc).isoformat(),
            },
        ).execute()
        ultimo_pago = get_data(res_reg)

    return ultimo_pago


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
            detail="Estado de membresía inválido. Permitidos: pendiente, programada, activa, vencida, cancelada.",
        )

    hoy = date.today()
    membresias = []
    for m in (rows if isinstance(rows, list) else []):
        if not isinstance(m, dict) or int(m.get("id_socio_gimnasio") or 0) not in set(target_sg_ids):
            continue
        enr = enriquecer_membresia_ciclo_vida(m, hoy=hoy)
        if estado_norm is not None and enr["estado_efectivo"] != estado_norm:
            continue
        membresias.append(enr)

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
            detail="Estado de membresía inválido. Permitidos: pendiente, programada, activa, vencida, cancelada.",
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
    hoy = date.today()
    membresias: List[Dict[str, Any]] = []
    socios_activos_set = set()
    for m in (rows if isinstance(rows, list) else []):
        if not isinstance(m, dict) or int(m.get("id_gimnasio") or 0) != gym_id:
            continue
        if id_socio is not None and _extract_socio_id_from_membresia(m) != id_socio:
            continue
        enr = enriquecer_membresia_ciclo_vida(m, hoy=hoy)
        if enr["estado_efectivo"] == "activa" and enr.get("id_socio_gimnasio") is not None:
            socios_activos_set.add(int(enr["id_socio_gimnasio"]))
        if estado_norm and enr["estado_efectivo"] != estado_norm:
            continue
        membresias.append(enr)

    return {
        "id_gimnasio": gym_id,
        "total": len(membresias),
        "socios_con_plan_activo": len(socios_activos_set),
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
    cancelar_activas_previas: bool = False,
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

    # Normalizar estado inicial en backend (el cliente nunca impone un estado incoherente con el pago)
    estado_rpc = "pendiente" if (estado == "pendiente" or (registrar_pago and estado_pago != "aprobado")) else "activa"

    # Encadenamiento y validación anti-solapamiento en backend (aplica tanto a 'activa' como a 'pendiente')
    hoy = date.today()
    sg_id = int(assoc["id_socio_gimnasio"])
    duracion_plan = max(1, int(plan.get("duracion_dias") or 30))

    # Si se solicita activar un nuevo plan reemplazando membresías activas previas, cancelarlas primero
    if cancelar_activas_previas and rol != "socio" and estado_rpc == "activa":
        try:
            _cancelar_membresias_activas_previas(actor_db, plan_gym_id, sg_id)
        except Exception as exc:
            raise HTTPException(
                status_code=_map_rpc_error_status(exc),
                detail=f"No fue posible cancelar la membresía activa previa del socio: {exc}",
            ) from exc

    contratos_previos = _fetch_contratos_asociacion(actor_db, sg_id, plan_gym_id)

    fecha_solicitada = parse_iso_date(fecha_inicio) or hoy
    fecha_inicio_efectiva, _max_venc = calcular_inicio_encadenado(
        contratos_previos, fecha_solicitada, hoy=hoy
    )
    fecha_venc_efectiva = fecha_inicio_efectiva + timedelta(days=duracion_plan)

    conflicto = buscar_conflicto_superposicion(
        contratos_previos,
        fecha_inicio_efectiva,
        fecha_venc_efectiva,
        hoy=hoy,
    )
    if conflicto is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"El período calculado ({fecha_inicio_efectiva.isoformat()} a {fecha_venc_efectiva.isoformat()}) "
                f"se superpone con la contratación vigente/programada #{conflicto.get('id_membresia_socio')} "
                f"({conflicto.get('fecha_inicio')} a {conflicto.get('fecha_vencimiento')})."
            ),
        )

    rpc_params = {
        "p_id_socio": target_socio_id,
        "p_id_membresia_gimnasio": id_membresia_gimnasio,
        "p_fecha_inicio": fecha_inicio_efectiva.isoformat(),
        "p_estado": estado_rpc,
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

    ms_res = resultado.get("membresia_socio")
    ms_enriquecida = enriquecer_membresia_ciclo_vida(ms_res, hoy=hoy) if isinstance(ms_res, dict) else ms_res

    return {
        "mensaje": (
            "Renovación de membresía registrada exitosamente (nueva contratación histórica creada)."
            if es_renovacion
            else "Membresía contratada exitosamente."
        ),
        "socio_gimnasio": resultado.get("socio_gimnasio"),
        "membresia": ms_enriquecida,
        "membresia_socio": ms_enriquecida,
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
        cancelar_activas_previas=req.cancelar_activas_previas,
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
        cancelar_activas_previas=req.cancelar_activas_previas,
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
    hoy = date.today()
    f_ini_actual = parse_iso_date(memb_actual.get("fecha_inicio"))
    f_venc_efectiva = parse_iso_date(req.fecha_vencimiento) or parse_iso_date(memb_actual.get("fecha_vencimiento"))
    estado_solicitado = req.estado if req.estado is not None else calcular_estado_efectivo_membresia(memb_actual, hoy=hoy)

    if f_ini_actual is not None and f_venc_efectiva is not None and f_venc_efectiva < f_ini_actual:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La fecha de vencimiento no puede ser anterior a la fecha de inicio.",
        )

    membresias_previas_canceladas: List[int] = []
    pago_generado: Optional[Dict[str, Any]] = None

    if estado_solicitado in ESTADOS_VIGENCIA_BLOQUEANTES:
        if f_venc_efectiva is not None and f_venc_efectiva < hoy:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No se puede marcar como activa o programada una contratación ya vencida.",
            )
        if req.cancelar_activas_previas and memb_actual.get("id_socio_gimnasio") is not None:
            try:
                membresias_previas_canceladas = _cancelar_membresias_activas_previas(
                    staff_db,
                    int(memb_actual["id_gimnasio"]),
                    int(memb_actual["id_socio_gimnasio"]),
                    excluir_id_membresia=id_membresia_socio,
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=_map_rpc_error_status(exc),
                    detail=f"No fue posible cancelar membresías activas previas del socio: {exc}",
                ) from exc

        if f_ini_actual is not None and f_venc_efectiva is not None:
            contratos_asoc = _fetch_contratos_asociacion(
                staff_db,
                int(memb_actual["id_socio_gimnasio"]),
                int(memb_actual["id_gimnasio"]),
            )
            conflicto = buscar_conflicto_superposicion(
                contratos_asoc,
                f_ini_actual,
                f_venc_efectiva,
                excluir_id_membresia=id_membresia_socio,
                hoy=hoy,
            )
            if conflicto is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"No se puede actualizar la contratación #{id_membresia_socio} porque su período "
                        f"({f_ini_actual.isoformat()} a {f_venc_efectiva.isoformat()}) se superpone con la contratación "
                        f"#{conflicto.get('id_membresia_socio')} ({conflicto.get('fecha_inicio')} a {conflicto.get('fecha_vencimiento')})."
                    ),
                )

    # Compatibilidad con el CHECK actual de BD si todavía no se aplicó la migración de 'programada'
    estado_para_rpc = "activa" if req.estado == "programada" else req.estado
    try:
        # Si pasa de pendiente a activa con sincronización de pago, aprobar/registrar el pago antes de activar
        if (
            estado_para_rpc == "activa"
            and req.registrar_pago_si_falta
            and str(memb_actual.get("estado") or "").strip().lower() == "pendiente"
        ):
            pago_generado = _aprobar_o_registrar_pago_para_activar(
                staff_db,
                memb_actual,
                metodo_pago=req.metodo_pago,
                comprobante=req.comprobante,
            )

        res_rpc = staff_db.rpc(
            "rpc_actualizar_estado_membresia",
            {
                "p_id_membresia_socio": id_membresia_socio,
                "p_estado": estado_para_rpc,
                "p_fecha_vencimiento": req.fecha_vencimiento,
            },
        ).execute()
        updated = get_data(res_rpc)
    except Exception as exc:
        raise HTTPException(
            status_code=_map_rpc_error_status(exc),
            detail=f"No fue posible actualizar la membresía: {exc}",
        ) from exc

    updated_enr = enriquecer_membresia_ciclo_vida(updated, hoy=hoy) if isinstance(updated, dict) else updated

    return {
        "mensaje": "Membresía actualizada exitosamente.",
        "membresia": updated_enr,
        "membresia_socio": updated_enr,
        "pago": pago_generado,
        "membresias_previas_canceladas": membresias_previas_canceladas,
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

