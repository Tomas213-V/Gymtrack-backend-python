from datetime import datetime, date, timezone
from typing import Dict, Any, Optional
import math
from fastapi import APIRouter, HTTPException, status, Depends, Query, Path
from app.config import supabase
from app.core.dependencies import get_optional_user
from app.models.asistencia import AsistenciaCreate, AsistenciaCheckout

router = APIRouter(prefix="/api/asistencias", tags=["Asistencias"])


@router.get("/estadisticas", status_code=status.HTTP_200_OK)
async def get_estadisticas(
    id_gimnasio: Optional[int] = Query(None),
    usuario_auth: Optional[Dict[str, Any]] = Depends(get_optional_user)
):
    """Obtener estadísticas de asistencias comparativas con el mes anterior."""
    gym_id = id_gimnasio or (usuario_auth.get("id_gimnasio") if usuario_auth else None)

    ahora = datetime.now()
    year_actual = ahora.year
    mes_actual = ahora.month

    # Inicio y fin del mes actual
    inicio_mes_actual = datetime(year_actual, mes_actual, 1).isoformat()
    if mes_actual == 12:
        fin_mes_actual = datetime(year_actual + 1, 1, 1).isoformat()
    else:
        fin_mes_actual = datetime(year_actual, mes_actual + 1, 1).isoformat()

    # Inicio y fin del mes anterior
    if mes_actual == 1:
        inicio_mes_anterior = datetime(year_actual - 1, 12, 1).isoformat()
        fin_mes_anterior = inicio_mes_actual
    else:
        inicio_mes_anterior = datetime(year_actual, mes_actual - 1, 1).isoformat()
        fin_mes_anterior = inicio_mes_actual

    q_actual = (
        supabase.from_("asistencia")
        .select("id_asistencia, socio!inner(id_gimnasio)", count="exact")
        .gte("hora_ingreso", inicio_mes_actual)
        .lt("hora_ingreso", fin_mes_actual)
    )
    q_anterior = (
        supabase.from_("asistencia")
        .select("id_asistencia, socio!inner(id_gimnasio)", count="exact")
        .gte("hora_ingreso", inicio_mes_anterior)
        .lt("hora_ingreso", fin_mes_anterior)
    )

    if gym_id:
        q_actual = q_actual.eq("socio.id_gimnasio", gym_id)
        q_anterior = q_anterior.eq("socio.id_gimnasio", gym_id)

    res_act = q_actual.execute()
    res_ant = q_anterior.execute()

    total_mes_actual = res_act.count or 0
    total_mes_anterior = res_ant.count or 0

    porcentaje_cambio = 0
    tendencia = "neutral"

    if total_mes_anterior > 0:
        porcentaje_cambio = round(((total_mes_actual - total_mes_anterior) / total_mes_anterior) * 100)
        tendencia = "up" if porcentaje_cambio >= 0 else "down"
    elif total_mes_actual > 0:
        porcentaje_cambio = 100
        tendencia = "up"

    meses_nombres = [
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"
    ]

    return {
        "totalMesActual": total_mes_actual,
        "totalMesAnterior": total_mes_anterior,
        "porcentajeCambio": abs(porcentaje_cambio),
        "tendencia": tendencia,
        "nombreMes": meses_nombres[mes_actual - 1],
    }


@router.get("/socio/{id_socio}", status_code=status.HTTP_200_OK)
async def get_asistencias_por_socio(
    id_socio: int = Path(...),
    usuario_auth: Optional[Dict[str, Any]] = Depends(get_optional_user)
):
    """Obtener historial completo de asistencias de un socio."""
    res = (
        supabase.from_("asistencia")
        .select("*")
        .eq("id_socio", id_socio)
        .order("hora_ingreso", desc=True)
        .execute()
    )

    asistencias = res.data or []
    return {
        "id_socio": id_socio,
        "total": len(asistencias),
        "asistencias": asistencias,
    }


