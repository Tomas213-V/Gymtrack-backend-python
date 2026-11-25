from datetime import datetime, date
from typing import Optional, Dict, Any, Tuple
from fastapi import HTTPException, status

MESES_ES = ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic']


def map_postgrest_error(exc: Exception) -> Tuple[int, str]:
    """
    Mapeo centralizado de excepciones `postgrest.exceptions.APIError` y errores de PostgreSQL/RPC
    hacia códigos HTTP semánticos (409, 403, 404, 400, 502):
      - 23505 (unique_violation) -> 409 Conflict
      - 42501 (insufficient_privilege / RLS) -> 403 Forbidden
      - 23503 (foreign_key_violation) -> 409 si RESTRICT/en uso, si no 400 Bad Request
      - 23514 (check_violation) / 23502 (not_null) / 22P02 (invalid_text_representation) -> 400 Bad Request
      - PGRST200 / PGRST202 / PGRST204 (error de esquema/relación PostgREST) -> 502 Bad Gateway
      - P0001 (raise exception de triggers/RPCs) -> inferido según mensaje (409/403/404/400)
    """
    code = str(getattr(exc, "code", None) or "").strip().upper()
    message = str(getattr(exc, "message", None) or str(exc) or "").strip()
    details = str(getattr(exc, "details", None) or "").strip()
    combined = f"{message} {details}".strip().lower()

    if code == "23505":
        return status.HTTP_409_CONFLICT, message or "Conflicto de unicidad: el registro ya existe."
    if code == "42501":
        return status.HTTP_403_FORBIDDEN, message or "Permiso denegado por las políticas de seguridad de la base de datos."
    if code == "23503":
        if any(k in combined for k in ("still referenced", "restrict", "delete")):
            return status.HTTP_409_CONFLICT, message or "No se puede completar la operación porque el registro está referenciado por historial."
        return status.HTTP_400_BAD_REQUEST, message or "Referencia inválida: la entidad relacionada no existe o pertenece a otro gimnasio."
    if code in ("23514", "23502", "22P02", "22001", "22003", "22007"):
        return status.HTTP_400_BAD_REQUEST, message or "Los datos enviados violan una restricción de validación del esquema."
    if code in ("PGRST200", "PGRST201", "PGRST202", "PGRST204"):
        return status.HTTP_502_BAD_GATEWAY, f"Error de compatibilidad de consulta PostgREST ({code}): {message}"
    if code == "PGRST116":
        return status.HTTP_404_NOT_FOUND, message or "Recurso no encontrado."

    # Excepciones de negocio (`P0001` u otros raise exception de triggers/RPCs)
    if any(
        k in combined
        for k in (
            "ya existe",
            "ya se encuentra",
            "duplicad",
            "conflicto",
            "superpuest",
            "límite máximo",
            "limite maximo",
            "ya fue anulada",
            "ya finalizada",
            "transición inválida",
            "transicion invalida",
            "inmutable",
            "ingreso en curso",
        )
    ):
        return status.HTTP_409_CONFLICT, message or "Conflicto de estado o duplicidad."
    if any(
        k in combined
        for k in (
            "no tienes autorización",
            "no tienes permisos",
            "sin permisos",
            "no autorizado",
            "no autorizados",
            "no está permitido transferir",
            "no esta permitido transferir",
            "no se puede reasignar la rutina",
            "row-level security",
            "permission denied",
        )
    ):
        return status.HTTP_403_FORBIDDEN, message or "No tienes autorización para realizar esta operación."
    if any(k in combined for k in ("no existe", "no encontrad", "no se encontró", "not found")):
        return status.HTTP_404_NOT_FOUND, message or "Recurso no encontrado."

    return status.HTTP_400_BAD_REQUEST, message or "No se pudo completar la operación en la base de datos."


