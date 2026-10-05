from datetime import datetime, date
from typing import Optional, Dict, Any

MESES_ES = ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic']


def get_data(res: Any) -> Any:
    """Extrae .data de una respuesta de Supabase de manera segura, retornando None si res es None."""
    if res is None:
        return None
    if hasattr(res, "data"):
        return res.data
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
    """Calcula métricas detalladas de vigencia, días restantes y progreso de membresía."""
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
    if dias_restantes == 0:
        tiempo_restante_texto = "Vence hoy"
    elif dias_restantes < 0:
        tiempo_restante_texto = f"Vencida hace {abs(dias_restantes)} días"
        porcentaje_transcurrido = 100
    else:
        tiempo_restante_texto = f"{dias_restantes} días"

    estado_clave = (membresia.get("estado") or "activo").lower()
    estado_etiqueta = "Activa"

    if esta_vencida or estado_clave in ("vencido", "vencida"):
        estado_clave = "vencida"
        estado_etiqueta = "Vencida"
    elif estado_clave == "pendiente":
        estado_etiqueta = "Pendiente de Pago"
    elif estado_clave in ("activo", "activa"):
        estado_clave = "activa"
        estado_etiqueta = "Activa"

    return {
        "tiene_membresia": True,
        "estado_clave": estado_clave,
        "estado_etiqueta": estado_etiqueta,
        "dias_restantes": max(0, dias_restantes),
        "dias_reales": dias_restantes,
        "tiempo_restante_texto": tiempo_restante_texto,
        "porcentaje_transcurrido": porcentaje_transcurrido,
        "esta_vencida": esta_vencida,
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

