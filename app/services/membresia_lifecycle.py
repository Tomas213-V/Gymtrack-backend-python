from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple


ESTADOS_MEMBRESIA_EFECTIVOS = {"pendiente", "programada", "activa", "vencida", "cancelada"}
ESTADOS_VIGENCIA_BLOQUEANTES = {"activa", "programada"}


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
    a partir de su estado almacenado y su ventana [fecha_inicio, fecha_vencimiento]:
      - 'cancelada'  : dada de baja lógicamente (conserva historial y pagos).
      - 'pendiente'  : esperando acreditación de pago.
      - 'vencida'    : marcada vencida o con fecha_vencimiento < hoy.
      - 'programada' : acreditada/activa con fecha_inicio > hoy (renovación futura).
      - 'activa'     : vigente hoy (fecha_inicio <= hoy <= fecha_vencimiento).
    """
    if not isinstance(membresia, dict):
        return "sin_membresia"

    ref_hoy = hoy or date.today()
    estado_db = str(membresia.get("estado") or "").strip().lower()
    if estado_db in ("cancelada", "cancelado"):
        return "cancelada"
    if estado_db == "pendiente":
        return "pendiente"

    f_ini = parse_iso_date(membresia.get("fecha_inicio"))
    f_venc = parse_iso_date(membresia.get("fecha_vencimiento"))

    if estado_db in ("vencida", "vencido") or (f_venc is not None and f_venc < ref_hoy):
        return "vencida"

    if estado_db in ("activa", "activo", "programada", "programado"):
        if f_ini is not None and f_ini > ref_hoy:
            return "programada"
        return "activa"

    return estado_db or "pendiente"


def enriquecer_membresia_ciclo_vida(
    membresia: Dict[str, Any],
    hoy: Optional[date] = None,
) -> Dict[str, Any]:
    """
    Devuelve una copia enriquecida de la contratación con su estado efectivo de ciclo de vida,
    preservando `estado_registrado` (valor crudo en BD) y normalizando `estado` para que
    ninguna contratación futura figure como `activa` ni ninguna vencida figure vigente.
    """
    ref_hoy = hoy or date.today()
    estado_db = str(membresia.get("estado") or "").strip().lower()
    estado_efectivo = calcular_estado_efectivo_membresia(membresia, hoy=ref_hoy)
    out = dict(membresia)
    out["estado_registrado"] = estado_db or estado_efectivo
    out["estado"] = estado_efectivo
    out["estado_efectivo"] = estado_efectivo
    out["es_vigente"] = estado_efectivo == "activa"
    out["es_programada"] = estado_efectivo == "programada"
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
    vigente o programada (`activa` / `programada`) del mismo `socio_gimnasio`.
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
    Calcula la fecha de inicio encadenada para una contratación o renovación:
    - Si existen contrataciones vigentes o programadas (`activa`/`programada`, no vencidas),
      toma `max(fecha_vencimiento)` y, si `fecha_inicio_solicitada <= max_vencimiento`,
      desplaza el inicio a `max_vencimiento + 1 día` conservando todos los días restantes.
    - Si no existen contrataciones vigentes/programadas, usa `fecha_inicio_solicitada` o `hoy`.
    """
    ref_hoy = hoy or date.today()
    base_solicitada = fecha_inicio_solicitada or ref_hoy

    vencimientos_vigentes: List[date] = []
    for c in contratos_existentes:
        if not isinstance(c, dict):
            continue
        est_eff = calcular_estado_efectivo_membresia(c, hoy=ref_hoy)
        if est_eff not in ESTADOS_VIGENCIA_BLOQUEANTES:
            continue
        c_fin = parse_iso_date(c.get("fecha_vencimiento"))
        if c_fin is not None and c_fin >= ref_hoy:
            vencimientos_vigentes.append(c_fin)

    max_venc = max(vencimientos_vigentes) if vencimientos_vigentes else None
    if max_venc is not None and base_solicitada <= max_venc:
        return max_venc + timedelta(days=1), max_venc

    return base_solicitada, max_venc


def seleccionar_membresia_vigente_y_programada(
    contratos: Iterable[Dict[str, Any]],
    hoy: Optional[date] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    A partir del historial de contrataciones de un `socio_gimnasio`, determina:
      1. La contratación principal actual:
         - Prioridad 1: vigente hoy (`estado_efectivo == 'activa'`).
         - Prioridad 2: próxima programada (`estado_efectivo == 'programada'`, inicio más próximo).
         - Prioridad 3: pendiente de acreditación (`estado_efectivo == 'pendiente'`).
         - Si no hay ninguna: None.
      2. La próxima renovación programada futura (`estado_efectivo == 'programada'`) distinta
         de la contratación principal (si existe).
    """
    ref_hoy = hoy or date.today()
    enriquecidas = [
        enriquecer_membresia_ciclo_vida(c, hoy=ref_hoy)
        for c in contratos
        if isinstance(c, dict)
    ]

    activas = [c for c in enriquecidas if c["estado_efectivo"] == "activa"]
    programadas = [c for c in enriquecidas if c["estado_efectivo"] == "programada"]
    pendientes = [c for c in enriquecidas if c["estado_efectivo"] == "pendiente"]

    activas.sort(
        key=lambda c: (
            parse_iso_date(c.get("fecha_inicio")) or date.min,
            int(c.get("id_membresia_socio") or 0),
        ),
        reverse=True,
    )
    programadas.sort(
        key=lambda c: (
            parse_iso_date(c.get("fecha_inicio")) or date.max,
            int(c.get("id_membresia_socio") or 0),
        )
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
    elif programadas:
        principal = programadas[0]
    elif pendientes:
        principal = pendientes[0]

    principal_id = principal.get("id_membresia_socio") if principal else None
    proxima_programada = next(
        (p for p in programadas if p.get("id_membresia_socio") != principal_id),
        None,
    )
    return principal, proxima_programada