@router.get("", status_code=status.HTTP_200_OK)
async def get_asistencias(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1),
    search: Optional[str] = Query(None),
    desde: Optional[str] = Query(None),
    hasta: Optional[str] = Query(None),
    id_socio: Optional[str] = Query(None),
    id_gimnasio: Optional[int] = Query(None),
    usuario_auth: Optional[Dict[str, Any]] = Depends(get_optional_user)
):
    """Listado paginado de asistencias con filtros de gimnasio, socio, fecha y búsqueda."""
    gym_id = id_gimnasio or (usuario_auth.get("id_gimnasio") if usuario_auth else None)
    offset = (page - 1) * limit

    query = (
        supabase.from_("asistencia")
        .select("""
            id_asistencia,
            id_socio,
            hora_ingreso,
            hora_salida,
            estado,
            socio!inner (
                id_socio,
                nombre,
                apellido,
                dni,
                email,
                telefono,
                estado,
                id_gimnasio
            )
        """, count="exact")
    )

    if gym_id:
        query = query.eq("socio.id_gimnasio", gym_id)

    if id_socio and id_socio.lower() != "todos":
        query = query.eq("id_socio", int(id_socio))

    if desde:
        f_desde = desde if "T" in desde else f"{desde}T00:00:00.000Z"
        query = query.gte("hora_ingreso", f_desde)
    if hasta:
        f_hasta = hasta if "T" in hasta else f"{hasta}T23:59:59.999Z"
        query = query.lte("hora_ingreso", f_hasta)

    if search and search.strip():
        s = search.strip()
        query = query.or_(f"nombre.ilike.%{s}%,apellido.ilike.%{s}%,dni.ilike.%{s}%", foreign_table="socio")

    query = query.order("hora_ingreso", desc=True).range(offset, offset + limit - 1)
    res = query.execute()

    count = res.count or 0
    return {
        "total": count,
        "page": page,
        "totalPages": math.ceil(count / limit) if limit else 1,
        "limit": limit,
        "asistencias": res.data or [],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def registrar_asistencia(
    req: AsistenciaCreate,
    usuario_auth: Optional[Dict[str, Any]] = Depends(get_optional_user)
):
    """Registrar asistencia para un socio mediante ID o DNI."""
    gym_id = req.id_gimnasio or (usuario_auth.get("id_gimnasio") if usuario_auth else None)

    if not req.id_socio and not req.dni:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes proporcionar el ID o el DNI del socio."
        )

    socio_query = (
        supabase.from_("socio")
        .select("""
            id_socio,
            nombre,
            apellido,
            dni,
            email,
            telefono,
            estado,
            membresia (
                id_membresia,
                estado,
                fecha_inicio,
                fecha_vencimiento,
                precio
            )
        """)
    )

    if req.id_socio:
        socio_query = socio_query.eq("id_socio", int(req.id_socio))
    else:
        socio_query = socio_query.eq("dni", str(req.dni).strip())

    if gym_id:
        socio_query = socio_query.eq("id_gimnasio", gym_id)

    res_socio = socio_query.maybe_single().execute()
    socio = res_socio.data

    if not socio:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No se encontró ningún socio con el identificador ingresado."
        )

    hora_ingreso = req.hora_ingreso or datetime.now(timezone.utc).isoformat()

    asist_data = {
        "id_socio": socio["id_socio"],
        "hora_ingreso": hora_ingreso,
        "estado": req.estado or "presente",
    }

    res_ins = supabase.from_("asistencia").insert([asist_data]).execute()
    if not res_ins.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al registrar la asistencia.")

    nueva_asistencia = res_ins.data[0]
    asistencia_completa = {
        **nueva_asistencia,
        "socio": socio,
    }

    return {
        "mensaje": f"Asistencia registrada con éxito para {socio.get('nombre')} {socio.get('apellido')}.",
        "asistencia": asistencia_completa,
    }


@router.patch("/{id}/checkout", status_code=status.HTTP_200_OK)
async def checkout_asistencia(
    id: int = Path(...),
    req: AsistenciaCheckout = ...,
    usuario_auth: Optional[Dict[str, Any]] = Depends(get_optional_user)
):
    """Registrar salida (checkout) de una asistencia."""
    gym_id = usuario_auth.get("id_gimnasio") if usuario_auth else None

    query = supabase.from_("asistencia").select("*, socio!inner(id_gimnasio)").eq("id_asistencia", id)
    if gym_id:
        query = query.eq("socio.id_gimnasio", gym_id)

    res = query.maybe_single().execute()
    asistencia = res.data

    if not asistencia:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="La asistencia no existe o no pertenece a este gimnasio."
        )

    estado_actual = (asistencia.get("estado") or "").lower()
    if estado_actual not in ("presente", "registrada"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No se puede registrar la salida. El estado actual de la asistencia es '{estado_actual}'."
        )

    nuevo_estado = req.estado or "completado"
    nueva_hora_salida = req.hora_salida or datetime.now(timezone.utc).isoformat()

    res_upd = (
        supabase.from_("asistencia")
        .update({"estado": nuevo_estado, "hora_salida": nueva_hora_salida})
        .eq("id_asistencia", id)
        .execute()
    )
    if not res_upd.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al registrar la salida.")

    return {
        "mensaje": "Salida registrada correctamente.",
        "asistencia": res_upd.data[0],
    }


@router.delete("/{id}", status_code=status.HTTP_200_OK)
async def eliminar_asistencia(
    id: int = Path(...),
    usuario_auth: Optional[Dict[str, Any]] = Depends(get_optional_user)
):
    """Eliminar un registro de asistencia."""
    res = supabase.from_("asistencia").delete().eq("id_asistencia", id).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="La asistencia no existe o ya fue eliminada.")

    return {"mensaje": "Asistencia eliminada correctamente."}
