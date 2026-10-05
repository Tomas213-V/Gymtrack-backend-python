import re
import time
import urllib.parse
from datetime import datetime, date, timedelta, timezone
from typing import Dict, Any, Optional, List
import httpx
from fastapi import APIRouter, HTTPException, status, Depends, Query, Request
from app.config import (
    supabase,
    MERCADOPAGO_ACCESS_TOKEN,
    FRONTEND_URL,
)
from app.core.dependencies import get_current_user
from app.services.utils import (
    formatear_fecha_espanol,
    formatear_precio_ars,
    calcular_metricas_membresia,
    get_data,
    sugerir_beneficios,
)
from app.services.payment_service import (
    crear_preferencia_mercadopago,
    crear_orden_uala,
)
from app.models.dashboard_socio import (
    ProgresoCreate,
    ActualizarMembresiaSocio,
    IniciarPagoSocio,
    ConfirmarPagoSocio,
)

router = APIRouter(prefix="/api/socio", tags=["Panel Socio y Pagos"])


async def resolver_id_socio(current_user: Dict[str, Any], query_id: Optional[int] = None) -> Optional[int]:
    """Resuelve el ID de socio según el token JWT o consulta administrativa."""
    if current_user.get("id_socio"):
        return int(current_user["id_socio"])

    if current_user.get("id_usuario"):
        res = (
            supabase.from_("socio")
            .select("id_socio")
            .eq("id_usuario", current_user["id_usuario"])
            .maybe_single()
            .execute()
        )
        socio_data = get_data(res)
        if socio_data:
            return int(socio_data["id_socio"])

    if query_id and current_user.get("rol") in ("dueño", "administrador"):
        return int(query_id)

    return None


