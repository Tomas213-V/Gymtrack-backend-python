from datetime import datetime, date, timedelta, timezone
from typing import Dict, Any, Optional, List
import calendar

from fastapi import APIRouter, HTTPException, status, Depends, Query
from app.config import supabase
from app.core.dependencies import get_optional_user, get_current_user
from app.services.utils import get_data

router = APIRouter(prefix="/api/dashboard", tags=["Dashboard Admin"])


def _parse_iso(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        # Standardize ISO string
        clean = dt_str.replace("Z", "+00:00")
        if "T" in clean:
            return datetime.fromisoformat(clean)
        return datetime.fromisoformat(f"{clean}T00:00:00+00:00")
    except Exception:
        return None


@router.get("/admin", status_code=status.HTTP_200_OK)
@router.get("", status_code=status.HTTP_200_OK)
async def get_dashboard_admin(
    id_gimnasio: Optional[int] = Query(None),
    current_user: Optional[Dict[str, Any]] = Depends(get_optional_user),
):
    """
    Obtiene métricas consolidadas, KPIs, gráficos y actividades recientes
    para el Dueño / Administrador del Gimnasio.
    """
    gym_id = id_gimnasio or (current_user.get("id_gimnasio") if current_user else None)

    if not gym_id and current_user and current_user.get("id_usuario"):
        # Buscar gimnasio asociado al usuario dueño
        res_g = (
            supabase.from_("gimnasio")
            .select("id_gimnasio")
            .eq("id_usuario", current_user["id_usuario"])
            .maybe_single()
            .execute()
        )
        g_data = get_data(res_g)
        if g_data:
            gym_id = g_data["id_gimnasio"]

    if not gym_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se pudo identificar el gimnasio autenticado.",
        )

    ahora = datetime.now(timezone.utc)
    year_actual = ahora.year
    mes_actual = ahora.month  # 1 to 12

    # Rango mes actual
    primer_dia_mes_actual = datetime(year_actual, mes_actual, 1, 0, 0, 0, tzinfo=timezone.utc)
    ultimo_dia_num_actual = calendar.monthrange(year_actual, mes_actual)[1]
    fin_mes_actual = datetime(year_actual, mes_actual, ultimo_dia_num_actual, 23, 59, 59, 999999, tzinfo=timezone.utc)

    # Rango mes anterior
    if mes_actual == 1:
        year_anterior = year_actual - 1
        mes_anterior = 12
    else:
        year_anterior = year_actual
        mes_anterior = mes_actual - 1

    primer_dia_mes_anterior = datetime(year_anterior, mes_anterior, 1, 0, 0, 0, tzinfo=timezone.utc)
    ultimo_dia_num_anterior = calendar.monthrange(year_anterior, mes_anterior)[1]
    fin_mes_anterior = datetime(year_anterior, mes_anterior, ultimo_dia_num_anterior, 23, 59, 59, 999999, tzinfo=timezone.utc)

    # Rango hoy
    inicio_hoy = datetime(ahora.year, ahora.month, ahora.day, 0, 0, 0, tzinfo=timezone.utc)
    fin_hoy = datetime(ahora.year, ahora.month, ahora.day, 23, 59, 59, 999999, tzinfo=timezone.utc)

    # Rango próximos 7 días
    en_7_dias = ahora + timedelta(days=7)

    # Formatos ISO para comparaciones
    inicio_mes_actual_iso = primer_dia_mes_actual.isoformat()
    fin_mes_actual_iso = fin_mes_actual.isoformat()
    inicio_mes_anterior_iso = primer_dia_mes_anterior.isoformat()
    fin_mes_anterior_iso = fin_mes_anterior.isoformat()
    inicio_hoy_iso = inicio_hoy.isoformat()
    fin_hoy_iso = fin_hoy.isoformat()
    en_7_dias_iso = en_7_dias.isoformat()
    ahora_iso = ahora.isoformat()

    # 1. INFORMACIÓN DEL GIMNASIO
    res_gym = (
        supabase.from_("gimnasio")
        .select("*")
        .eq("id_gimnasio", gym_id)
        .maybe_single()
        .execute()
    )
    gimnasio = get_data(res_gym) or {}

    # 2. CONSULTAR SOCIOS
    res_socios = (
        supabase.from_("socio")
        .select("id_socio, nombre, apellido, dni, email, telefono, estado, fecha_alta")
        .eq("id_gimnasio", gym_id)
        .execute()
    )
    socios = res_socios.data or []

    total_socios = len(socios)
    socios_activos = sum(1 for s in socios if (s.get("estado") or "").lower() == "activo")
    socios_inactivos = sum(1 for s in socios if (s.get("estado") or "").lower() == "inactivo")
    socios_pendientes = sum(1 for s in socios if (s.get("estado") or "").lower() == "pendiente")

    nuevos_socios_mes = 0
    for s in socios:
        f_alta = s.get("fecha_alta")
        if f_alta:
            f_alta_str = str(f_alta).split("T")[0]
            if primer_dia_mes_actual.date().isoformat() <= f_alta_str <= fin_mes_actual.date().isoformat():
                nuevos_socios_mes += 1

    # 3. CONSULTAR PAGOS
    try:
        res_pagos = (
            supabase.from_("pago")
            .select("""
                id_pago,
                monto,
                fecha_pago,
                metodo_pago,
                estado,
                membresia!inner(
                    id_membresia,
                    plan_membresia(nombre),
                    socio!inner(id_socio, nombre, apellido, dni, id_gimnasio)
                )
            """)
            .eq("membresia.socio.id_gimnasio", gym_id)
            .order("id_pago", desc=True)
            .execute()
        )
        pagos_list = res_pagos.data or []
    except Exception:
        # Fallback si falla el join anidado con Supabase
        res_pagos = (
            supabase.from_("pago")
            .select("*, membresia(*, plan_membresia(*), socio(*))")
            .order("id_pago", desc=True)
            .execute()
        )
        pagos_raw = res_pagos.data or []
        pagos_list = [
            p for p in pagos_raw
            if p.get("membresia", {}).get("socio", {}).get("id_gimnasio") == gym_id
        ]

    recaudacion_mes_actual = 0.0
    recaudacion_mes_anterior = 0.0
    pagos_realizados = 0
    pagos_pendientes = 0
    pagos_vencidos = 0

    for p in pagos_list:
        monto = float(p.get("monto") or 0)
        estado_p = (p.get("estado") or "").lower()
        f_pago_raw = p.get("fecha_pago")
        f_pago = str(f_pago_raw).split("T")[0] if f_pago_raw else ""

        if primer_dia_mes_actual.date().isoformat() <= f_pago <= fin_mes_actual.date().isoformat():
            if estado_p == "pagado":
                recaudacion_mes_actual += monto
                pagos_realizados += 1
            elif estado_p == "pendiente":
                pagos_pendientes += 1
            elif estado_p == "vencido":
                pagos_vencidos += 1

        if primer_dia_mes_anterior.date().isoformat() <= f_pago <= fin_mes_anterior.date().isoformat() and estado_p == "pagado":
            recaudacion_mes_anterior += monto

    # Variación porcentual de ingresos
    if recaudacion_mes_anterior > 0:
        variacion_ingresos = round(((recaudacion_mes_actual - recaudacion_mes_anterior) / recaudacion_mes_anterior) * 100)
    elif recaudacion_mes_actual > 0:
        variacion_ingresos = 100
    else:
        variacion_ingresos = 0

    # Histórico de ingresos últimos 6 meses para gráfico
    meses_nombres = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
    ingresos_ultimos_6_meses = []

    for i in range(5, -1, -1):
        # Calcular año y mes de hace i meses
        m_calc = mes_actual - i
        y_calc = year_actual
        while m_calc <= 0:
            m_calc += 12
            y_calc -= 1

        m_inicio_date = date(y_calc, m_calc, 1).isoformat()
        last_day = calendar.monthrange(y_calc, m_calc)[1]
        m_fin_date = date(y_calc, m_calc, last_day).isoformat()

        total_mes = 0.0
        for p in pagos_list:
            est = (p.get("estado") or "").lower()
            f = str(p.get("fecha_pago") or "").split("T")[0]
            if m_inicio_date <= f <= m_fin_date and est == "pagado":
                total_mes += float(p.get("monto") or 0)

        mes_label = f"{meses_nombres[m_calc - 1]}"
        if y_calc != year_actual:
            mes_label += f" '{str(y_calc)[2:]}"

        ingresos_ultimos_6_meses.append({
            "mes": mes_label,
            "recaudacion": total_mes,
        })

    # 4. CONSULTAR ASISTENCIAS
    try:
        res_asist = (
            supabase.from_("asistencia")
            .select("""
                id_asistencia,
                hora_ingreso,
                hora_salida,
                estado,
                socio!inner(id_socio, nombre, apellido, dni, id_gimnasio)
            """)
            .eq("socio.id_gimnasio", gym_id)
            .order("hora_ingreso", desc=True)
            .execute()
        )
        asistencias_list = res_asist.data or []
    except Exception:
        res_asist = (
            supabase.from_("asistencia")
            .select("*, socio(*)")
            .order("hora_ingreso", desc=True)
            .execute()
        )
        asist_raw = res_asist.data or []
        asistencias_list = [
            a for a in asist_raw
            if a.get("socio", {}).get("id_gimnasio") == gym_id
        ]

    asistencias_hoy = 0
    asistencias_mes_actual = 0
    asistencias_mes_anterior = 0

    # Afluencia por día de la semana (Lunes a Domingo)
    dias_semana_map = {0: "Lun", 1: "Mar", 2: "Mié", 3: "Jue", 4: "Vie", 5: "Sáb", 6: "Dom"}
    orden_dias = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
    conteo_dias = {d: 0 for d in orden_dias}

    hoy_date_str = ahora.date().isoformat()
    mes_actual_inicio_date = primer_dia_mes_actual.date().isoformat()
    mes_actual_fin_date = fin_mes_actual.date().isoformat()
    mes_anterior_inicio_date = primer_dia_mes_anterior.date().isoformat()
    mes_anterior_fin_date = fin_mes_anterior.date().isoformat()

    for a in asistencias_list:
        h_raw = a.get("hora_ingreso")
        if h_raw:
            h_date = str(h_raw).split("T")[0]
            if h_date == hoy_date_str:
                asistencias_hoy += 1
            if mes_actual_inicio_date <= h_date <= mes_actual_fin_date:
                asistencias_mes_actual += 1
            if mes_anterior_inicio_date <= h_date <= mes_anterior_fin_date:
                asistencias_mes_anterior += 1

            dt_parsed = _parse_iso(h_raw)
            if dt_parsed:
                # weekday(): Monday is 0 and Sunday is 6
                dia_nombre = dias_semana_map.get(dt_parsed.weekday())
                if dia_nombre in conteo_dias:
                    conteo_dias[dia_nombre] += 1

    variacion_asistencias = 0
    if asistencias_mes_anterior > 0:
        variacion_asistencias = round(((asistencias_mes_actual - asistencias_mes_anterior) / asistencias_mes_anterior) * 100)
    elif asistencias_mes_actual > 0:
        variacion_asistencias = 100

    asistencias_por_dia = [{"dia": dia, "asistencias": conteo_dias[dia]} for dia in orden_dias]

    # 5. CONSULTAR MEMBRESÍAS Y PLANES
    try:
        res_memb = (
            supabase.from_("membresia")
            .select("""
                id_membresia,
                fecha_vencimiento,
                fecha_fin,
                estado,
                plan_membresia(nombre),
                socio!inner(id_socio, nombre, apellido, dni, id_gimnasio)
            """)
            .eq("socio.id_gimnasio", gym_id)
            .execute()
        )
        membresias_list = res_memb.data or []
    except Exception:
        res_memb = (
            supabase.from_("membresia")
            .select("*, plan_membresia(*), socio(*)")
            .execute()
        )
        memb_raw = res_memb.data or []
        membresias_list = [
            m for m in memb_raw
            if m.get("socio", {}).get("id_gimnasio") == gym_id
        ]

    # Distribución por planes
    planes_conteo: Dict[str, int] = {}
    for m in membresias_list:
        plan_nombre = (m.get("plan_membresia") or {}).get("nombre") or "General / Estándar"
        planes_conteo[plan_nombre] = planes_conteo.get(plan_nombre, 0) + 1

    distribucion_planes = [{"name": nombre, "value": count} for nombre, count in planes_conteo.items()]

    # Membresías vencidas y próximas a vencer (excluyendo estrictamente canceladas)
    hoy_date = ahora.date()
    total_membresias_vencidas = 0
    total_membresias_por_vencer = 0
    proximos_vencimientos = []

    for m in membresias_list:
        est = (m.get("estado") or "").lower().strip()
        # Si la membresía está cancelada, no se cuenta como deuda o cuota a cobrar
        if est in ("cancelada", "cancelado"):
            continue

        es_vencida = est in ("vencida", "vencido")
        es_por_vencer = False

        f_venc = m.get("fecha_vencimiento") or m.get("fecha_fin")
        if f_venc:
            try:
                f_venc_date = date.fromisoformat(str(f_venc).split("T")[0])
                dias_restantes = (f_venc_date - hoy_date).days
                if dias_restantes <= 0:
                    es_vencida = True
                elif dias_restantes <= 7:
                    es_por_vencer = True
            except Exception:
                pass

        if es_vencida:
            total_membresias_vencidas += 1
        elif es_por_vencer:
            total_membresias_por_vencer += 1
            socio_data = m.get("socio") or {}
            proximos_vencimientos.append({
                "id_membresia": m.get("id_membresia"),
                "socio": {
                    "id_socio": socio_data.get("id_socio"),
                    "nombre": f"{socio_data.get('nombre') or ''} {socio_data.get('apellido') or ''}".strip(),
                    "dni": socio_data.get("dni"),
                },
                "plan": (m.get("plan_membresia") or {}).get("nombre") or "Estándar",
                "fecha_fin": f_venc,
                "fecha_vencimiento": f_venc,
            })

    # 6. ACTIVIDAD RECIENTE (Últimas 6 asistencias y últimos 6 pagos)
    ultimas_asistencias = []
    for a in asistencias_list[:6]:
        socio_data = a.get("socio") or {}
        ultimas_asistencias.append({
            "id": a.get("id_asistencia"),
            "socio": f"{socio_data.get('nombre') or ''} {socio_data.get('apellido') or ''}".strip(),
            "dni": socio_data.get("dni") or "S/D",
            "hora": a.get("hora_ingreso"),
            "estado": a.get("estado") or "presente",
        })

    ultimos_pagos = []
    for p in pagos_list[:6]:
        memb = p.get("membresia") or {}
        socio_data = memb.get("socio") or {}
        plan_data = memb.get("plan_membresia") or {}
        ultimos_pagos.append({
            "id": p.get("id_pago"),
            "socio": f"{socio_data.get('nombre') or ''} {socio_data.get('apellido') or ''}".strip(),
            "plan": plan_data.get("nombre") or "Cuota Gimnasio",
            "monto": float(p.get("monto") or 0),
            "metodo": p.get("metodo_pago") or "Efectivo",
            "estado": (p.get("estado") or "pagado").lower(),
            "fecha": p.get("fecha_pago"),
        })

    return {
        "gimnasio": {
            "id": gimnasio.get("id_gimnasio") or gym_id,
            "nombre": gimnasio.get("nombre") or "GymTrack Central",
            "codigo_acceso": gimnasio.get("codigo_acceso") or "",
        },
        "kpis": {
            "socios": {
                "total": total_socios,
                "activos": socios_activos,
                "inactivos": socios_inactivos,
                "pendientes": socios_pendientes,
                "nuevosMes": nuevos_socios_mes,
            },
            "ingresos": {
                "mesActual": recaudacion_mes_actual,
                "mesAnterior": recaudacion_mes_anterior,
                "variacionPorcentaje": variacion_ingresos,
                "pagosRealizados": pagos_realizados,
                "pagosPendientes": pagos_pendientes,
                "pagosVencidos": pagos_vencidos,
            },
            "asistencias": {
                "hoy": asistencias_hoy,
                "mesActual": asistencias_mes_actual,
                "mesAnterior": asistencias_mes_anterior,
                "variacionPorcentaje": variacion_asistencias,
            },
            "membresiasPorVencer": total_membresias_por_vencer,
            "membresiasVencidas": total_membresias_vencidas,
        },
        "graficos": {
            "ingresosMensuales": ingresos_ultimos_6_meses,
            "asistenciasPorDia": asistencias_por_dia,
            "distribucionPlanes": (
                distribucion_planes
                if distribucion_planes
                else [{"name": "Estándar", "value": max(1, total_socios)}]
            ),
        },
        "actividadReciente": {
            "asistencias": ultimas_asistencias,
            "pagos": ultimos_pagos,
        },
        "proximosVencimientos": proximos_vencimientos,
    }
