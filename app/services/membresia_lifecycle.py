from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple


ESTADOS_MEMBRESIA_EFECTIVOS = {"pendiente", "activa", "vencida", "cancelada"}
ESTADOS_VIGENCIA_BLOQUEANTES = {"activa"}
ESTADOS_DB_ACTIVOS = {"activa", "activo", "programada", "programado"}


def parse_iso_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    raw = str(value).strip().split("T")[0]
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def calcular_estado_efectivo_membresia(
    membresia: Optional[Dict[str, Any]],
    hoy: Optional[date] = None,
) -> str:
    """
    Determina el estado real del ciclo de vida de una contratación (`public.membresia_socio`)
    sin estado intermedio 'programada':
      - 'cancelada' : dada de baja lógicamente (conserva historial y pagos).
      - 'pendiente' : esperando acreditación de pago.
      - 'vencida'   : marcada vencida o con fecha_vencimiento < hoy.
      - 'activa'    : contratación activa y no vencida.
    """
    if not isinstance(membresia, dict):
        return "sin_membresia"

    ref_hoy = hoy or date.today()
    estado_db = str(membresia.get("estado") or "").strip().lower()
    if estado_db in ("cancelada", "cancelado"):
        return "cancelada"
    if estado_db == "pendiente":
        return "pendiente"

    f_venc = parse_iso_date(membresia.get("fecha_vencimiento"))

    if estado_db in ("vencida", "vencido") or (f_venc is not None and f_venc < ref_hoy):
        return "vencida"

    if estado_db in ESTADOS_DB_ACTIVOS:
        return "activa"

    return estado_db or "pendiente"


def calcular_dias_restantes_membresia(
    membresia: Optional[Dict[str, Any]],
    hoy: Optional[date] = None,
) -> int:
    """
    Calcula los días restantes de uso de una membresía activa no vencida para transferirlos
    a una nueva contratación que la reemplaza.
    - Si inició hoy o antes (caso normal): `(fecha_vencimiento - hoy).days`.
      Ej: plan de 30 días, pasaron 27 -> vence en 3 días -> retorna 3.
    - Si existiera un contrato heredado con fecha_inicio futura: `(fecha_vencimiento - fecha_inicio).days`.
    - Si está vencida, cancelada o pendiente: retorna 0.
    """
    if not isinstance(membresia, dict):
        return 0

    ref_hoy = hoy or date.today()
    estado_db = str(membresia.get("estado") or "").strip().lower()
    if estado_db not in ESTADOS_DB_ACTIVOS:
        return 0

    f_venc = parse_iso_date(membresia.get("fecha_vencimiento"))
    if f_venc is None or f_venc < ref_hoy:
        return 0

    f_ini = parse_iso_date(membresia.get("fecha_inicio"))
    inicio_ref = max(ref_hoy, f_ini) if f_ini is not None else ref_hoy
    return max(0, (f_venc - inicio_ref).days)


def calcular_dias_restantes_acumulados(
    contratos_existentes: Iterable[Dict[str, Any]],
    *,
    excluir_id_membresia: Optional[int] = None,
    hoy: Optional[date] = None,
) -> int:
    """
    Suma los días restantes de uso de todas las membresías activas no vencidas previas
    de una asociación `socio_gimnasio` que serán canceladas al activar una nueva membresía.
    """
    ref_hoy = hoy or date.today()
    total = 0
    for c in contratos_existentes:
        if not isinstance(c, dict):
            continue
        cid = c.get("id_membresia_socio")
        if excluir_id_membresia is not None and cid is not None and int(cid) == int(excluir_id_membresia):
            continue
        total += calcular_dias_restantes_membresia(c, hoy=ref_hoy)
    return total


