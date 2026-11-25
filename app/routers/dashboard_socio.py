import time
from datetime import date
from typing import Dict, Any, Optional, List
from fastapi import APIRouter, HTTPException, status, Depends, Query, Request
from supabase import Client

from app.core.dependencies import get_current_user, run_db_parallel
from app.services.utils import (
    formatear_precio_ars,
    calcular_metricas_membresia,
    get_data,
    sugerir_beneficios,
)
from app.services.payment_service import crear_preferencia_mercadopago
from app.models.dashboard_socio import (
    CheckinSocioRequest,
    SolicitarMembresiaSocio,
    IniciarPagoSocio,
)
from app.routers.socios import (
    _resolve_socio_profile_context,
    _fetch_socio_gimnasios_for_socio,
)
from app.routers.planes_membresia import PLAN_COLUMNS
from app.routers.membresias import (
    MEMBRESIA_SOCIO_SELECT,
    _fetch_contratos_asociacion,
    _map_rpc_error_status,
)
from app.services.membresia_lifecycle import (
    buscar_conflicto_superposicion,
    calcular_inicio_encadenado,
    enriquecer_membresia_ciclo_vida,
    parse_iso_date,
    seleccionar_membresia_vigente_y_programada,
)
from app.routers.pagos import (
    PAGO_SELECT,
    _extract_socio_id_from_pago,
    _extract_gym_id_from_pago,
)
from app.routers.asistencias import (
    ASISTENCIA_SELECT,
    _extract_gym_id_from_asistencia,
    _map_asistencia_rpc_error,
)
from app.routers.rutinas import (
    RUTINA_SOCIO_SELECT,
    ASIGNACION_SELECT,
    _sort_rutina_items,
)

router = APIRouter(prefix="/api/socio", tags=["Panel Socio"])


def _select_active_gym_for_socio(
    asociaciones: List[Dict[str, Any]],
    requested_gym_id: Optional[int],
) -> Optional[Dict[str, Any]]:
    by_gym = {
        int(a["id_gimnasio"]): a
        for a in asociaciones
        if isinstance(a, dict) and a.get("id_gimnasio") is not None
    }
    if requested_gym_id is not None:
        if requested_gym_id not in by_gym:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No posees asociación con el gimnasio solicitado.",
            )
        return by_gym[requested_gym_id]
    activas = [a for a in asociaciones if isinstance(a, dict) and a.get("estado") is True]
    if activas:
        return activas[0]
    return asociaciones[0] if asociaciones else None


# ==============================================================================
# WEBHOOK EXTERNO (RECEPTOR DE NOTIFICACIONES SIN SERVICE_ROLE)
# ==============================================================================