async def obtener_socio_autenticado(current_user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Obtiene los datos completos del socio autenticado."""
    id_socio = await resolver_id_socio(current_user)
    if not id_socio:
        return None

    res = (
        supabase.from_("socio")
        .select("id_socio, id_gimnasio, nombre, apellido, dni, email, telefono, estado, fecha_alta")
        .eq("id_socio", id_socio)
        .maybe_single()
        .execute()
    )
    return get_data(res)


# ==============================================================================
# WEBHOOK PÚBLICO
# ==============================================================================

@router.post("/pagos/webhook", status_code=status.HTTP_200_OK)
async def webhook_pago(request: Request, proveedor: Optional[str] = Query("mercadopago")):
    """Endpoint público para webhooks de pasarelas de pago."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    query_params = dict(request.query_params)
    print(f"[WEBHOOK {proveedor.upper()}] Recibido payload:", body or query_params)

    # Procesar notificación básica
    return {"recibido": True, "proveedor": proveedor}


# ==============================================================================
# DASHBOARD GENERAL DEL SOCIO
# ==============================================================================

@router.get("/dashboard", status_code=status.HTTP_200_OK)
async def get_dashboard_socio(
    id: Optional[int] = Query(None),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Carga todas las métricas, membresía, rutinas y progreso para el panel del socio."""
    id_socio = await resolver_id_socio(current_user, id)
    if not id_socio:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario no autenticado como socio.")

    # 1. Socio y gimnasio
    res_socio = (
        supabase.from_("socio")
        .select("""
            id_socio,
            id_gimnasio,
            dni,
            telefono,
            fecha_alta,
            estado,
            nombre,
            apellido,
            email,
            gimnasio (
                id_gimnasio,
                nombre,
                direccion,
                telefono,
                email
            )
        """)
        .eq("id_socio", id_socio)
        .maybe_single()
        .execute()
    )
    socio = get_data(res_socio)
    if not socio:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No se encontraron datos del socio en el sistema.")

    # 2. Membresía más reciente
    res_memb = (
        supabase.from_("membresia")
        .select("""
            id_membresia,
            id_plan_membresia,
            precio,
            fecha_inicio,
            fecha_vencimiento,
            estado,
            plan_membresia (
                id_plan_membresia,
                nombre,
                precio,
                duracion_dias
            )
        """)
        .eq("id_socio", id_socio)
        .order("fecha_vencimiento", desc=True)
        .limit(1)
        .maybe_single()
        .execute()
    )
    membresia = get_data(res_memb)

    # 3. Último pago
    res_pago = (
        supabase.from_("pago")
        .select("id_pago, monto, fecha_pago, metodo_pago, estado, comprobante, id_membresia, membresia!inner(id_socio)")
        .eq("membresia.id_socio", id_socio)
        .order("fecha_pago", desc=True)
        .limit(1)
        .maybe_single()
        .execute()
    )
    ultimo_pago = get_data(res_pago)

    # 4. Progresos
    res_prog_ultimo = (
        supabase.from_("progreso")
        .select("id_progreso, peso, fecha, observaciones")
        .eq("id_socio", id_socio)
        .order("fecha", desc=True)
        .limit(1)
        .maybe_single()
        .execute()
    )
    ultimo_progreso = get_data(res_prog_ultimo)

    res_prog_hist = (
        supabase.from_("progreso")
        .select("id_progreso, peso, fecha, observaciones")
        .eq("id_socio", id_socio)
        .order("fecha", desc=True)
        .limit(6)
        .execute()
    )
    historial_progreso = res_prog_hist.data or []

    # 5. Asistencias
    hoy = date.today()
    inicio_mes = date(hoy.year, hoy.month, 1).isoformat()
    res_asist_tot = supabase.from_("asistencia").select("id_asistencia", count="exact").eq("id_socio", id_socio).execute()
    res_asist_mes = (
        supabase.from_("asistencia")
        .select("id_asistencia", count="exact")
        .eq("id_socio", id_socio)
        .gte("hora_ingreso", inicio_mes)
        .execute()
    )
    res_asist_rec = (
        supabase.from_("asistencia")
        .select("id_asistencia, hora_ingreso, hora_salida, estado")
        .eq("id_socio", id_socio)
        .order("hora_ingreso", desc=True)
        .limit(5)
        .execute()
    )

    total_asistencias = res_asist_tot.count or 0
    asistencias_mes = res_asist_mes.count or 0
    asistencias_recientes = res_asist_rec.data or []

    # 6. Rutina activa
    res_rutina = (
        supabase.from_("rutina")
        .select("""
            id_rutina,
            nombre,
            descripcion,
            estado,
            rutina_ejercicio (
                id_rutina_ejercicio,
                series,
                repeticiones,
                peso,
                descanso,
                orden,
                ejercicio (
                    id_ejercicio,
                    nombre,
                    grupo_muscular,
                    dificultad
                )
            )
        """)
        .eq("id_socio", id_socio)
        .order("id_rutina", desc=True)
        .limit(1)
        .maybe_single()
        .execute()
    )
    rutina_activa = get_data(res_rutina)

    # Métricas membresía
    dias_restantes = None
    porcentaje_transcurrido = None
    membresia_expirada = False

    if membresia and membresia.get("fecha_vencimiento"):
        try:
            fv = date.fromisoformat(str(membresia["fecha_vencimiento"]).split("T")[0])
            dias_restantes = (fv - hoy).days
            membresia_expirada = dias_restantes < 0

            fi = date.fromisoformat(str(membresia.get("fecha_inicio") or (fv - timedelta(days=30))).split("T")[0])
            tot_d = max(1, (fv - fi).days)
            usados_d = max(0, (hoy - fi).days)
            porcentaje_transcurrido = min(100, max(0, round((usados_d / tot_d) * 100)))
        except Exception:
            pass

    # Variación de peso
    variacion_peso = None
    if len(historial_progreso) >= 2:
        try:
            actual = float(historial_progreso[0]["peso"])
            previo = float(historial_progreso[1]["peso"])
            variacion_peso = round(actual - previo, 1)
        except Exception:
            pass

    # Ordenar ejercicios
    ejercicios_ordenados = []
    if rutina_activa and rutina_activa.get("rutina_ejercicio"):
        ejercicios_ordenados = sorted(rutina_activa["rutina_ejercicio"], key=lambda x: x.get("orden") or 0)

    plan_info = membresia.get("plan_membresia") if membresia else None
    plan_nombre = plan_info.get("nombre") if plan_info else "Plan Personalizado"
    plan_precio = (membresia.get("precio") or (plan_info.get("precio") if plan_info else 0)) if membresia else 0

    return {
        "socio": {
            "id_socio": socio["id_socio"],
            "nombre": socio.get("nombre"),
            "apellido": socio.get("apellido"),
            "dni": socio.get("dni"),
            "email": socio.get("email"),
            "telefono": socio.get("telefono"),
        },
        "gimnasio": socio.get("gimnasio") or {
            "nombre": "GymTrack Fitness Club",
            "direccion": "Av. Central 1234",
            "telefono": "+54 11 5555-8888",
        },
        "membresia": {
            "id_membresia": membresia["id_membresia"],
            "plan": plan_nombre,
            "estado": membresia.get("estado"),
            "fecha_inicio": membresia.get("fecha_inicio"),
            "fecha_vencimiento": membresia.get("fecha_vencimiento"),
            "precio": plan_precio,
            "dias_restantes": dias_restantes,
            "porcentaje_transcurrido": porcentaje_transcurrido,
            "expirada": membresia_expirada,
        } if membresia else {
            "estado": "sin_membresia",
            "mensaje": "No tienes una membresía activa asignada.",
        },
        "ultimo_pago": ultimo_pago,
        "progreso": {
            "peso_actual": ultimo_progreso.get("peso") if ultimo_progreso else None,
            "observaciones": ultimo_progreso.get("observaciones") if ultimo_progreso else None,
            "fecha_ultimo_registro": ultimo_progreso.get("fecha") if ultimo_progreso else None,
            "variacion_mes": variacion_peso,
            "historial": historial_progreso,
        },
        "entrenamientos": {
            "total_asistencias": total_asistencias,
            "asistencias_mes": asistencias_mes,
            "recientes": asistencias_recientes,
        },
        "rutina_activa": {
            "id_rutina": rutina_activa["id_rutina"],
            "nombre": rutina_activa.get("nombre"),
            "descripcion": rutina_activa.get("descripcion"),
            "ejercicios": ejercicios_ordenados,
        } if rutina_activa else None,
        "acciones_rapidas": [
            {"clave": "ver_rutina", "titulo": "Mi Rutina", "activo": bool(rutina_activa), "link": "/rutinas"},
            {"clave": "mi_membresia", "titulo": "Membresía", "activo": True, "link": "/socio/membresia"},
            {"clave": "asistencias", "titulo": "Asistencias", "activo": True, "link": "/asistencias"},
            {"clave": "ejercicios", "titulo": "Ejercicios", "activo": True, "link": "/ejercicios"},
            {"clave": "perfil", "titulo": "Mi Perfil", "activo": True, "link": "/perfil"},
        ],
    }


@router.post("/checkin", status_code=status.HTTP_201_CREATED)
async def registrar_checkin_socio(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Permite al socio marcar su asistencia directamente desde su panel."""
    id_socio = await resolver_id_socio(current_user)
    if not id_socio:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario no autenticado como socio.")

    now_iso = datetime.now(timezone.utc).isoformat()
    res = (
        supabase.from_("asistencia")
        .insert([{
            "id_socio": id_socio,
            "hora_ingreso": now_iso,
            "estado": "presente",
        }])
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al registrar check-in.")

    return {
        "mensaje": "¡Asistencia registrada con éxito! Que tengas un gran entrenamiento 💪",
        "asistencia": res.data[0],
    }


@router.post("/progreso", status_code=status.HTTP_201_CREATED)
async def registrar_progreso_socio(
    req: ProgresoCreate,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Permite al socio cargar su peso actual y notas desde su panel."""
    id_socio = await resolver_id_socio(current_user)
    if not id_socio:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario no autenticado como socio.")

    fecha_registro = req.fecha or date.today().isoformat()
    res = (
        supabase.from_("progreso")
        .insert([{
            "id_socio": id_socio,
            "peso": float(req.peso),
            "fecha": fecha_registro,
            "observaciones": req.observaciones or None,
        }])
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al registrar el progreso.")

    return {
        "mensaje": "¡Peso registrado exitosamente!",
        "progreso": res.data[0],
    }


# ==============================================================================
# MÓDULO DE MI MEMBRESÍA DEL SOCIO
# ==============================================================================

@router.get("/membresia", status_code=status.HTTP_200_OK)
async def get_mi_membresia(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Obtener información completa de la membresía del socio, métricas y pagos."""
    socio = await obtener_socio_autenticado(current_user)
    if not socio:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Acceso no autorizado. Perfil no identificado.")

    # 1. Membresía más reciente
    res_m = (
        supabase.from_("membresia")
        .select("""
            *,
            plan_membresia (*)
        """)
        .eq("id_socio", socio["id_socio"])
        .order("fecha_vencimiento", desc=True)
        .limit(1)
        .maybe_single()
        .execute()
    )
    membresia = get_data(res_m)

    # 2. Historial de pagos
    res_pagos = (
        supabase.from_("pago")
        .select("""
            id_pago,
            monto,
            fecha_pago,
            metodo_pago,
            estado,
            comprobante,
            membresia!inner(id_socio)
        """)
        .eq("membresia.id_socio", socio["id_socio"])
        .order("fecha_pago", desc=True)
        .limit(10)
        .execute()
    )
    pagos = res_pagos.data or []

    # 3. Planes disponibles
    planes = []
    if socio.get("id_gimnasio"):
        res_pl = (
            supabase.from_("plan_membresia")
            .select("*")
            .eq("id_gimnasio", socio["id_gimnasio"])
            .order("precio", desc=False)
            .execute()
        )
        planes_raw = res_pl.data or []
        if not planes_raw:
            planes_semilla = [
                {"id_gimnasio": socio["id_gimnasio"], "nombre": "Plan Básico", "precio": 15000, "duracion_dias": 30, "estado": "Activo"},
                {"id_gimnasio": socio["id_gimnasio"], "nombre": "Plan Premium", "precio": 22000, "duracion_dias": 30, "estado": "Activo"},
                {"id_gimnasio": socio["id_gimnasio"], "nombre": "Pase Libre Total", "precio": 28000, "duracion_dias": 30, "estado": "Activo"}
            ]
            try:
                res_ins = supabase.from_("plan_membresia").insert(planes_semilla).execute()
                planes_raw = res_ins.data or planes_semilla
            except Exception:
                planes_raw = planes_semilla

        planes = [p for p in planes_raw if not p.get("estado") or (p.get("estado") or "").lower() == "activo"]

    metricas = calcular_metricas_membresia(membresia)

    # Armar datos enriquecidos de membresía
    membresia_resp = None
    if membresia:
        plan_obj = membresia.get("plan_membresia") or {}
        precio_monto = float(membresia.get("precio") or plan_obj.get("precio", 0))
        membresia_resp = {
            "id_membresia": membresia["id_membresia"],
            "id_plan_membresia": membresia.get("id_plan_membresia"),
            "nombre_plan": plan_obj.get("nombre", "Plan Estándar"),
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

    return {
        "socio": socio,
        "membresia": membresia_resp,
        "metricas": metricas,
        "pagos": pagos,
        "planes_disponibles": planes,
    }


@router.get("/planes-disponibles", status_code=status.HTTP_200_OK)
async def get_planes_disponibles(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Obtener planes disponibles exclusivos del gimnasio del socio con cálculo de beneficios y upgrades."""
    socio = await obtener_socio_autenticado(current_user)
    if not socio:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Acceso no autorizado. Perfil de socio no encontrado."
        )

    # 1. Obtener la membresía actual para comparar y clasificar planes superiores
    res_memb = (
        supabase.from_("membresia")
        .select("id_membresia, id_plan_membresia, precio, estado")
        .eq("id_socio", socio["id_socio"])
        .order("fecha_vencimiento", desc=True)
        .limit(1)
        .maybe_single()
        .execute()
    )
    memb_actual = get_data(res_memb)
    plan_actual_id = memb_actual.get("id_plan_membresia") if memb_actual else None
    precio_actual = float(memb_actual.get("precio", 0) or 0) if memb_actual else 0.0

    # 2. Consultar planes de membresía que pertenecen al gimnasio del socio
    res_planes = (
        supabase.from_("plan_membresia")
        .select("*")
        .eq("id_gimnasio", socio.get("id_gimnasio"))
        .order("precio", desc=False)
        .execute()
    )
    planes = res_planes.data or []

    # Si el gimnasio aún no tiene planes cargados, sembrar 3 planes predeterminados para dicho gimnasio
    if not planes and socio.get("id_gimnasio"):
        planes_semilla = [
            {"id_gimnasio": socio["id_gimnasio"], "nombre": "Plan Básico", "precio": 15000, "duracion_dias": 30, "estado": "Activo"},
            {"id_gimnasio": socio["id_gimnasio"], "nombre": "Plan Premium", "precio": 22000, "duracion_dias": 30, "estado": "Activo"},
            {"id_gimnasio": socio["id_gimnasio"], "nombre": "Pase Libre Total", "precio": 28000, "duracion_dias": 30, "estado": "Activo"}
        ]
        try:
            res_ins = supabase.from_("plan_membresia").insert(planes_semilla).execute()
            planes = res_ins.data or planes_semilla
        except Exception:
            planes = planes_semilla

    # 3. Estructurar y clasificar planes para el frontend
    planes_formateados = []
    for plan in (planes or []):
        est = (plan.get("estado") or "").lower()
        if est and est != "activo":
            continue

        precio_num = float(plan.get("precio", 0) or 0)
        duracion = plan.get("duracion_dias", 30) or 30
        es_plan_actual = plan.get("id_plan_membresia") == plan_actual_id
        es_superior = (not es_plan_actual) and (precio_num > precio_actual)
        diferencia = precio_num - precio_actual

        planes_formateados.append({
            "id_plan_membresia": plan.get("id_plan_membresia"),
            "id_gimnasio": plan.get("id_gimnasio"),
            "nombre": plan.get("nombre"),
            "precio": precio_num,
            "precio_formateado": formatear_precio_ars(precio_num),
            "duracion_dias": duracion,
            "duracion_texto": f"{duracion} días",
            "estado": plan.get("estado"),
            "es_plan_actual": es_plan_actual,
            "es_superior": es_superior,
            "diferencia_precio": diferencia if diferencia > 0 else 0,
            "diferencia_precio_formateada": formatear_precio_ars(diferencia) if diferencia > 0 else None,
            "beneficios": sugerir_beneficios(plan.get("nombre")),
        })

    return {
        "gimnasio_id": socio.get("id_gimnasio"),
        "plan_actual_id": plan_actual_id,
        "total_planes": len(planes_formateados),
        "planes": planes_formateados,
    }


@router.post("/membresia/actualizar", status_code=status.HTTP_200_OK)
async def solicitar_actualizacion(
    req: ActualizarMembresiaSocio,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Solicitar renovación o cambio de plan de membresía."""
    socio = await obtener_socio_autenticado(current_user)
    if not socio:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Acceso no autorizado.")

    plan_id = req.id_plan or req.plan_id
    if not plan_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Debes seleccionar un plan.")

    res_plan = (
        supabase.from_("plan_membresia")
        .select("*")
        .eq("id_plan_membresia", plan_id)
        .maybe_single()
        .execute()
    )
    plan = get_data(res_plan)
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="El plan seleccionado no existe.")

    meses = req.meses or 1
    duracion_dias = (plan.get("duracion_dias") or 30) * meses
    hoy = date.today()

    # Buscar membresía actual
    res_m = (
        supabase.from_("membresia")
        .select("*")
        .eq("id_socio", socio["id_socio"])
        .order("fecha_vencimiento", desc=True)
        .limit(1)
        .maybe_single()
        .execute()
    )
    memb_actual = get_data(res_m)

    f_inicio = hoy
    if memb_actual and memb_actual.get("fecha_vencimiento"):
        fv = date.fromisoformat(str(memb_actual["fecha_vencimiento"]).split("T")[0])
        if fv > hoy:
            f_inicio = date.fromisoformat(str(memb_actual.get("fecha_inicio") or hoy).split("T")[0])
            f_venc = fv + timedelta(days=duracion_dias)
        else:
            f_venc = hoy + timedelta(days=duracion_dias)
    else:
        f_venc = hoy + timedelta(days=duracion_dias)

    precio_total = float(plan.get("precio", 0)) * meses

    if memb_actual:
        res_upd = (
            supabase.from_("membresia")
            .update({
                "id_plan_membresia": plan_id,
                "precio": precio_total,
                "fecha_inicio": f_inicio.isoformat(),
                "fecha_vencimiento": f_venc.isoformat(),
                "estado": "activo",
            })
            .eq("id_membresia", memb_actual["id_membresia"])
            .execute()
        )
        memb_resultado = res_upd.data[0]
    else:
        res_ins = (
            supabase.from_("membresia")
            .insert([{
                "id_socio": socio["id_socio"],
                "id_plan_membresia": plan_id,
                "precio": precio_total,
                "fecha_inicio": f_inicio.isoformat(),
                "fecha_vencimiento": f_venc.isoformat(),
                "estado": "activo",
            }])
            .execute()
        )
        memb_resultado = res_ins.data[0]

    return {
        "mensaje": f"Membresía actualizada con éxito al plan {plan.get('nombre')}.",
        "membresia": memb_resultado,
    }


# ==============================================================================
# INTEGRACIÓN DE PAGOS DIGITALES (MERCADO PAGO / UALÁ)
# ==============================================================================

@router.post("/pagos/iniciar", status_code=status.HTTP_200_OK)
async def iniciar_pago(
    req: IniciarPagoSocio,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Generar sesión de pago digital (Mercado Pago o Ualá)."""
    socio = await obtener_socio_autenticado(current_user)
    if not socio:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Acceso no autorizado.")

    plan_id = req.id_plan or req.plan_id
    if not plan_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Debes indicar un plan de membresía.")

    res_plan = (
        supabase.from_("plan_membresia")
        .select("*")
        .eq("id_plan_membresia", plan_id)
        .maybe_single()
        .execute()
    )
    plan = get_data(res_plan)
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan de membresía no encontrado.")

    # Gimnasio
    res_gym = (
        supabase.from_("gimnasio")
        .select("nombre")
        .eq("id_gimnasio", socio["id_gimnasio"])
        .maybe_single()
        .execute()
    )
    gym = get_data(res_gym)

    meses = req.meses or 1
    monto_total = float(plan.get("precio", 0)) * meses
    external_ref = f"GYM-{socio['id_gimnasio']}-SOC-{socio['id_socio']}-PL-{plan['id_plan_membresia']}-{int(time.time())}"

    proveedor = (req.proveedor or "mercadopago").lower()
    if proveedor == "uala":
        orden = await crear_orden_uala(
            titulo=plan.get("nombre", "Plan GymTrack"),
            monto=monto_total,
            external_reference=external_ref,
            socio=socio,
            gimnasio=gym,
        )
    else:
        orden = await crear_preferencia_mercadopago(
            titulo=plan.get("nombre", "Plan GymTrack"),
            monto=monto_total,
            external_reference=external_ref,
            socio=socio,
            gimnasio=gym,
        )

    return {
        "orden": orden,
        "plan": plan,
        "socio": socio,
    }


@router.post("/pagos/confirmar", status_code=status.HTTP_200_OK)
async def confirmar_pago(
    req: ConfirmarPagoSocio,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Confirmar acreditación de pago y actualizar vigencia de membresía."""
    socio = await obtener_socio_autenticado(current_user)
    if not socio:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Acceso no autorizado.")

    plan_id = req.id_plan or req.plan_id
    plan = None
    if plan_id:
        res_p = (
            supabase.from_("plan_membresia")
            .select("*")
            .eq("id_plan_membresia", plan_id)
            .maybe_single()
            .execute()
        )
        plan = get_data(res_p)

    monto = float(req.monto) if req.monto is not None else (float(plan.get("precio", 0)) if plan else 0.0)
    duracion_dias = int(plan.get("duracion_dias", 30)) if plan else 30
    hoy = date.today()

    # Membresía actual
    res_m = (
        supabase.from_("membresia")
        .select("*")
        .eq("id_socio", socio["id_socio"])
        .order("fecha_vencimiento", desc=True)
        .limit(1)
        .maybe_single()
        .execute()
    )
    memb_actual = get_data(res_m)

    f_inicio = hoy
    f_venc = hoy + timedelta(days=duracion_dias)

    if memb_actual and memb_actual.get("fecha_vencimiento"):
        fv = date.fromisoformat(str(memb_actual["fecha_vencimiento"]).split("T")[0])
        if fv > hoy:
            f_inicio = date.fromisoformat(str(memb_actual.get("fecha_inicio") or hoy).split("T")[0])
            f_venc = fv + timedelta(days=duracion_dias)

    id_memb_final = None
    if memb_actual:
        res_upd = (
            supabase.from_("membresia")
            .update({
                "id_plan_membresia": plan_id or memb_actual.get("id_plan_membresia"),
                "precio": monto,
                "fecha_inicio": f_inicio.isoformat(),
                "fecha_vencimiento": f_venc.isoformat(),
                "estado": "activo",
            })
            .eq("id_membresia", memb_actual["id_membresia"])
            .execute()
        )
        id_memb_final = memb_actual["id_membresia"]
    else:
        res_ins = (
            supabase.from_("membresia")
            .insert([{
                "id_socio": socio["id_socio"],
                "id_plan_membresia": plan_id,
                "precio": monto,
                "fecha_inicio": f_inicio.isoformat(),
                "fecha_vencimiento": f_venc.isoformat(),
                "estado": "activo",
            }])
            .execute()
        )
        if res_ins.data:
            id_memb_final = res_ins.data[0]["id_membresia"]

    comprobante_str = req.comprobante or req.id_pago_externo or f"PAG-{int(time.time())}"
    res_pago = (
        supabase.from_("pago")
        .insert([{
            "id_membresia": id_memb_final,
            "monto": monto,
            "fecha_pago": hoy.isoformat(),
            "metodo_pago": (req.metodo_pago or req.proveedor or "digital").lower(),
            "estado": "pagado",
            "comprobante": comprobante_str,
        }])
        .execute()
    )

    # Actualizar estado del socio a activo
    supabase.from_("socio").update({"estado": "activo"}).eq("id_socio", socio["id_socio"]).execute()

    return {
        "aprobado": True,
        "mensaje": f"Pago acreditado exitosamente. Tu plan está activo hasta el {f_venc.isoformat()}.",
        "pago": res_pago.data[0] if res_pago.data else None,
    }


@router.get("/pagos/verificar-mp", status_code=status.HTTP_200_OK)
async def verificar_pago_mercadopago(
    external_reference: Optional[str] = Query(None),
    payment_id: Optional[str] = Query(None),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Verificar estado del pago directamente en la API de Mercado Pago y acreditarlo automáticamente."""
    socio = await obtener_socio_autenticado(current_user)
    if not socio:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Acceso no autorizado.")

    if not MERCADOPAGO_ACCESS_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No hay token de Mercado Pago configurado en las variables de entorno."
        )

    url_search = ""
    if external_reference:
        encoded_ref = urllib.parse.quote(external_reference)
        url_search = f"https://api.mercadopago.com/v1/payments/search?external_reference={encoded_ref}"
    elif payment_id:
        url_search = f"https://api.mercadopago.com/v1/payments/{urllib.parse.quote(payment_id)}"
    else:
        url_search = "https://api.mercadopago.com/v1/payments/search?sort=date_created&criteria=desc&limit=10"

    headers = {"Authorization": f"Bearer {MERCADOPAGO_ACCESS_TOKEN}"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        mp_res = await client.get(url_search, headers=headers)
        if mp_res.status_code != 200:
            return {"aprobado": False, "mensaje": "No se encontró el pago en Mercado Pago."}

        mp_data = mp_res.json()

    pago_encontrado = None
    results = mp_data.get("results")
    if results and isinstance(results, list):
        if external_reference:
            pago_encontrado = next(
                (p for p in results if p.get("external_reference") == external_reference and p.get("status") == "approved"),
                results[0]
            )
        else:
            pago_encontrado = next(
                (p for p in results if p.get("status") == "approved" and f"-SOC-{socio['id_socio']}-" in str(p.get("external_reference") or "")),
                None
            )
    elif mp_data.get("id") and mp_data.get("status") == "approved":
        pago_encontrado = mp_data

    if not pago_encontrado or pago_encontrado.get("status") != "approved":
        return {
            "aprobado": False,
            "estado_mp": pago_encontrado.get("status") if pago_encontrado else "no_encontrado",
            "mensaje": "Aún no se detecta la aprobación de Mercado Pago.",
        }

    mp_id = str(pago_encontrado.get("id"))
    comprobante_str = f"MP-{mp_id}"

    # Validar si ya está registrado
    res_exist = (
        supabase.from_("pago")
        .select("id_pago, id_membresia")
        .or_(f"comprobante.eq.{comprobante_str},comprobante.eq.{mp_id}")
        .maybe_single()
        .execute()
    )
    exist_data = get_data(res_exist)
    if exist_data:
        return {
            "aprobado": True,
            "ya_registrado": True,
            "mensaje": "El pago ya se encuentra registrado y acreditado en GymTrack.",
            "pago_id": exist_data["id_pago"],
        }

    # Extraer ID de plan
    plan_id = None
    ext_ref = pago_encontrado.get("external_reference") or ""
    match = re.search(r"-PL-(\d+)-", ext_ref)
    if match:
        plan_id = int(match.group(1))

    plan = None
    if plan_id:
        res_p = supabase.from_("plan_membresia").select("*").eq("id_plan_membresia", plan_id).maybe_single().execute()
        plan = get_data(res_p)

    monto_final = float(pago_encontrado.get("transaction_amount") or (plan.get("precio", 0) if plan else 0))
    duracion_dias = int(plan.get("duracion_dias", 30)) if plan else 30
    hoy = date.today()

    # Membresía actual
    res_m = (
        supabase.from_("membresia")
        .select("*")
        .eq("id_socio", socio["id_socio"])
        .order("fecha_vencimiento", desc=True)
        .limit(1)
        .maybe_single()
        .execute()
    )
    memb_actual = get_data(res_m)

    f_inicio = hoy
    f_venc = hoy + timedelta(days=duracion_dias)

    if memb_actual and memb_actual.get("fecha_vencimiento"):
        fv = date.fromisoformat(str(memb_actual["fecha_vencimiento"]).split("T")[0])
        if fv > hoy:
            f_inicio = date.fromisoformat(str(memb_actual.get("fecha_inicio") or hoy).split("T")[0])
            f_venc = fv + timedelta(days=duracion_dias)

    id_memb_final = None
    if memb_actual:
        res_upd = (
            supabase.from_("membresia")
            .update({
                "id_plan_membresia": plan.get("id_plan_membresia") if plan else memb_actual.get("id_plan_membresia"),
                "precio": monto_final,
                "fecha_inicio": f_inicio.isoformat(),
                "fecha_vencimiento": f_venc.isoformat(),
                "estado": "activo",
            })
            .eq("id_membresia", memb_actual["id_membresia"])
            .execute()
        )
        id_memb_final = memb_actual["id_membresia"]
    else:
        res_ins = (
            supabase.from_("membresia")
            .insert([{
                "id_socio": socio["id_socio"],
                "id_plan_membresia": plan.get("id_plan_membresia") if plan else None,
                "precio": monto_final,
                "fecha_inicio": f_inicio.isoformat(),
                "fecha_vencimiento": f_venc.isoformat(),
                "estado": "activo",
            }])
            .execute()
        )
        if res_ins.data:
            id_memb_final = res_ins.data[0]["id_membresia"]

    res_pago = (
        supabase.from_("pago")
        .insert([{
            "id_membresia": id_memb_final,
            "monto": monto_final,
            "fecha_pago": hoy.isoformat(),
            "metodo_pago": "mercadopago",
            "estado": "pagado",
            "comprobante": comprobante_str,
        }])
        .execute()
    )

    supabase.from_("socio").update({"estado": "activo"}).eq("id_socio", socio["id_socio"]).execute()

    return {
        "aprobado": True,
        "mensaje": f"¡Pago de {formatear_precio_ars(monto_final)} acreditado exitosamente! Tu plan ahora es {plan.get('nombre', 'Plan Activo') if plan else 'Plan Activo'}.",
        "pago": res_pago.data[0] if res_pago.data else None,
        "membresia": {
            "id_membresia": id_memb_final,
            "nombre_plan": plan.get("nombre", "Plan Activo") if plan else "Plan Activo",
            "precio": monto_final,
            "precio_formateado": formatear_precio_ars(monto_final),
            "fecha_inicio": f_inicio.isoformat(),
            "fecha_vencimiento": f_venc.isoformat(),
            "estado": "activa",
        },
    }