def raise_http_from_postgrest_error(exc: Exception, prefix: Optional[str] = None) -> None:
    """Convierte una excepción de PostgREST/RPC en un HTTPException con código semántico."""
    http_status, msg = map_postgrest_error(exc)
    detail = f"{prefix}: {msg}" if prefix else msg
    raise HTTPException(status_code=http_status, detail=detail) from exc


def get_data(res: Any) -> Any:
    """Extrae .data de una respuesta de Supabase de manera segura, retornando None si res es None."""
    if res is None:
        return None
    if hasattr(res, "data"):
        return res.data
    return None


def insert_gimnasio_with_rls(
    db: Any,
    payload: Dict[str, Any],
    columns: str = "id_gimnasio, nombre, telefono, email, fecha_registro, estado, id_dueno",
) -> Optional[Dict[str, Any]]:
    """
    Inserta un registro en `public.gimnasio` respetando estrictamente RLS del dueño.

    Usa `returning="minimal"` en PostgREST para evitar `INSERT ... RETURNING *` dentro de la
    misma sentencia SQL: la política `gimnasio_select_authorized` evalúa `fn_dueno_posee_gimnasio`
    (función `STABLE` que consulta `public.gimnasio` con el snapshot previo al INSERT).
    Tras completar el INSERT, recupera la fila recién creada en un segundo `SELECT`, donde
    el registro ya está confirmado y es visible para `fn_dueno_posee_gimnasio`.
    """
    table_builder = db.from_("gimnasio")
    try:
        res_ins = table_builder.insert([payload], returning="minimal").execute()
    except TypeError:
        res_ins = table_builder.insert([payload]).execute()

    rows = get_data(res_ins)
    if isinstance(rows, list) and rows:
        return rows[0]

    sel_query = (
        db.from_("gimnasio")
        .select(columns)
        .eq("id_dueno", payload["id_dueno"])
        .eq("nombre", payload["nombre"])
        .order("id_gimnasio", desc=True)
        .limit(1)
    )
    res_sel = sel_query.execute()
    sel_rows = get_data(res_sel)
    if isinstance(sel_rows, list) and sel_rows:
        return sel_rows[0]
    return None


def formatear_fecha_espanol(fecha_str: Optional[str]) -> str:
    """Formatea una fecha 'YYYY-MM-DD' o ISO a formato amigable: '15 Dic 2026'."""
    if not fecha_str:
        return 'Sin fecha'
    try:
        raw = str(fecha_str).split('T')[0]
        parts = raw.split('-')
        if len(parts) == 3:
            anio = parts[0]
            mes_index = int(parts[1]) - 1
            dia = int(parts[2])
            mes_nom = MESES_ES[mes_index] if 0 <= mes_index < len(MESES_ES) else parts[1]
            return f"{dia} {mes_nom} {anio}"
        dt = datetime.fromisoformat(str(fecha_str))
        return f"{dt.day} {MESES_ES[dt.month - 1]} {dt.year}"
    except Exception:
        return str(fecha_str)


def formatear_precio_ars(monto: Any) -> str:
    """Formatea un número a moneda argentina, ej: 12500 -> '$12.500'."""
    if monto is None:
        return '$0'
    try:
        val = int(round(float(monto)))
        # Formato con punto como separador de miles
        formatted = f"{val:,}".replace(",", ".")
        return f"${formatted}"
    except Exception:
        return '$0'


