from datetime import date, datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
from fastapi import APIRouter, HTTPException, status, Depends, Query, Path
from supabase import Client

from app.core.dependencies import get_current_user, run_db_parallel
from app.services.utils import get_data
from app.models.pago import PagoCreate, PagoUpdateEstado
from app.routers.socios import (
    _verify_staff_can_manage_socios,
    _resolve_socio_profile_context,
    _fetch_socio_gimnasios_for_gym,
    _fetch_socio_gimnasios_for_socio,
)
from app.routers.membresias import (
    MEMBRESIA_SOCIO_SELECT,
    _cancelar_activas_y_calcular_dias_restantes,
    _fetch_membresia_or_404,
    _extract_socio_id_from_membresia,
    _map_rpc_error_status,
)
from app.services.membresia_lifecycle import (
    enriquecer_membresia_ciclo_vida,
    parse_iso_date,
    seleccionar_membresia_vigente_y_programada,
)
from app.routers.planes_membresia import PLAN_COLUMNS

router = APIRouter(prefix="/api/pagos", tags=["Pagos"])

PAGO_SELECT = (
    "id_pago, fecha_pago, monto, metodo_pago, estado, comprobante, id_membresia_socio, "
    "membresia_socio(id_membresia_socio, id_socio_gimnasio, id_membresia_gimnasio, id_gimnasio, "
    "precio, fecha_inicio, fecha_vencimiento, estado, "
    "socio_gimnasio(id_socio_gimnasio, id_socio, id_gimnasio, estado, "
    "socio(id_socio, nombre, apellido, dni, email)), "
    "membresia_gimnasio(id_membresia_gimnasio, nombre, precio, duracion_dias))"
)

ESTADOS_PAGO_VALIDOS = {"pendiente", "aprobado", "rechazado", "anulado"}


def _verify_staff_can_access_pagos(
    current_user: Dict[str, Any],
    id_gimnasio: Optional[int] = None,
) -> tuple[Dict[str, Any], int]:
    """
    Valida permisos financieros sobre `public.pago` en correspondencia con la política RLS
    `pago_select_authorized`, `rpc_registrar_pago` y `rpc_actualizar_estado_pago`:
      - Dueño del gimnasio.
      - Administrador organizacional activo del gimnasio.
      - Empleado operativo con rol activo `'administrador'` o `'recepcionista'` en el gimnasio.
    Rechaza estrictamente a `entrenador`, `socio` y personal de otros gimnasios con `403 Forbidden`.
    """
    return _verify_staff_can_manage_socios(current_user, id_gimnasio=id_gimnasio)


def _fetch_pago_or_404(db: Client, id_pago: int) -> Dict[str, Any]:
    res = (
        db.from_("pago")
        .select(PAGO_SELECT)
        .eq("id_pago", id_pago)
        .maybe_single()
        .execute()
    )
    pago = get_data(res)
    if not isinstance(pago, dict):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pago no encontrado.")
    return pago


def _extract_gym_id_from_pago(pago: Dict[str, Any]) -> Optional[int]:
    ms = pago.get("membresia_socio")
    if isinstance(ms, dict) and ms.get("id_gimnasio") is not None:
        return int(ms["id_gimnasio"])
    return None


def _extract_socio_id_from_pago(pago: Dict[str, Any]) -> Optional[int]:
    ms = pago.get("membresia_socio")
    if isinstance(ms, dict):
        return _extract_socio_id_from_membresia(ms)
    return None


def _filter_pagos_by_gym_and_params(
    rows: List[Dict[str, Any]],
    gym_id: int,
    estado: Optional[str] = None,
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    id_membresia_socio: Optional[int] = None,
) -> List[Dict[str, Any]]:
    estado_norm = estado.strip().lower() if estado and estado.strip().lower() != "todos" else None
    if estado_norm and estado_norm not in ESTADOS_PAGO_VALIDOS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Estado de pago inválido. Permitidos: pendiente, aprobado, rechazado, anulado.",
        )

    filtrados: List[Dict[str, Any]] = []
    for p in rows:
        if not isinstance(p, dict):
            continue
        if _extract_gym_id_from_pago(p) != gym_id:
            continue
        if id_membresia_socio is not None and int(p.get("id_membresia_socio") or 0) != id_membresia_socio:
            continue
        if estado_norm and str(p.get("estado") or "").strip().lower() != estado_norm:
            continue
        f_pago = str(p.get("fecha_pago") or "")
        if desde and f_pago and f_pago < desde:
            continue
        if hasta and f_pago and f_pago > hasta:
            continue
        filtrados.append(p)
    return filtrados


