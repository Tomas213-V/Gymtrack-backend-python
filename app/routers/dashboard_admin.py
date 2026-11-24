import calendar
from datetime import datetime, date, timezone
from typing import Dict, Any, Optional, List, Set, Tuple
from fastapi import APIRouter, HTTPException, status, Depends, Query
from supabase import Client

from app.core.dependencies import (
    get_current_user,
    _switch_profile_context_if_available,
    run_db_parallel,
)
from app.services.utils import get_data
from app.routers.gimnasios import ensure_active_organizational_assignments
from app.routers.socios import _fetch_socio_gimnasios_for_gym
from app.routers.membresias import MEMBRESIA_SOCIO_SELECT
from app.routers.pagos import PAGO_SELECT, _extract_gym_id_from_pago
from app.routers.asistencias import (
    ASISTENCIA_SELECT,
    _extract_gym_id_from_asistencia,
    _get_staff_authorized_sucursal_ids_for_gym,
    _fetch_sucursal_or_404,
)
from app.routers.rutinas import ASIGNACION_SELECT

router = APIRouter(prefix="/api/dashboard", tags=["Dashboard Organizacional"])


def _parse_iso(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        clean = str(dt_str).replace("Z", "+00:00")
        if "T" in clean:
            return datetime.fromisoformat(clean)
        return datetime.fromisoformat(f"{clean}T00:00:00+00:00")
    except Exception:
        return None


def _resolve_dashboard_staff_scope(
    current_user: Dict[str, Any],
    id_gimnasio: Optional[int] = None,
    id_sucursal: Optional[int] = None,
) -> Tuple[Dict[str, Any], int, Optional[Set[int]], bool, bool]:
    """
    Resuelve el alcance autorizado del staff para el dashboard organizacional:
      Retorna `(ctx, gym_id, allowed_sucursal_ids_or_None, can_view_financials, can_view_training)`.
    - Dueño / Administrador organizacional activo: alcance completo (todas las sucursales, finanzas y rutinas).
    - Empleado operativo activo:
      - `administrador`: sucursales asignadas, finanzas y rutinas.
      - `recepcionista`: sucursales asignadas y finanzas (sin gestión de rutinas).
      - `entrenador`: sucursales asignadas y rutinas (SIN acceso a información financiera `public.pago`).
    - Socio o gimnasio/sucursal ajena: `403 Forbidden`.
    """
    ensure_active_organizational_assignments(current_user)
    if current_user.get("rol") == "socio" and current_user.get("requested_role") == "socio":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Los socios no tienen acceso al dashboard organizacional.",
        )

    db_initial: Client = current_user["supabase"]
    suc_gym_id: Optional[int] = None
    if id_sucursal is not None:
        suc = _fetch_sucursal_or_404(db_initial, id_sucursal)
        suc_gym_id = int(suc["id_gimnasio"])
        if id_gimnasio is not None and int(id_gimnasio) != suc_gym_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="La sucursal indicada no pertenece al gimnasio solicitado.",
            )

    req_gym = current_user.get("requested_gym_id")
    target_gid = suc_gym_id or id_gimnasio or current_user.get("id_gimnasio")
    if target_gid is not None and req_gym is not None and int(target_gid) != int(req_gym):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No se permite cruzar gimnasios al consultar el dashboard.",
        )

    def _try(ctx: Dict[str, Any]):
        rol = ctx.get("rol")
        if rol == "socio":
            return None
        gid = int(target_gid) if target_gid is not None else (
            int(ctx["id_gimnasio"]) if ctx.get("id_gimnasio") is not None else None
        )
        if gid is None:
            return None
        scope = _get_staff_authorized_sucursal_ids_for_gym(
            ctx, gid, {"administrador", "recepcionista", "entrenador"}
        )
        if scope is None:
            # Dueño o administrador organizacional
            suc_filter = {int(id_sucursal)} if id_sucursal is not None else None
            return ctx, gid, suc_filter, True, True
        if not scope:
            return None
        if id_sucursal is not None and int(id_sucursal) not in scope:
            return None
        roles_gym = {
            r.lower() for r in ((ctx.get("roles_empleado_por_gimnasio") or {}).get(gid) or [])
        }
        can_view_fin = bool(roles_gym.intersection({"administrador", "recepcionista"}))
        can_view_train = bool(roles_gym.intersection({"administrador", "entrenador"}))
        suc_filter = {int(id_sucursal)} if id_sucursal is not None else scope
        return ctx, gid, suc_filter, can_view_fin, can_view_train

    matched = _try(current_user)
    if matched is not None:
        return matched

    if current_user.get("requested_role") is None:
        for candidate in ("dueño", "administrador", "empleado"):
            if candidate in (current_user.get("perfiles_disponibles") or []):
                switched = _switch_profile_context_if_available(current_user, candidate)
                matched = _try(switched)
                if matched is not None:
                    return matched

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="No tienes autorización para consultar el dashboard de este gimnasio o sucursal.",
    )