def calcular_metricas_membresia(membresia: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Calcula métricas detalladas de vigencia, días restantes y porcentaje transcurrido de membresía."""
    if not membresia or not membresia.get("fecha_vencimiento"):
        return {
            "tiene_membresia": False,
            "estado_clave": "sin_membresia",
            "estado_etiqueta": "Sin Membresía",
            "dias_restantes": 0,
            "tiempo_restante_texto": "0 días",
            "porcentaje_transcurrido": 0,
            "esta_vencida": True,
            "fecha_inicio_formateada": "-",
            "fecha_vencimiento_formateada": "-"
        }

    hoy = date.today()
    raw_venc = str(membresia["fecha_vencimiento"]).split("T")[0]
    f_venc = date.fromisoformat(raw_venc)

    raw_ini = str(membresia.get("fecha_inicio") or "").split("T")[0]
    if raw_ini:
        f_ini = date.fromisoformat(raw_ini)
    else:
        f_ini = date.fromordinal(f_venc.toordinal() - 30)

    dias_restantes = (f_venc - hoy).days

    duracion_total_dias = max(1, (f_venc - f_ini).days)
    dias_usados = max(0, (hoy - f_ini).days)
    porcentaje_transcurrido = min(100, max(0, round((dias_usados / duracion_total_dias) * 100)))

    esta_vencida = dias_restantes < 0
    es_futura = f_ini > hoy

    est_raw = str(membresia.get("estado") or "activa").strip().lower()
    estado_clave = est_raw
    estado_etiqueta = "Activa"

    if est_raw in ("cancelada", "cancelado"):
        estado_clave = "cancelada"
        estado_etiqueta = "Cancelada"
        tiempo_restante_texto = "Cancelada"
    elif est_raw == "pendiente":
        estado_clave = "pendiente"
        estado_etiqueta = "Pendiente de Pago"
        if es_futura:
            dias_para_inicio = (f_ini - hoy).days
            tiempo_restante_texto = f"Inicia en {dias_para_inicio} días"
        elif dias_restantes <= 0:
            tiempo_restante_texto = "Pendiente de acreditación"
        else:
            tiempo_restante_texto = f"{dias_restantes} días"
    elif esta_vencida or est_raw in ("vencido", "vencida"):
        estado_clave = "vencida"
        estado_etiqueta = "Vencida"
        tiempo_restante_texto = f"Vencida hace {abs(dias_restantes)} días" if dias_restantes < 0 else "Vencida"
        porcentaje_transcurrido = 100
    else:
        estado_clave = "activa"
        estado_etiqueta = "Activa"
        if dias_restantes == 0:
            tiempo_restante_texto = "Vence hoy"
        else:
            tiempo_restante_texto = f"{dias_restantes} días"

    return {
        "tiene_membresia": estado_clave in ("activa", "pendiente"),
        "es_vigente": estado_clave == "activa" and not es_futura,
        "es_programada": False,
        "estado_clave": estado_clave,
        "estado_etiqueta": estado_etiqueta,
        "dias_restantes": max(0, dias_restantes),
        "dias_reales": dias_restantes,
        "tiempo_restante_texto": tiempo_restante_texto,
        "porcentaje_transcurrido": porcentaje_transcurrido,
        "esta_vencida": esta_vencida or estado_clave == "vencida",
        "fecha_inicio_formateada": formatear_fecha_espanol(membresia.get("fecha_inicio")),
        "fecha_vencimiento_formateada": formatear_fecha_espanol(membresia.get("fecha_vencimiento"))
    }


def sugerir_beneficios(nombre_plan: Optional[str]) -> list:
    """Beneficios sugeridos por nivel de plan para enriquecer la experiencia visual del socio."""
    n = (nombre_plan or "").lower()
    if any(k in n for k in ["libre", "total", "vip"]):
        return [
            "Acceso ilimitado todos los días y horarios",
            "Acceso libre a sala de musculación y cardio",
            "Todas las clases grupales incluidas",
            "Plan de entrenamiento personalizado y seguimiento",
            "Acceso a vestuarios y casilleros premium",
        ]
    if "premium" in n:
        return [
            "Acceso completo de lunes a sábado",
            "Sala de musculación y máquinas guiadas",
            "Hasta 3 clases grupales por semana",
            "Rutina personalizada inicial",
            "Descuentos en suplementos y merchandising",
        ]
    return [
        "Acceso de lunes a viernes",
        "Sala de musculación y peso libre",
        "Evaluación inicial con profesor",
        "App de seguimiento de asistencias",
    ]