@router.post("/pagos/webhook", status_code=status.HTTP_200_OK)
async def webhook_pago(request: Request, proveedor: Optional[str] = Query("mercadopago")):
    """Receptor de notificaciones de pasarela (sin acreditación automática hasta validar firma HMAC)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    return {
        "recibido": True,
        "proveedor": proveedor,
        "requiere_verificacion_firma": True,
        "evento_detectado": bool(body or dict(request.query_params)),
    }


# ==============================================================================
# DASHBOARD GENERAL DEL SOCIO (MULTI-GIMNASIO, SIN TABLA `progreso`)
# ==============================================================================

@router.get("/dashboard", status_code=status.HTTP_200_OK)
async def get_dashboard_socio(
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Panel integral del socio autenticado adaptado al modelo definitivo de Supabase:
    - Identidad global del socio (`public.socio`, sin `id_gimnasio`).
    - Asociaciones multi-gimnasio (`public.socio_gimnasio`).
    - Membresía vigente (`public.membresia_socio` + `public.membresia_gimnasio`).
    - Último pago (`public.pago`).
    - Asistencias (`public.asistencia_socio` con sucursal, excluyendo anuladas).
    - Rutinas separadas: personales (`public.rutina_socio`) y asignadas (`public.rutinas_asignadas_socio_gimnasio`).
    - Sin tabla ni bloque `progreso` (eliminada del esquema definitivo).
    """
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]
    access_token = socio_ctx.get("access_token")

    req_gym = id_gimnasio or current_user.get("requested_gym_id")

    # Fase 1 en paralelo: perfil socio, asociaciones multi-gimnasio, pagos, asistencias y rutinas personales
    def _q_socio(client: Client):
        return get_data(
            client.from_("socio")
            .select("id_socio, dni, telefono, fecha_alta, estado, nombre, apellido, email")
            .eq("id_socio", own_socio_id)
            .maybe_single()
            .execute()
        )

    def _q_asoc(client: Client):
        return _fetch_socio_gimnasios_for_socio(client, own_socio_id)

    def _q_pagos(client: Client):
        return get_data(client.from_("pago").select(PAGO_SELECT).order("fecha_pago", desc=True).execute()) or []

    def _q_asist(client: Client):
        return get_data(
            client.from_("asistencia_socio")
            .select(ASISTENCIA_SELECT)
            .eq("id_socio", own_socio_id)
            .order("hora_ingreso", desc=True)
            .execute()
        ) or []

    def _q_rp(client: Client):
        return get_data(
            client.from_("rutina_socio")
            .select(RUTINA_SOCIO_SELECT)
            .eq("id_socio", own_socio_id)
            .order("id_rutina_socio", desc=True)
            .execute()
        ) or []

    socio, asociaciones, pagos_raw, asist_raw, rp_raw = await run_db_parallel(
        db,
        access_token,
        [_q_socio, _q_asoc, _q_pagos, _q_asist, _q_rp],
    )

    if not isinstance(socio, dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No se encontraron datos del socio en el sistema.",
        )

    assoc_activa = _select_active_gym_for_socio(asociaciones, int(req_gym) if req_gym else None)
    selected_gym_id = int(assoc_activa["id_gimnasio"]) if assoc_activa else None
    selected_sg_id = int(assoc_activa["id_socio_gimnasio"]) if assoc_activa else None
    gimnasio_activo = assoc_activa.get("gimnasio") if isinstance(assoc_activa, dict) else None

    # Fase 2 en paralelo (dependiente de selected_sg_id): membresía vigente y rutinas asignadas
    membresia_actual: Optional[Dict[str, Any]] = None
    membresia_programada: Optional[Dict[str, Any]] = None
    rutinas_asignadas: List[Dict[str, Any]] = []
    if selected_sg_id is not None:
        def _q_memb(client: Client):
            return get_data(
                client.from_("membresia_socio")
                .select(MEMBRESIA_SOCIO_SELECT)
                .eq("id_socio_gimnasio", selected_sg_id)
                .order("fecha_vencimiento", desc=True)
                .execute()
            ) or []

        def _q_ra(client: Client):
            return get_data(
                client.from_("rutinas_asignadas_socio_gimnasio")
                .select(ASIGNACION_SELECT)
                .eq("id_socio_gimnasio", selected_sg_id)
                .order("id_rutina_asignada", desc=True)
                .execute()
            ) or []

        raw_m_list, raw_ra_list = await run_db_parallel(db, access_token, [_q_memb, _q_ra])
        raw_m = [
            m for m in (raw_m_list if isinstance(raw_m_list, list) else [])
            if isinstance(m, dict) and int(m.get("id_socio_gimnasio") or 0) == selected_sg_id
        ]
        membresia_actual, membresia_programada = seleccionar_membresia_vigente_y_programada(raw_m)
        rutinas_asignadas = [
            a for a in (raw_ra_list if isinstance(raw_ra_list, list) else [])
            if isinstance(a, dict)
            and int(a.get("id_socio_gimnasio") or 0) == selected_sg_id
            and a.get("estado") is True
        ]

    metricas_membresia = calcular_metricas_membresia(membresia_actual)

    # 2. Último pago del socio (en el gimnasio seleccionado si existe)
    pagos_propios = [
        p for p in (pagos_raw if isinstance(pagos_raw, list) else [])
        if isinstance(p, dict)
        and _extract_socio_id_from_pago(p) == own_socio_id
        and (selected_gym_id is None or _extract_gym_id_from_pago(p) == selected_gym_id)
    ]
    ultimo_pago = pagos_propios[0] if pagos_propios else None

    # 3. Asistencias del socio (`public.asistencia_socio`, excluyendo anuladas)
    hoy = date.today()
    inicio_mes = date(hoy.year, hoy.month, 1).isoformat()
    asist_validas = [
        a for a in (asist_raw if isinstance(asist_raw, list) else [])
        if isinstance(a, dict)
        and int(a.get("id_socio") or 0) == own_socio_id
        and str(a.get("estado") or "").lower() != "anulada"
        and (selected_gym_id is None or _extract_gym_id_from_asistencia(a) == selected_gym_id)
    ]
    asistencias_mes = sum(
        1 for a in asist_validas if str(a.get("hora_ingreso") or "").split("T")[0] >= inicio_mes
    )

    # 4. Rutinas personales (`public.rutina_socio`)
    rutinas_personales = [
        _sort_rutina_items(r, "rutina_ejercicio_socio")
        for r in (rp_raw if isinstance(rp_raw, list) else [])
        if isinstance(r, dict) and int(r.get("id_socio") or 0) == own_socio_id and r.get("estado") is True
    ]

    plan_obj = (
        membresia_actual.get("membresia_gimnasio")
        if isinstance(membresia_actual, dict) and isinstance(membresia_actual.get("membresia_gimnasio"), dict)
        else {}
    )
    membresia_payload = (
        {
            "id_membresia_socio": membresia_actual["id_membresia_socio"],
            "id_membresia_gimnasio": membresia_actual.get("id_membresia_gimnasio"),
            "id_gimnasio": membresia_actual.get("id_gimnasio"),
            "plan": plan_obj.get("nombre") or "Plan Contratado",
            "estado": metricas_membresia["estado_clave"],
            "estado_etiqueta": metricas_membresia["estado_etiqueta"],
            "fecha_inicio": membresia_actual.get("fecha_inicio"),
            "fecha_vencimiento": membresia_actual.get("fecha_vencimiento"),
            "precio": float(membresia_actual.get("precio") or 0),
            "dias_restantes": metricas_membresia["dias_restantes"],
            "porcentaje_transcurrido": metricas_membresia["porcentaje_transcurrido"],
            "expirada": metricas_membresia["esta_vencida"],
            "proxima_programada": (
                {
                    "id_membresia_socio": membresia_programada["id_membresia_socio"],
                    "plan": (membresia_programada.get("membresia_gimnasio") or {}).get("nombre") or "Plan Programado",
                    "fecha_inicio": membresia_programada.get("fecha_inicio"),
                    "fecha_vencimiento": membresia_programada.get("fecha_vencimiento"),
                    "estado": "programada",
                }
                if membresia_programada
                else None
            ),
        }
        if membresia_actual
        else {
            "id_membresia_socio": None,
            "estado": "sin_membresia",
            "plan": "Sin Membresía Activa",
            "dias_restantes": 0,
            "porcentaje_transcurrido": 0,
            "expirada": True,
        }
    )

    return {
        "socio": socio,
        "gimnasio_activo": gimnasio_activo,
        "gimnasios_asociados": asociaciones,
        "membresia": membresia_payload,
        "ultimo_pago": ultimo_pago,
        "entrenamientos": {
            "total_asistencias": len(asist_validas),
            "asistencias_mes": asistencias_mes,
            "recientes": asist_validas[:5],
        },
        "rutinas_personales": rutinas_personales,
        "rutinas_asignadas": rutinas_asignadas,
    }