@router.get("/mis-pagos", status_code=status.HTTP_200_OK)
async def get_mis_pagos(
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Permite al socio autenticado consultar exclusivamente su propio historial de pagos."""
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]

    asociaciones = _fetch_socio_gimnasios_for_socio(db, own_socio_id)
    allowed_gym_ids = {int(a["id_gimnasio"]) for a in asociaciones if a.get("id_gimnasio") is not None}
    if id_gimnasio is not None and id_gimnasio not in allowed_gym_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No posees asociación con el gimnasio solicitado.",
        )

    res = db.from_("pago").select(PAGO_SELECT).order("fecha_pago", desc=True).execute()
    rows = get_data(res)
    propios = [
        p for p in (rows if isinstance(rows, list) else [])
        if isinstance(p, dict)
        and _extract_socio_id_from_pago(p) == own_socio_id
        and (id_gimnasio is None or _extract_gym_id_from_pago(p) == id_gimnasio)
    ]
    return {
        "id_socio": own_socio_id,
        "total": len(propios),
        "datos": propios,
    }


@router.get("/resumen", status_code=status.HTTP_200_OK)
async def get_resumen_pagos(
    desde: Optional[str] = Query(default=None),
    hasta: Optional[str] = Query(default=None),
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Obtiene métricas financieras consolidadas de `public.pago` para el gimnasio autorizado
    usando los estados definitivos (`aprobado`, `pendiente`, `rechazado`, `anulado`).
    """
    staff_ctx, gym_id = _verify_staff_can_access_pagos(current_user, id_gimnasio=id_gimnasio)
    db: Client = staff_ctx["supabase"]
    access_token = staff_ctx.get("access_token")

    [rows] = await run_db_parallel(
        db,
        access_token,
        [lambda c: get_data(c.from_("pago").select(PAGO_SELECT).order("fecha_pago", desc=True).execute())],
    )
    pagos = _filter_pagos_by_gym_and_params(
        rows if isinstance(rows, list) else [],
        gym_id=gym_id,
        desde=desde,
        hasta=hasta,
    )

    resumen = {
        "id_gimnasio": gym_id,
        "total_recaudado": 0.0,
        "pagos_aprobados": 0,
        "pagos_pendientes": 0,
        "pagos_rechazados": 0,
        "pagos_anulados": 0,
    }
    for p in pagos:
        est = str(p.get("estado") or "").strip().lower()
        if est == "aprobado":
            resumen["total_recaudado"] += float(p.get("monto") or 0)
            resumen["pagos_aprobados"] += 1
        elif est == "pendiente":
            resumen["pagos_pendientes"] += 1
        elif est == "rechazado":
            resumen["pagos_rechazados"] += 1
        elif est == "anulado":
            resumen["pagos_anulados"] += 1

    resumen["total_recaudado"] = round(resumen["total_recaudado"], 2)
    return resumen


@router.get("/membresias", status_code=status.HTTP_200_OK)
async def get_pagos_membresias(
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Lista socios asociados (`socio_gimnasio`) y sus contrataciones (`membresia_socio`) para cobro."""
    staff_ctx, gym_id = _verify_staff_can_access_pagos(current_user, id_gimnasio=id_gimnasio)
    db: Client = staff_ctx["supabase"]
    access_token = staff_ctx.get("access_token")

    asociaciones, membs = await run_db_parallel(
        db,
        access_token,
        [
            lambda c: _fetch_socio_gimnasios_for_gym(c, gym_id),
            lambda c: get_data(
                c.from_("membresia_socio")
                .select(MEMBRESIA_SOCIO_SELECT)
                .eq("id_gimnasio", gym_id)
                .order("fecha_vencimiento", desc=True)
                .execute()
            ),
        ],
    )
    membs_by_sg: Dict[int, List[Dict[str, Any]]] = {}
    for m in (membs if isinstance(membs, list) else []):
        if isinstance(m, dict) and int(m.get("id_gimnasio") or 0) == gym_id:
            sg_id = int(m.get("id_socio_gimnasio") or 0)
            membs_by_sg.setdefault(sg_id, []).append(m)

    resultado = []
    for sg in asociaciones:
        sg_id = int(sg.get("id_socio_gimnasio") or 0)
        socio_obj = sg.get("socio") if isinstance(sg.get("socio"), dict) else {}
        m_list = [enriquecer_membresia_ciclo_vida(m) for m in membs_by_sg.get(sg_id, [])]
        m_activa, m_programada = seleccionar_membresia_vigente_y_programada(m_list)
        resultado.append(
            {
                "id_socio_gimnasio": sg_id,
                "id_gimnasio": gym_id,
                "estado_asociacion": sg.get("estado"),
                "socio": socio_obj,
                "membresias": m_list,
                "membresia_activa": m_activa,
                "membresia_programada": m_programada,
            }
        )
    return resultado


@router.get("/planes", status_code=status.HTTP_200_OK)
async def get_pagos_planes(
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Lista los planes activos (`public.membresia_gimnasio`) del gimnasio autorizado para cobros (sin autosembrado legacy)."""
    staff_ctx, gym_id = _verify_staff_can_access_pagos(current_user, id_gimnasio=id_gimnasio)
    db: Client = staff_ctx["supabase"]

    res = (
        db.from_("membresia_gimnasio")
        .select(PLAN_COLUMNS)
        .eq("id_gimnasio", gym_id)
        .order("id_membresia_gimnasio", desc=False)
        .execute()
    )
    rows = get_data(res)
    return [
        p for p in (rows if isinstance(rows, list) else [])
        if isinstance(p, dict) and int(p.get("id_gimnasio") or 0) == gym_id and p.get("estado") is not False
    ]


@router.get("", status_code=status.HTTP_200_OK)
async def get_pagos(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=10, ge=1, le=200),
    estado: Optional[str] = Query(default=None),
    desde: Optional[str] = Query(default=None),
    hasta: Optional[str] = Query(default=None),
    id_membresia_socio: Optional[int] = Query(default=None, gt=0),
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Listado paginado de pagos (`public.pago`) del gimnasio autorizado.
    - Si es socio: devuelve únicamente sus propios pagos.
    - Si es staff financiero autorizado: devuelve los pagos de su gimnasio.
    - Rechaza entrenadores y gimnasios ajenos con `403 Forbidden`.
    """
    if current_user.get("rol") == "socio":
        return await get_mis_pagos(id_gimnasio=id_gimnasio, current_user=current_user)

    staff_ctx, gym_id = _verify_staff_can_access_pagos(current_user, id_gimnasio=id_gimnasio)
    db: Client = staff_ctx["supabase"]
    access_token = staff_ctx.get("access_token")

    [rows] = await run_db_parallel(
        db,
        access_token,
        [lambda c: get_data(c.from_("pago").select(PAGO_SELECT).order("id_pago", desc=True).execute())],
    )
    filtrados = _filter_pagos_by_gym_and_params(
        rows if isinstance(rows, list) else [],
        gym_id=gym_id,
        estado=estado,
        desde=desde,
        hasta=hasta,
        id_membresia_socio=id_membresia_socio,
    )
    filtrados.sort(key=lambda r: int(r.get("id_pago") or 0), reverse=True)
    total = len(filtrados)
    offset = (page - 1) * limit
    pagina_rows = filtrados[offset : offset + limit]

    return {
        "id_gimnasio": gym_id,
        "total": total,
        "pagina": page,
        "limite": limit,
        "datos": pagina_rows,
    }


@router.get("/{id_pago}", status_code=status.HTTP_200_OK)
async def get_pago_by_id(
    id_pago: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Obtiene el detalle de un pago validando pertenencia al socio o al gimnasio autorizado."""
    db: Client = current_user["supabase"]
    pago = _fetch_pago_or_404(db, id_pago)

    if current_user.get("rol") == "socio":
        own_id = current_user.get("id_socio")
        if not own_id or _extract_socio_id_from_pago(pago) != int(own_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes autorización para consultar un pago ajeno.",
            )
        return pago

    gym_id = _extract_gym_id_from_pago(pago)
    if gym_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pago sin contratación asociada.")
    _verify_staff_can_access_pagos(current_user, id_gimnasio=gym_id)
    return pago


@router.post("", status_code=status.HTTP_201_CREATED)
async def registrar_pago(
    req: PagoCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Registra un pago sobre una contratación existente `public.membresia_socio` utilizando
    exclusivamente la RPC `public.rpc_registrar_pago`.
    - Valida pertenencia de la contratación al gimnasio autorizado (evita pagos cruzados).
    - Valida saldo pendiente real en backend y en RPC (bloquea sobrepagos e importes inválidos).
    - Prohíbe operaciones financieras a socios y entrenadores (`403 Forbidden`).
    """
    if current_user.get("rol") == "socio":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Los socios no tienen autorización para registrar pagos administrativos directamente.",
        )

    db_initial: Client = current_user["supabase"]
    contrato = _fetch_membresia_or_404(db_initial, req.id_membresia_socio)
    contrato_gym_id = int(contrato["id_gimnasio"])

    if req.id_gimnasio is not None and int(req.id_gimnasio) != contrato_gym_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No se permiten pagos cruzados entre gimnasios: la contratación pertenece a otro gimnasio.",
        )

    staff_ctx, _ = _verify_staff_can_access_pagos(current_user, id_gimnasio=contrato_gym_id)
    staff_db: Client = staff_ctx["supabase"]

    if str(contrato.get("estado") or "").lower() == "cancelada":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se pueden registrar pagos sobre una contratación cancelada.",
        )

    precio_contrato = round(float(contrato.get("precio") or 0), 2)
    res_pagos_exist = (
        staff_db.from_("pago")
        .select("id_pago, monto, estado, comprobante")
        .eq("id_membresia_socio", req.id_membresia_socio)
        .execute()
    )
    pagos_existentes = get_data(res_pagos_exist) or []
    total_aprobado = round(
        sum(
            float(p.get("monto") or 0)
            for p in (pagos_existentes if isinstance(pagos_existentes, list) else [])
            if isinstance(p, dict) and str(p.get("estado") or "").lower() == "aprobado"
        ),
        2,
    )
    saldo_pendiente = round(max(0.0, precio_contrato - total_aprobado), 2)

    # Si no se envía monto, el backend determina el saldo pendiente real de la contratación
    monto_efectivo = saldo_pendiente if req.monto is None else round(float(req.monto), 2)

    if monto_efectivo < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El monto del pago no puede ser negativo.",
        )

    if precio_contrato > 0 and monto_efectivo == 0 and req.estado == "aprobado":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se puede aprobar un pago de monto 0 cuando el precio contratado es mayor a 0.",
        )

    if req.estado in ("pendiente", "aprobado") and round(total_aprobado + monto_efectivo, 2) > precio_contrato:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"El monto ({monto_efectivo}) excede el saldo pendiente de la contratación "
                f"(precio: {precio_contrato}, ya aprobado: {total_aprobado}). No se permiten sobrepagos."
            ),
        )

    # Al aprobarse el pago y cubrirse el contrato pendiente, reemplazar cualquier membresía activa previa
    # y transferir sus días restantes de uso al vencimiento de la nueva contratación activada.
    membresias_previas_canceladas: List[int] = []
    dias_transferidos: int = 0
    f_venc_c = parse_iso_date(contrato.get("fecha_vencimiento"))
    va_a_activar_pendiente = (
        req.estado == "aprobado"
        and str(contrato.get("estado") or "").strip().lower() == "pendiente"
        and round(total_aprobado + monto_efectivo, 2) >= precio_contrato
    )
    if va_a_activar_pendiente and contrato.get("id_socio_gimnasio") is not None:
        try:
            membresias_previas_canceladas, dias_transferidos = _cancelar_activas_y_calcular_dias_restantes(
                staff_db,
                contrato_gym_id,
                int(contrato["id_socio_gimnasio"]),
                excluir_id_membresia=req.id_membresia_socio,
                hoy=date.today(),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=_map_rpc_error_status(exc),
                detail=f"No fue posible reemplazar la membresía activa previa al aprobar el pago: {exc}",
            ) from exc

    rpc_params = {
        "p_id_membresia_socio": req.id_membresia_socio,
        "p_monto": monto_efectivo,
        "p_metodo_pago": req.metodo_pago,
        "p_estado": req.estado,
        "p_comprobante": req.comprobante.strip() if req.comprobante and req.comprobante.strip() else None,
        "p_fecha_pago": req.fecha_pago or datetime.now(timezone.utc).isoformat(),
    }

    try:
        res_rpc = staff_db.rpc("rpc_registrar_pago", rpc_params).execute()
        pago_creado = get_data(res_rpc)
    except Exception as exc:
        raise HTTPException(
            status_code=_map_rpc_error_status(exc),
            detail=f"No fue posible registrar el pago: {exc}",
        ) from exc

    if va_a_activar_pendiente and dias_transferidos > 0 and f_venc_c is not None:
        nueva_f_venc = (f_venc_c + timedelta(days=dias_transferidos)).isoformat()
        try:
            staff_db.rpc(
                "rpc_actualizar_estado_membresia",
                {
                    "p_id_membresia_socio": req.id_membresia_socio,
                    "p_estado": None,
                    "p_fecha_vencimiento": nueva_f_venc,
                },
            ).execute()
        except Exception as exc:
            raise HTTPException(
                status_code=_map_rpc_error_status(exc),
                detail=f"El pago se registró pero no fue posible sumar los {dias_transferidos} días restantes anteriores: {exc}",
            ) from exc

    return {
        "mensaje": (
            f"Pago registrado con éxito. Se canceló la membresía anterior y se sumaron {dias_transferidos} días restantes."
            if dias_transferidos > 0
            else "Pago registrado con éxito."
        ),
        "pago": pago_creado,
        "dias_transferidos": dias_transferidos,
        "membresias_previas_canceladas": membresias_previas_canceladas,
    }


@router.patch("/{id_pago}/estado", status_code=status.HTTP_200_OK)
async def actualizar_estado_pago(
    req: PagoUpdateEstado,
    id_pago: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Transiciona el estado de un pago existente mediante `public.rpc_actualizar_estado_pago`.
    - Respeta la máquina de estados contable: un pago aprobado solo puede pasar a 'anulado';
      un pago anulado es inmutable; un pago rechazado no puede reactivarse como aprobado.
    - Bloquea modificaciones sobre monto, método, comprobante, fecha o contratación (`403`).
    """
    if (
        req.monto is not None
        or req.metodo_pago is not None
        or req.comprobante is not None
        or req.fecha_pago is not None
        or req.id_membresia_socio is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No está permitido modificar importes, fechas, referencias ni método de un pago existente.",
        )

    if current_user.get("rol") == "socio":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Los socios no tienen autorización para modificar el estado de pagos.",
        )

    db_initial: Client = current_user["supabase"]
    pago_actual = _fetch_pago_or_404(db_initial, id_pago)
    gym_id = _extract_gym_id_from_pago(pago_actual)
    if gym_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pago sin contratación asociada.")

    staff_ctx, _ = _verify_staff_can_access_pagos(current_user, id_gimnasio=gym_id)
    estado_actual = str(pago_actual.get("estado") or "").strip().lower()
    nuevo_estado = req.estado

    if estado_actual == "anulado" and nuevo_estado != "anulado":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Un pago anulado es inmutable y no puede reactivarse ni transicionar.",
        )
    if estado_actual == "aprobado" and nuevo_estado not in ("aprobado", "anulado"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Un pago confirmado/aprobado no puede modificarse arbitrariamente; únicamente admite anulación lógica ('anulado').",
        )
    if estado_actual == "rechazado" and nuevo_estado not in ("rechazado", "anulado"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Un pago rechazado no puede pasar a aprobado; genere un nuevo pago o anúlelo.",
        )

    staff_db: Client = staff_ctx["supabase"]
    membresias_previas_canceladas: List[int] = []
    dias_transferidos: int = 0
    ms_pago = pago_actual.get("membresia_socio") if isinstance(pago_actual.get("membresia_socio"), dict) else None
    f_venc_c = parse_iso_date(ms_pago.get("fecha_vencimiento")) if ms_pago else None
    va_a_activar_pendiente = False
    if (
        nuevo_estado == "aprobado"
        and ms_pago is not None
        and str(ms_pago.get("estado") or "").strip().lower() == "pendiente"
        and ms_pago.get("id_socio_gimnasio") is not None
        and ms_pago.get("id_membresia_socio") is not None
    ):
        precio_ms = round(float(ms_pago.get("precio") or 0), 2)
        monto_este_pago = round(float(pago_actual.get("monto") or 0), 2)
        res_otros_p = (
            staff_db.from_("pago")
            .select("id_pago, monto, estado")
            .eq("id_membresia_socio", int(ms_pago["id_membresia_socio"]))
            .execute()
        )
        otros_p = get_data(res_otros_p) or []
        ya_aprobado = round(
            sum(
                float(p.get("monto") or 0)
                for p in (otros_p if isinstance(otros_p, list) else [])
                if isinstance(p, dict)
                and int(p.get("id_pago") or 0) != id_pago
                and str(p.get("estado") or "").strip().lower() == "aprobado"
            ),
            2,
        )
        va_a_activar_pendiente = round(ya_aprobado + monto_este_pago, 2) >= precio_ms

    if va_a_activar_pendiente:
        try:
            membresias_previas_canceladas, dias_transferidos = _cancelar_activas_y_calcular_dias_restantes(
                staff_db,
                gym_id,
                int(ms_pago["id_socio_gimnasio"]),
                excluir_id_membresia=int(ms_pago["id_membresia_socio"]),
                hoy=date.today(),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=_map_rpc_error_status(exc),
                detail=f"No fue posible reemplazar la membresía activa previa al aprobar el pago: {exc}",
            ) from exc

    try:
        res_rpc = staff_db.rpc(
            "rpc_actualizar_estado_pago",
            {
                "p_id_pago": id_pago,
                "p_estado": nuevo_estado,
            },
        ).execute()
        pago_actualizado = get_data(res_rpc)
    except Exception as exc:
        raise HTTPException(
            status_code=_map_rpc_error_status(exc),
            detail=f"No fue posible actualizar el estado del pago: {exc}",
        ) from exc

    if va_a_activar_pendiente and dias_transferidos > 0 and f_venc_c is not None:
        nueva_f_venc = (f_venc_c + timedelta(days=dias_transferidos)).isoformat()
        try:
            staff_db.rpc(
                "rpc_actualizar_estado_membresia",
                {
                    "p_id_membresia_socio": int(ms_pago["id_membresia_socio"]),
                    "p_estado": None,
                    "p_fecha_vencimiento": nueva_f_venc,
                },
            ).execute()
        except Exception as exc:
            raise HTTPException(
                status_code=_map_rpc_error_status(exc),
                detail=f"El pago se aprobó pero no fue posible sumar los {dias_transferidos} días restantes anteriores: {exc}",
            ) from exc

    return {
        "mensaje": (
            f"Estado del pago actualizado correctamente. Se canceló la membresía anterior y se sumaron {dias_transferidos} días restantes."
            if dias_transferidos > 0
            else "Estado del pago actualizado correctamente."
        ),
        "pago": pago_actualizado,
        "dias_transferidos": dias_transferidos,
        "membresias_previas_canceladas": membresias_previas_canceladas,
    }


@router.delete("/{id_pago}", status_code=status.HTTP_200_OK)
async def anular_pago(
    id_pago: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Anulación lógica de un pago (`estado = 'anulado'`) mediante `public.rpc_actualizar_estado_pago`.
    Está prohibido eliminar físicamente registros de `public.pago` (`trg_proteger_pago_inmutable`).
    """
    res = await actualizar_estado_pago(
        req=PagoUpdateEstado(estado="anulado"),
        id_pago=id_pago,
        current_user=current_user,
    )
    return {
        "mensaje": "Pago anulado lógicamente conservando el historial contable.",
        "anulado": True,
        "pago": res["pago"],
    }