@router.get("/admin", status_code=status.HTTP_200_OK)
@router.get("", status_code=status.HTTP_200_OK)
async def get_dashboard_admin(
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    id_sucursal: Optional[int] = Query(default=None, gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Dashboard organizacional adaptado al modelo definitivo de Supabase:
    - Aislado estrictamente por `id_gimnasio` y sucursales autorizadas del usuario (cliente RLS).
    - KPIs de socios (`socio_gimnasio`), membresías (`membresia_socio` + `membresia_gimnasio`),
      pagos (`pago`, solo si el rol tiene permiso financiero; oculto para `entrenador`),
      asistencias (`asistencia_socio` + `sucursal`, excluyendo anuladas) y rutinas/asignaciones
      (`rutina_gimnasio` + `rutinas_asignadas_socio_gimnasio`).
    """
    staff_ctx, gym_id, allowed_suc_ids, can_view_financials, can_view_training = _resolve_dashboard_staff_scope(
        current_user, id_gimnasio=id_gimnasio, id_sucursal=id_sucursal
    )
    db: Client = staff_ctx["supabase"]

    ahora = datetime.now(timezone.utc)
    hoy_date = ahora.date()
    year_actual = ahora.year
    mes_actual = ahora.month

    primer_dia_mes_actual = date(year_actual, mes_actual, 1)
    ultimo_dia_mes_actual = date(year_actual, mes_actual, calendar.monthrange(year_actual, mes_actual)[1])

    if mes_actual == 1:
        year_anterior, mes_anterior = year_actual - 1, 12
    else:
        year_anterior, mes_anterior = year_actual, mes_actual - 1

    primer_dia_mes_anterior = date(year_anterior, mes_anterior, 1)
    ultimo_dia_mes_anterior = date(year_anterior, mes_anterior, calendar.monthrange(year_anterior, mes_anterior)[1])

    # Ejecutar todas las consultas independientes del dashboard en paralelo (sin bloquear event loop)
    access_token = staff_ctx.get("access_token")

    def _q_gym(client: Client):
        return get_data(
            client.from_("gimnasio")
            .select("id_gimnasio, nombre, telefono, email, fecha_registro, estado, id_dueno")
            .eq("id_gimnasio", gym_id)
            .maybe_single()
            .execute()
        ) or {"id_gimnasio": gym_id}

    def _q_sucs(client: Client):
        return get_data(
            client.from_("sucursal")
            .select("id_sucursal, descripcion, direccion, telefono, estado, id_gimnasio")
            .eq("id_gimnasio", gym_id)
            .order("id_sucursal", desc=False)
            .execute()
        ) or []

    def _q_asoc(client: Client):
        return _fetch_socio_gimnasios_for_gym(client, gym_id)

    def _q_memb(client: Client):
        return get_data(
            client.from_("membresia_socio")
            .select(MEMBRESIA_SOCIO_SELECT)
            .eq("id_gimnasio", gym_id)
            .order("fecha_vencimiento", desc=True)
            .execute()
        ) or []

    def _q_asist(client: Client):
        return get_data(
            client.from_("asistencia_socio")
            .select(ASISTENCIA_SELECT)
            .order("hora_ingreso", desc=True)
            .execute()
        ) or []

    def _q_pagos(client: Client):
        return get_data(client.from_("pago").select(PAGO_SELECT).order("id_pago", desc=True).execute()) or []

    def _q_rutinas(client: Client):
        return get_data(
            client.from_("rutina_gimnasio")
            .select("id_rutina_gimnasio, id_gimnasio, estado")
            .eq("id_gimnasio", gym_id)
            .execute()
        ) or []

    def _q_asignaciones(client: Client):
        return get_data(
            client.from_("rutinas_asignadas_socio_gimnasio")
            .select(ASIGNACION_SELECT)
            .eq("id_gimnasio", gym_id)
            .execute()
        ) or []

    query_tasks: List[Any] = [_q_gym, _q_sucs, _q_asoc, _q_memb, _q_asist]
    if can_view_financials:
        query_tasks.append(_q_pagos)
    if can_view_training:
        query_tasks.extend([_q_rutinas, _q_asignaciones])

    query_results = await run_db_parallel(db, access_token, query_tasks)
    idx = 0
    gimnasio = query_results[idx]; idx += 1
    sucursales_raw = query_results[idx]; idx += 1
    asociaciones = query_results[idx]; idx += 1
    membresias_raw = query_results[idx]; idx += 1
    asist_raw = query_results[idx]; idx += 1
    pagos_raw = query_results[idx] if can_view_financials else []
    if can_view_financials:
        idx += 1
    rg_raw = query_results[idx] if can_view_training else []
    asig_raw = query_results[idx + 1] if can_view_training else []

    # 1. Gimnasio y sucursales autorizadas
    sucursales = [
        s for s in (sucursales_raw if isinstance(sucursales_raw, list) else [])
        if isinstance(s, dict)
        and int(s.get("id_gimnasio") or 0) == gym_id
        and (allowed_suc_ids is None or int(s.get("id_sucursal") or 0) in allowed_suc_ids)
    ]

    # 2. Socios asociados (`public.socio_gimnasio`)
    total_socios = len(asociaciones)
    socios_activos = sum(1 for a in asociaciones if a.get("estado") is True)
    socios_inactivos = sum(1 for a in asociaciones if a.get("estado") is not True)
    nuevos_socios_mes = sum(
        1
        for a in asociaciones
        if a.get("fecha_alta")
        and primer_dia_mes_actual.isoformat() <= str(a["fecha_alta"]).split("T")[0] <= ultimo_dia_mes_actual.isoformat()
    )

    # 3. Contrataciones de membresías (`public.membresia_socio`)
    membresias_list = [
        m for m in (membresias_raw if isinstance(membresias_raw, list) else [])
        if isinstance(m, dict) and int(m.get("id_gimnasio") or 0) == gym_id
    ]

    planes_conteo: Dict[str, int] = {}
    membresias_activas = 0
    membresias_pendientes = 0
    total_membresias_vencidas = 0
    total_membresias_por_vencer = 0
    proximos_vencimientos: List[Dict[str, Any]] = []

    for m in membresias_list:
        plan_obj = m.get("membresia_gimnasio") if isinstance(m.get("membresia_gimnasio"), dict) else {}
        plan_nombre = plan_obj.get("nombre") or "Plan sin nombre"
        est = str(m.get("estado") or "").strip().lower()

        if est != "cancelada":
            planes_conteo[plan_nombre] = planes_conteo.get(plan_nombre, 0) + 1

        if est == "cancelada":
            continue
        if est == "pendiente":
            membresias_pendientes += 1
            continue

        es_vencida = est == "vencida"
        es_por_vencer = False
        f_venc = m.get("fecha_vencimiento")
        if f_venc:
            try:
                fv_date = date.fromisoformat(str(f_venc).split("T")[0])
                dias_rest = (fv_date - hoy_date).days
                if dias_rest < 0:
                    es_vencida = True
                elif est == "activa":
                    membresias_activas += 1
                    if dias_rest <= 7:
                        es_por_vencer = True
            except Exception:
                if est == "activa":
                    membresias_activas += 1
        elif est == "activa":
            membresias_activas += 1

        if es_vencida:
            total_membresias_vencidas += 1
        elif es_por_vencer:
            total_membresias_por_vencer += 1
            sg = m.get("socio_gimnasio") if isinstance(m.get("socio_gimnasio"), dict) else {}
            soc = sg.get("socio") if isinstance(sg.get("socio"), dict) else {}
            proximos_vencimientos.append(
                {
                    "id_membresia_socio": m.get("id_membresia_socio"),
                    "socio": {
                        "id_socio": soc.get("id_socio"),
                        "nombre": f"{soc.get('nombre') or ''} {soc.get('apellido') or ''}".strip(),
                        "dni": soc.get("dni"),
                    },
                    "plan": plan_nombre,
                    "fecha_vencimiento": f_venc,
                }
            )

    distribucion_planes = [{"name": nombre, "value": cnt} for nombre, cnt in planes_conteo.items()]

    # 4. Pagos (`public.pago`) — SOLO para roles con acceso financiero (nunca entrenador)
    ingresos_kpi: Optional[Dict[str, Any]] = None
    ingresos_ultimos_6_meses: List[Dict[str, Any]] = []
    ultimos_pagos: List[Dict[str, Any]] = []

    if can_view_financials:
        pagos_list = [
            p for p in (pagos_raw if isinstance(pagos_raw, list) else [])
            if isinstance(p, dict) and _extract_gym_id_from_pago(p) == gym_id
        ]

        recaudacion_mes_actual = 0.0
        recaudacion_mes_anterior = 0.0
        pagos_aprobados = 0
        pagos_pendientes = 0
        pagos_rechazados = 0
        pagos_anulados = 0

        for p in pagos_list:
            monto = float(p.get("monto") or 0)
            est_p = str(p.get("estado") or "").strip().lower()
            f_pago = str(p.get("fecha_pago") or "").split("T")[0]

            if primer_dia_mes_actual.isoformat() <= f_pago <= ultimo_dia_mes_actual.isoformat():
                if est_p == "aprobado":
                    recaudacion_mes_actual += monto
                    pagos_aprobados += 1
                elif est_p == "pendiente":
                    pagos_pendientes += 1
                elif est_p == "rechazado":
                    pagos_rechazados += 1
                elif est_p == "anulado":
                    pagos_anulados += 1

            if (
                primer_dia_mes_anterior.isoformat() <= f_pago <= ultimo_dia_mes_anterior.isoformat()
                and est_p == "aprobado"
            ):
                recaudacion_mes_anterior += monto

        if recaudacion_mes_anterior > 0:
            variacion_ingresos = round(
                ((recaudacion_mes_actual - recaudacion_mes_anterior) / recaudacion_mes_anterior) * 100
            )
        elif recaudacion_mes_actual > 0:
            variacion_ingresos = 100
        else:
            variacion_ingresos = 0

        ingresos_kpi = {
            "mesActual": round(recaudacion_mes_actual, 2),
            "mesAnterior": round(recaudacion_mes_anterior, 2),
            "variacionPorcentaje": variacion_ingresos,
            "pagosAprobados": pagos_aprobados,
            "pagosPendientes": pagos_pendientes,
            "pagosRechazados": pagos_rechazados,
            "pagosAnulados": pagos_anulados,
        }

        meses_nombres = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
        for i in range(5, -1, -1):
            m_calc = mes_actual - i
            y_calc = year_actual
            while m_calc <= 0:
                m_calc += 12
                y_calc -= 1
            m_ini = date(y_calc, m_calc, 1).isoformat()
            m_fin = date(y_calc, m_calc, calendar.monthrange(y_calc, m_calc)[1]).isoformat()
            total_mes = sum(
                float(p.get("monto") or 0)
                for p in pagos_list
                if str(p.get("estado") or "").strip().lower() == "aprobado"
                and m_ini <= str(p.get("fecha_pago") or "").split("T")[0] <= m_fin
            )
            label = meses_nombres[m_calc - 1]
            if y_calc != year_actual:
                label += f" '{str(y_calc)[2:]}"
            ingresos_ultimos_6_meses.append({"mes": label, "recaudacion": round(total_mes, 2)})

        for p in pagos_list[:6]:
            ms = p.get("membresia_socio") if isinstance(p.get("membresia_socio"), dict) else {}
            sg = ms.get("socio_gimnasio") if isinstance(ms.get("socio_gimnasio"), dict) else {}
            soc = sg.get("socio") if isinstance(sg.get("socio"), dict) else {}
            plan_obj = ms.get("membresia_gimnasio") if isinstance(ms.get("membresia_gimnasio"), dict) else {}
            ultimos_pagos.append(
                {
                    "id_pago": p.get("id_pago"),
                    "id_membresia_socio": p.get("id_membresia_socio"),
                    "socio": f"{soc.get('nombre') or ''} {soc.get('apellido') or ''}".strip(),
                    "plan": plan_obj.get("nombre") or "Cuota Gimnasio",
                    "monto": float(p.get("monto") or 0),
                    "metodo_pago": p.get("metodo_pago"),
                    "estado": str(p.get("estado") or "").lower(),
                    "fecha_pago": p.get("fecha_pago"),
                }
            )

    # 5. Asistencias (`public.asistencia_socio`, excluyendo 'anulada' y filtradas por sucursales autorizadas)
    asistencias_list = [
        a for a in (asist_raw if isinstance(asist_raw, list) else [])
        if isinstance(a, dict)
        and _extract_gym_id_from_asistencia(a) == gym_id
        and (allowed_suc_ids is None or int(a.get("id_sucursal") or 0) in allowed_suc_ids)
        and str(a.get("estado") or "").strip().lower() != "anulada"
    ]

    asistencias_hoy = 0
    asistencias_abiertas = 0
    asistencias_mes_actual = 0
    asistencias_mes_anterior = 0
    dias_semana_map = {0: "Lun", 1: "Mar", 2: "Mié", 3: "Jue", 4: "Vie", 5: "Sáb", 6: "Dom"}
    orden_dias = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
    conteo_dias = {d: 0 for d in orden_dias}

    hoy_str = hoy_date.isoformat()
    for a in asistencias_list:
        if str(a.get("estado") or "").lower() == "presente" and a.get("hora_salida") is None:
            asistencias_abiertas += 1
        h_raw = a.get("hora_ingreso")
        if h_raw:
            h_date = str(h_raw).split("T")[0]
            if h_date == hoy_str:
                asistencias_hoy += 1
            if primer_dia_mes_actual.isoformat() <= h_date <= ultimo_dia_mes_actual.isoformat():
                asistencias_mes_actual += 1
            if primer_dia_mes_anterior.isoformat() <= h_date <= ultimo_dia_mes_anterior.isoformat():
                asistencias_mes_anterior += 1
            dt_parsed = _parse_iso(h_raw)
            if dt_parsed:
                dia_nombre = dias_semana_map.get(dt_parsed.weekday())
                if dia_nombre in conteo_dias:
                    conteo_dias[dia_nombre] += 1

    if asistencias_mes_anterior > 0:
        variacion_asistencias = round(
            ((asistencias_mes_actual - asistencias_mes_anterior) / asistencias_mes_anterior) * 100
        )
    elif asistencias_mes_actual > 0:
        variacion_asistencias = 100
    else:
        variacion_asistencias = 0

    asistencias_por_dia = [{"dia": d, "asistencias": conteo_dias[d]} for d in orden_dias]
    ultimas_asistencias = []
    for a in asistencias_list[:6]:
        soc = a.get("socio") if isinstance(a.get("socio"), dict) else {}
        suc = a.get("sucursal") if isinstance(a.get("sucursal"), dict) else {}
        ultimas_asistencias.append(
            {
                "id_asistencia_socio": a.get("id_asistencia_socio"),
                "id_sucursal": a.get("id_sucursal"),
                "sucursal": suc.get("descripcion") or suc.get("direccion"),
                "socio": f"{soc.get('nombre') or ''} {soc.get('apellido') or ''}".strip(),
                "dni": soc.get("dni") or "S/D",
                "hora_ingreso": a.get("hora_ingreso"),
                "hora_salida": a.get("hora_salida"),
                "estado": a.get("estado"),
            }
        )

    # 6. Rutinas y asignaciones (`public.rutina_gimnasio` y `public.rutinas_asignadas_socio_gimnasio`)
    rutinas_kpi: Optional[Dict[str, Any]] = None
    if can_view_training:
        rg_rows = [
            r for r in (rg_raw if isinstance(rg_raw, list) else [])
            if isinstance(r, dict) and int(r.get("id_gimnasio") or 0) == gym_id
        ]
        asig_rows = [
            a for a in (asig_raw if isinstance(asig_raw, list) else [])
            if isinstance(a, dict) and int(a.get("id_gimnasio") or 0) == gym_id
        ]
        rutinas_kpi = {
            "totalPlantillas": len(rg_rows),
            "plantillasActivas": sum(1 for r in rg_rows if r.get("estado") is True),
            "asignacionesActivas": sum(1 for a in asig_rows if a.get("estado") is True),
            "asignacionesFinalizadas": sum(1 for a in asig_rows if a.get("estado") is not True),
        }

    return {
        "gimnasio": {
            "id_gimnasio": gimnasio.get("id_gimnasio", gym_id),
            "nombre": gimnasio.get("nombre"),
            "email": gimnasio.get("email"),
            "telefono": gimnasio.get("telefono"),
            "estado": gimnasio.get("estado", True),
        },
        "alcance": {
            "rol": staff_ctx.get("rol"),
            "id_gimnasio": gym_id,
            "id_sucursal_filtro": id_sucursal,
            "sucursales_autorizadas": [s.get("id_sucursal") for s in sucursales],
            "acceso_financiero": can_view_financials,
            "acceso_rutinas": can_view_training,
        },
        "sucursales": sucursales,
        "kpis": {
            "socios": {
                "total": total_socios,
                "activos": socios_activos,
                "inactivos": socios_inactivos,
                "nuevosMes": nuevos_socios_mes,
            },
            "ingresos": ingresos_kpi,
            "asistencias": {
                "hoy": asistencias_hoy,
                "abiertas": asistencias_abiertas,
                "mesActual": asistencias_mes_actual,
                "mesAnterior": asistencias_mes_anterior,
                "variacionPorcentaje": variacion_asistencias,
            },
            "membresias": {
                "activas": membresias_activas,
                "pendientes": membresias_pendientes,
                "porVencer": total_membresias_por_vencer,
                "vencidas": total_membresias_vencidas,
            },
            "rutinas": rutinas_kpi,
        },
        "graficos": {
            "ingresosMensuales": ingresos_ultimos_6_meses,
            "asistenciasPorDia": asistencias_por_dia,
            "distribucionPlanes": distribucion_planes,
        },
        "actividadReciente": {
            "asistencias": ultimas_asistencias,
            "pagos": ultimos_pagos,
        },
        "proximosVencimientos": proximos_vencimientos,
    }