@router.post("/checkin", status_code=status.HTTP_201_CREATED)
async def registrar_checkin_socio(
    req: CheckinSocioRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Registra el ingreso del socio autenticado en una sucursal específica utilizando
    exclusivamente `public.rpc_registrar_ingreso_asistencia`.
    """
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]

    try:
        res_rpc = db.rpc(
            "rpc_registrar_ingreso_asistencia",
            {
                "p_id_sucursal": req.id_sucursal,
                "p_id_socio": own_socio_id,
            },
        ).execute()
        asistencia = get_data(res_rpc)
    except Exception as exc:
        raise HTTPException(
            status_code=_map_asistencia_rpc_error(exc),
            detail=f"No fue posible registrar el check-in: {exc}",
        ) from exc

    return {
        "mensaje": "Asistencia registrada con éxito.",
        "asistencia": asistencia,
    }


# ==============================================================================
# MEMBRESÍA Y PLANES DEL SOCIO (SOLO ESQUEMA DEFINITIVO, SIN AUTOSEMBRADO)
# ==============================================================================

@router.get("/membresia", status_code=status.HTTP_200_OK)
async def get_mi_membresia(
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Devuelve la membresía vigente, historial de pagos y planes activos del gimnasio asociado."""
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]

    asociaciones = _fetch_socio_gimnasios_for_socio(db, own_socio_id)
    assoc = _select_active_gym_for_socio(asociaciones, id_gimnasio)
    if not assoc:
        return {
            "id_socio": own_socio_id,
            "membresia": None,
            "metricas": calcular_metricas_membresia(None),
            "pagos": [],
            "planes_disponibles": [],
        }

    gym_id = int(assoc["id_gimnasio"])
    sg_id = int(assoc["id_socio_gimnasio"])

    res_m = (
        db.from_("membresia_socio")
        .select(MEMBRESIA_SOCIO_SELECT)
        .eq("id_socio_gimnasio", sg_id)
        .order("fecha_vencimiento", desc=True)
        .execute()
    )
    raw_m = [
        m for m in (get_data(res_m) or [])
        if isinstance(m, dict) and int(m.get("id_socio_gimnasio") or 0) == sg_id
    ]
    membresia, membresia_programada = seleccionar_membresia_vigente_y_programada(raw_m)
    historial_membresias = [enriquecer_membresia_ciclo_vida(m) for m in raw_m]

    res_pagos = db.from_("pago").select(PAGO_SELECT).order("fecha_pago", desc=True).execute()
    pagos = [
        p for p in (get_data(res_pagos) or [])
        if isinstance(p, dict)
        and _extract_socio_id_from_pago(p) == own_socio_id
        and _extract_gym_id_from_pago(p) == gym_id
    ][:10]

    res_pl = (
        db.from_("membresia_gimnasio")
        .select(PLAN_COLUMNS)
        .eq("id_gimnasio", gym_id)
        .order("precio", desc=False)
        .execute()
    )
    planes = [
        p for p in (get_data(res_pl) or [])
        if isinstance(p, dict) and int(p.get("id_gimnasio") or 0) == gym_id and p.get("estado") is True
    ]

    metricas = calcular_metricas_membresia(membresia)
    membresia_resp = None
    if membresia:
        plan_obj = membresia.get("membresia_gimnasio") if isinstance(membresia.get("membresia_gimnasio"), dict) else {}
        precio_monto = float(membresia.get("precio") or 0)
        membresia_resp = {
            "tiene_membresia": metricas["tiene_membresia"],
            "id_membresia_socio": membresia["id_membresia_socio"],
            "id_membresia_gimnasio": membresia.get("id_membresia_gimnasio"),
            "id_gimnasio": gym_id,
            "nombre_plan": plan_obj.get("nombre", "Plan Contratado"),
            "precio": precio_monto,
            "precio_formateado": formatear_precio_ars(precio_monto),
            "fecha_inicio": membresia.get("fecha_inicio"),
            "fecha_vencimiento": membresia.get("fecha_vencimiento"),
            "fecha_inicio_formateada": metricas["fecha_inicio_formateada"],
            "fecha_vencimiento_formateada": metricas["fecha_vencimiento_formateada"],
            "dias_restantes": metricas["dias_restantes"],
            "tiempo_restante_texto": metricas["tiempo_restante_texto"],
            "porcentaje_transcurrido": metricas["porcentaje_transcurrido"],
            "estado": metricas["estado_clave"],
            "estado_etiqueta": metricas["estado_etiqueta"],
            "esta_vencida": metricas["esta_vencida"],
        }

    membresia_programada_resp = None
    if membresia_programada:
        plan_prog_obj = (
            membresia_programada.get("membresia_gimnasio")
            if isinstance(membresia_programada.get("membresia_gimnasio"), dict)
            else {}
        )
        met_prog = calcular_metricas_membresia(membresia_programada)
        membresia_programada_resp = {
            "id_membresia_socio": membresia_programada["id_membresia_socio"],
            "id_membresia_gimnasio": membresia_programada.get("id_membresia_gimnasio"),
            "nombre_plan": plan_prog_obj.get("nombre", "Renovación Programada"),
            "precio": float(membresia_programada.get("precio") or 0),
            "precio_formateado": formatear_precio_ars(membresia_programada.get("precio") or 0),
            "fecha_inicio": membresia_programada.get("fecha_inicio"),
            "fecha_vencimiento": membresia_programada.get("fecha_vencimiento"),
            "fecha_inicio_formateada": met_prog["fecha_inicio_formateada"],
            "fecha_vencimiento_formateada": met_prog["fecha_vencimiento_formateada"],
            "estado": "programada",
            "estado_etiqueta": "Programada",
            "tiempo_restante_texto": met_prog["tiempo_restante_texto"],
        }

    return {
        "id_socio": own_socio_id,
        "id_gimnasio": gym_id,
        "membresia": membresia_resp,
        "membresia_programada": membresia_programada_resp,
        "historial_membresias": historial_membresias,
        "metricas": metricas,
        "pagos": pagos,
        "planes_disponibles": planes,
    }