def enriquecer_membresia_ciclo_vida(
    membresia: Dict[str, Any],
    hoy: Optional[date] = None,
) -> Dict[str, Any]:
    """
    Devuelve una copia enriquecida de la contratación con su estado efectivo de ciclo de vida,
    preservando `estado_registrado` (valor crudo en BD).
    """
    ref_hoy = hoy or date.today()
    estado_db = str(membresia.get("estado") or "").strip().lower()
    estado_efectivo = calcular_estado_efectivo_membresia(membresia, hoy=ref_hoy)
    f_ini = parse_iso_date(membresia.get("fecha_inicio"))
    f_venc = parse_iso_date(membresia.get("fecha_vencimiento"))
    vigente_hoy = (
        estado_efectivo == "activa"
        and (f_ini is None or f_ini <= ref_hoy)
        and (f_venc is None or ref_hoy <= f_venc)
    )
    out = dict(membresia)
    out["estado_registrado"] = estado_db or estado_efectivo
    out["estado"] = estado_efectivo
    out["estado_efectivo"] = estado_efectivo
    out["es_vigente"] = vigente_hoy
    out["es_programada"] = False
    out["es_vencida"] = estado_efectivo == "vencida"
    return out


def periodos_se_superponen(
    inicio_a: date,
    fin_a: date,
    inicio_b: date,
    fin_b: date,
) -> bool:
    """Retorna True si dos intervalos cerrados [inicio, fin] comparten al menos un día."""
    return inicio_a <= fin_b and fin_a >= inicio_b


def buscar_conflicto_superposicion(
    contratos_existentes: Iterable[Dict[str, Any]],
    fecha_inicio: date,
    fecha_vencimiento: date,
    *,
    excluir_id_membresia: Optional[int] = None,
    hoy: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    """
    Verifica si [fecha_inicio, fecha_vencimiento] se superpone con alguna contratación
    activa del mismo `socio_gimnasio`.
    """
    ref_hoy = hoy or date.today()
    for c in contratos_existentes:
        if not isinstance(c, dict):
            continue
        cid = c.get("id_membresia_socio")
        if excluir_id_membresia is not None and cid is not None and int(cid) == int(excluir_id_membresia):
            continue
        est_eff = calcular_estado_efectivo_membresia(c, hoy=ref_hoy)
        if est_eff not in ESTADOS_VIGENCIA_BLOQUEANTES:
            continue
        c_ini = parse_iso_date(c.get("fecha_inicio"))
        c_fin = parse_iso_date(c.get("fecha_vencimiento"))
        if c_ini is None or c_fin is None:
            continue
        if periodos_se_superponen(fecha_inicio, fecha_vencimiento, c_ini, c_fin):
            return c
    return None


def calcular_inicio_encadenado(
    contratos_existentes: Iterable[Dict[str, Any]],
    fecha_inicio_solicitada: Optional[date] = None,
    *,
    hoy: Optional[date] = None,
) -> Tuple[date, Optional[date]]:
    """
    Mantiene la firma de compatibilidad sin programar inicios futuros:
    una nueva membresía siempre inicia en `fecha_inicio_solicitada` o `hoy`
    (los días restantes de la membresía anterior cancelada se suman al vencimiento).
    """
    ref_hoy = hoy or date.today()
    return (fecha_inicio_solicitada or ref_hoy), None


def seleccionar_membresia_vigente_y_programada(
    contratos: Iterable[Dict[str, Any]],
    hoy: Optional[date] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    A partir del historial de contrataciones de un `socio_gimnasio`, determina la contratación
    principal actual (`activa` más reciente, o `pendiente` si no hay activa).
    Ya no existen membresías programadas futuras, por lo que el segundo elemento es siempre `None`.
    """
    ref_hoy = hoy or date.today()
    enriquecidas = [
        enriquecer_membresia_ciclo_vida(c, hoy=ref_hoy)
        for c in contratos
        if isinstance(c, dict)
    ]

    activas = [c for c in enriquecidas if c["estado_efectivo"] == "activa"]
    pendientes = [c for c in enriquecidas if c["estado_efectivo"] == "pendiente"]

    activas.sort(
        key=lambda c: (
            1 if c.get("es_vigente") else 0,
            parse_iso_date(c.get("fecha_inicio")) or date.min,
            int(c.get("id_membresia_socio") or 0),
        ),
        reverse=True,
    )
    pendientes.sort(
        key=lambda c: (
            parse_iso_date(c.get("fecha_inicio")) or date.min,
            int(c.get("id_membresia_socio") or 0),
        ),
        reverse=True,
    )

    principal: Optional[Dict[str, Any]] = None
    if activas:
        principal = activas[0]
    elif pendientes:
        principal = pendientes[0]

    return principal, None
