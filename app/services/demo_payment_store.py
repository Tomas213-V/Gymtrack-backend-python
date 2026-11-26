import threading
from typing import Any, Dict, List, Optional, Tuple

import app.config as app_config
from app.services.membresia_lifecycle import ESTADOS_DB_ACTIVOS

_DEMO_SIM_LOCK = threading.Lock()
_DEMO_SIM_MEMBRESIAS: List[Dict[str, Any]] = []
_DEMO_SIM_PAGOS: List[Dict[str, Any]] = []
_DEMO_SIM_SEQ = 900000


def reset_demo_simulated_payments_store() -> None:
    """Limpia las simulaciones en memoria (útil en tests)."""
    with _DEMO_SIM_LOCK:
        _DEMO_SIM_MEMBRESIAS.clear()
        _DEMO_SIM_PAGOS.clear()


def next_demo_sim_ids() -> Tuple[int, int]:
    """Genera IDs únicos negativos/altos en memoria para membresía y pago simulados."""
    global _DEMO_SIM_SEQ
    with _DEMO_SIM_LOCK:
        _DEMO_SIM_SEQ += 1
        ms_id = _DEMO_SIM_SEQ
        _DEMO_SIM_SEQ += 1
        pago_id = _DEMO_SIM_SEQ
        return ms_id, pago_id


def registrar_demo_simulacion(
    *,
    own_socio_id: int,
    plan_gym_id: int,
    sg_id: int,
    sim_ms: Dict[str, Any],
    sim_pago: Dict[str, Any],
) -> None:
    """Registra una membresía activa y pago aprobado simulados en memoria para modo presentación."""
    with _DEMO_SIM_LOCK:
        for m in _DEMO_SIM_MEMBRESIAS:
            if (
                int(m.get("id_gimnasio") or 0) == int(plan_gym_id)
                and int(m.get("id_socio_gimnasio") or 0) == int(sg_id)
                and str(m.get("estado") or "").strip().lower() in ESTADOS_DB_ACTIVOS
            ):
                m["estado"] = "cancelada"
        _DEMO_SIM_MEMBRESIAS.insert(0, dict(sim_ms))
        _DEMO_SIM_PAGOS.insert(0, dict(sim_pago))


def list_demo_membresias_para(
    *,
    gym_id: Optional[int] = None,
    sg_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    if app_config.has_service_role():
        return []
    with _DEMO_SIM_LOCK:
        out: List[Dict[str, Any]] = []
        for m in _DEMO_SIM_MEMBRESIAS:
            if gym_id is not None and int(m.get("id_gimnasio") or 0) != int(gym_id):
                continue
            if sg_id is not None and int(m.get("id_socio_gimnasio") or 0) != int(sg_id):
                continue
            out.append(dict(m))
        return out


def merge_demo_membresias(
    rows: Optional[List[Dict[str, Any]]],
    *,
    gym_id: Optional[int] = None,
    sg_id: Optional[int] = None,
    sg_ids: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    """
    Combina contrataciones de BD con contrataciones simuladas en memoria cuando no existe
    `SUPABASE_SERVICE_ROLE_KEY`. Si existe una simulación activa para una asociación
    `(id_socio_gimnasio, id_gimnasio)`, refleja como canceladas las activas previas de BD.
    """
    base_list = [dict(r) for r in (rows or []) if isinstance(r, dict)]
    if app_config.has_service_role():
        return base_list

    allowed_sgs = set(int(x) for x in sg_ids) if sg_ids is not None else None
    with _DEMO_SIM_LOCK:
        sim_list = []
        for m in _DEMO_SIM_MEMBRESIAS:
            m_gym = int(m.get("id_gimnasio") or 0)
            m_sg = int(m.get("id_socio_gimnasio") or 0)
            if gym_id is not None and m_gym != int(gym_id):
                continue
            if sg_id is not None and m_sg != int(sg_id):
                continue
            if allowed_sgs is not None and m_sg not in allowed_sgs:
                continue
            sim_list.append(dict(m))

    if not sim_list:
        return base_list

    active_sim_keys = {
        (int(m.get("id_socio_gimnasio") or 0), int(m.get("id_gimnasio") or 0))
        for m in sim_list
        if str(m.get("estado") or "").strip().lower() in ESTADOS_DB_ACTIVOS
    }
    for r in base_list:
        key = (int(r.get("id_socio_gimnasio") or 0), int(r.get("id_gimnasio") or 0))
        if key in active_sim_keys and str(r.get("estado") or "").strip().lower() in ESTADOS_DB_ACTIVOS:
            r["estado"] = "cancelada"

    return sim_list + base_list


def merge_demo_pagos(
    rows: Optional[List[Dict[str, Any]]],
    *,
    gym_id: Optional[int] = None,
    socio_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Combina pagos de BD con pagos simulados en memoria cuando no existe `service_role`."""
    base_list = [dict(r) for r in (rows or []) if isinstance(r, dict)]
    if app_config.has_service_role():
        return base_list

    with _DEMO_SIM_LOCK:
        sim_list = []
        for p in _DEMO_SIM_PAGOS:
            ms = p.get("membresia_socio") if isinstance(p.get("membresia_socio"), dict) else {}
            sg = ms.get("socio_gimnasio") if isinstance(ms.get("socio_gimnasio"), dict) else {}
            p_gym = ms.get("id_gimnasio")
            p_soc = sg.get("id_socio")
            if gym_id is not None and int(p_gym or 0) != int(gym_id):
                continue
            if socio_id is not None and int(p_soc or 0) != int(socio_id):
                continue
            sim_list.append(dict(p))

    if not sim_list:
        return base_list

    return sim_list + base_list


def get_demo_membresia_by_id(id_membresia_socio: int) -> Optional[Dict[str, Any]]:
    if app_config.has_service_role():
        return None
    with _DEMO_SIM_LOCK:
        for m in _DEMO_SIM_MEMBRESIAS:
            if int(m.get("id_membresia_socio") or 0) == int(id_membresia_socio):
                return dict(m)
    return None


def update_demo_membresia(
    id_membresia_socio: int,
    *,
    estado: Optional[str] = None,
    fecha_vencimiento: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if app_config.has_service_role():
        return None
    with _DEMO_SIM_LOCK:
        for m in _DEMO_SIM_MEMBRESIAS:
            if int(m.get("id_membresia_socio") or 0) == int(id_membresia_socio):
                if estado is not None:
                    m["estado"] = str(estado).strip().lower()
                if fecha_vencimiento is not None:
                    m["fecha_vencimiento"] = fecha_vencimiento
                return dict(m)
    return None


def get_demo_pago_by_id(id_pago: int) -> Optional[Dict[str, Any]]:
    if app_config.has_service_role():
        return None
    with _DEMO_SIM_LOCK:
        for p in _DEMO_SIM_PAGOS:
            if int(p.get("id_pago") or 0) == int(id_pago):
                return dict(p)
    return None