@router.get("/planes-disponibles", status_code=status.HTTP_200_OK)
async def get_planes_disponibles(
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Lista los planes activos (`public.membresia_gimnasio`) del gimnasio asociado al socio (sin autosembrado)."""
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]

    asociaciones = _fetch_socio_gimnasios_for_socio(db, own_socio_id)
    assoc = _select_active_gym_for_socio(asociaciones, id_gimnasio)
    if not assoc or assoc.get("estado") is not True:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Necesitas una asociación activa en el gimnasio para consultar sus planes.",
        )

    gym_id = int(assoc["id_gimnasio"])
    sg_id = int(assoc["id_socio_gimnasio"])

    res_m = (
        db.from_("membresia_socio")
        .select("id_membresia_socio, id_membresia_gimnasio, precio, estado, fecha_inicio, fecha_vencimiento")
        .eq("id_socio_gimnasio", sg_id)
        .order("fecha_vencimiento", desc=True)
        .execute()
    )
    raw_m = [m for m in (get_data(res_m) or []) if isinstance(m, dict)]
    memb_actual, _ = seleccionar_membresia_vigente_y_programada(raw_m)
    if memb_actual and memb_actual.get("estado_efectivo") != "activa":
        memb_actual = None
    plan_actual_id = memb_actual.get("id_membresia_gimnasio") if memb_actual else None
    precio_actual = float(memb_actual.get("precio") or 0) if memb_actual else 0.0

    res_pl = (
        db.from_("membresia_gimnasio")
        .select(PLAN_COLUMNS)
        .eq("id_gimnasio", gym_id)
        .order("precio", desc=False)
        .execute()
    )
    planes_activos = [
        p for p in (get_data(res_pl) or [])
        if isinstance(p, dict) and int(p.get("id_gimnasio") or 0) == gym_id and p.get("estado") is True
    ]

    planes_formateados = []
    for plan in planes_activos:
        precio_num = float(plan.get("precio") or 0)
        duracion = int(plan.get("duracion_dias") or 30)
        es_plan_actual = plan.get("id_membresia_gimnasio") == plan_actual_id
        diferencia = precio_num - precio_actual
        planes_formateados.append(
            {
                "id_membresia_gimnasio": plan.get("id_membresia_gimnasio"),
                "id_gimnasio": gym_id,
                "nombre": plan.get("nombre"),
                "descripcion": plan.get("descripcion"),
                "precio": precio_num,
                "precio_formateado": formatear_precio_ars(precio_num),
                "duracion_dias": duracion,
                "duracion_texto": f"{duracion} días",
                "limite_asistencias_semanales": plan.get("limite_asistencias_semanales"),
                "limite_asistencias_totales": plan.get("limite_asistencias_totales"),
                "estado": True,
                "es_plan_actual": es_plan_actual,
                "es_superior": (not es_plan_actual) and (precio_num > precio_actual),
                "diferencia_precio": diferencia if diferencia > 0 else 0,
                "diferencia_precio_formateada": formatear_precio_ars(diferencia) if diferencia > 0 else None,
                "beneficios": sugerir_beneficios(plan.get("nombre")),
            }
        )

    return {
        "id_gimnasio": gym_id,
        "plan_actual_id": plan_actual_id,
        "total_planes": len(planes_formateados),
        "planes": planes_formateados,
    }


@router.post("/membresia/solicitar", status_code=status.HTTP_201_CREATED)
@router.post("/membresia/actualizar", status_code=status.HTTP_201_CREATED)
async def solicitar_contratacion_pendiente(
    req: SolicitarMembresiaSocio,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Genera una solicitud de contratación en estado `'pendiente'` mediante `public.rpc_contratar_membresia`.
    Nunca sobrescribe contratos anteriores ni activa membresías sin acreditación de pago.
    """
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]

    res_plan = (
        db.from_("membresia_gimnasio")
        .select(PLAN_COLUMNS)
        .eq("id_membresia_gimnasio", req.id_membresia_gimnasio)
        .maybe_single()
        .execute()
    )
    plan = get_data(res_plan)
    if not isinstance(plan, dict) or plan.get("estado") is not True:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El plan seleccionado no existe o se encuentra inactivo.",
        )

    plan_gym_id = int(plan["id_gimnasio"])
    asocs = _fetch_socio_gimnasios_for_socio(db, own_socio_id)
    assoc = next(
        (a for a in asocs if int(a.get("id_gimnasio") or 0) == plan_gym_id and a.get("estado") is True),
        None,
    )
    if not assoc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No posees una asociación activa en el gimnasio de este plan.",
        )

    from datetime import timedelta

    hoy = date.today()
    sg_id = int(assoc["id_socio_gimnasio"])
    duracion_plan = max(1, int(plan.get("duracion_dias") or 30))
    contratos_previos = _fetch_contratos_asociacion(db, sg_id, plan_gym_id)
    fecha_solicitada = parse_iso_date(req.fecha_inicio) or hoy
    fecha_inicio_efectiva, _ = calcular_inicio_encadenado(contratos_previos, fecha_solicitada, hoy=hoy)
    fecha_venc_efectiva = fecha_inicio_efectiva + timedelta(days=duracion_plan)

    conflicto = buscar_conflicto_superposicion(
        contratos_previos, fecha_inicio_efectiva, fecha_venc_efectiva, hoy=hoy
    )
    if conflicto is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"La solicitud ({fecha_inicio_efectiva.isoformat()} a {fecha_venc_efectiva.isoformat()}) "
                f"se superpone con tu contratación vigente/programada #{conflicto.get('id_membresia_socio')}."
            ),
        )

    try:
        res_rpc = db.rpc(
            "rpc_contratar_membresia",
            {
                "p_id_socio": own_socio_id,
                "p_id_membresia_gimnasio": req.id_membresia_gimnasio,
                "p_fecha_inicio": fecha_inicio_efectiva.isoformat(),
                "p_estado": "pendiente",
                "p_registrar_pago": False,
                "p_metodo_pago": "efectivo",
                "p_estado_pago": "pendiente",
                "p_comprobante": None,
                "p_monto_override": None,
            },
        ).execute()
        resultado = get_data(res_rpc)
    except Exception as exc:
        raise HTTPException(
            status_code=_map_rpc_error_status(exc),
            detail=f"No fue posible registrar la solicitud de membresía: {exc}",
        ) from exc

    return {
        "mensaje": "Solicitud de membresía registrada en estado pendiente hasta la acreditación del pago.",
        "membresia_socio": resultado.get("membresia_socio") if isinstance(resultado, dict) else resultado,
    }


@router.post("/pagos/iniciar", status_code=status.HTTP_200_OK)
async def iniciar_pago(
    req: IniciarPagoSocio,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Prepara una sesión de pago digital (Mercado Pago) usando exclusivamente el precio real del plan."""
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]

    res_plan = (
        db.from_("membresia_gimnasio")
        .select(PLAN_COLUMNS)
        .eq("id_membresia_gimnasio", req.id_membresia_gimnasio)
        .maybe_single()
        .execute()
    )
    plan = get_data(res_plan)
    if not isinstance(plan, dict):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan de membresía no encontrado.")
    if plan.get("estado") is not True:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El plan seleccionado está inactivo y no admite nuevos pagos.",
        )

    plan_gym_id = int(plan["id_gimnasio"])
    asocs = _fetch_socio_gimnasios_for_socio(db, own_socio_id)
    assoc = next(
        (a for a in asocs if int(a.get("id_gimnasio") or 0) == plan_gym_id and a.get("estado") is True),
        None,
    )
    if not assoc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El plan seleccionado no pertenece a un gimnasio en el que tengas asociación activa.",
        )

    res_socio = (
        db.from_("socio")
        .select("id_socio, nombre, apellido, dni, email, telefono")
        .eq("id_socio", own_socio_id)
        .maybe_single()
        .execute()
    )
    socio = get_data(res_socio) or {"id_socio": own_socio_id}
    gym = assoc.get("gimnasio") if isinstance(assoc.get("gimnasio"), dict) else {"id_gimnasio": plan_gym_id}

    monto_real = round(float(plan.get("precio") or 0), 2)
    external_ref = f"GYM-{plan_gym_id}-SOC-{own_socio_id}-PL-{req.id_membresia_gimnasio}-{int(time.time())}"

    orden = await crear_preferencia_mercadopago(
        titulo=plan.get("nombre", "Plan GymTrack"),
        monto=monto_real,
        external_reference=external_ref,
        socio=socio,
        gimnasio=gym,
    )

    return {
        "orden": orden,
        "plan": plan,
        "socio": socio,
    }
