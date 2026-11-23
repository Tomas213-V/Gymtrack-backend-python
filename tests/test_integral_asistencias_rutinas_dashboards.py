import copy
import unittest
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

import main as main_module
from app.core.security import verify_supabase_access_token


class FakeSupabaseResponse:
    def __init__(self, data: Any = None, count: Optional[int] = None):
        self.data = data
        self.count = count


class FakeIntegralQueryBuilder:
    def __init__(self, table_name: str, state: Dict[str, List[Dict[str, Any]]]):
        self.table_name = table_name
        self.state = state
        self.filters_eq: Dict[str, Any] = {}
        self.filters_in: Dict[str, List[Any]] = {}
        self.is_single = False
        self._update_payload: Optional[Dict[str, Any]] = None
        self._insert_payload: Optional[List[Dict[str, Any]]] = None
        self._delete_requested = False

    def select(self, *_args, **_kwargs) -> "FakeIntegralQueryBuilder":
        return self

    def eq(self, col: str, val: Any) -> "FakeIntegralQueryBuilder":
        self.filters_eq[col] = val
        return self

    def in_(self, col: str, vals: List[Any]) -> "FakeIntegralQueryBuilder":
        self.filters_in[col] = list(vals)
        return self

    def order(self, *_args, **_kwargs) -> "FakeIntegralQueryBuilder":
        return self

    def maybe_single(self) -> "FakeIntegralQueryBuilder":
        self.is_single = True
        return self

    def single(self) -> "FakeIntegralQueryBuilder":
        self.is_single = True
        return self

    def update(self, payload: Dict[str, Any]) -> "FakeIntegralQueryBuilder":
        self._update_payload = payload
        return self

    def insert(self, rows: List[Dict[str, Any]]) -> "FakeIntegralQueryBuilder":
        self._insert_payload = rows
        return self

    def delete(self) -> "FakeIntegralQueryBuilder":
        self._delete_requested = True
        return self

    def _enrich_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        enriched = copy.deepcopy(row)
        gyms_by_id = {int(g["id_gimnasio"]): g for g in self.state.get("gimnasio", [])}
        sucs_by_id = {int(s["id_sucursal"]): s for s in self.state.get("sucursal", [])}
        socios_by_id = {int(s["id_socio"]): s for s in self.state.get("socio", [])}
        sgs_by_id = {int(sg["id_socio_gimnasio"]): sg for sg in self.state.get("socio_gimnasio", [])}
        emps_by_id = {int(e["id_empleado"]): e for e in self.state.get("empleado", [])}
        planes_by_id = {int(p["id_membresia_gimnasio"]): p for p in self.state.get("membresia_gimnasio", [])}
        membs_by_id = {int(m["id_membresia_socio"]): m for m in self.state.get("membresia_socio", [])}
        ej_socio_by_id = {int(e["id_ejercicio_socio"]): e for e in self.state.get("ejercicio_socio", [])}
        ej_gym_by_id = {int(e["id_ejercicio_gimnasio"]): e for e in self.state.get("ejercicio_gimnasio", [])}
        rg_by_id = {int(r["id_rutina_gimnasio"]): r for r in self.state.get("rutina_gimnasio", [])}

        if self.table_name == "administrador":
            aid = int(enriched.get("id_administrador") or 0)
            ags = []
            for ag in self.state.get("administrador_gimnasio", []):
                if int(ag.get("id_administrador") or 0) == aid:
                    ag_copy = copy.deepcopy(ag)
                    gid = int(ag_copy.get("id_gimnasio") or 0)
                    if gid in gyms_by_id:
                        ag_copy["gimnasio"] = copy.deepcopy(gyms_by_id[gid])
                    ags.append(ag_copy)
            enriched["administrador_gimnasio"] = ags

        elif self.table_name == "empleado":
            eid = int(enriched.get("id_empleado") or 0)
            ess = []
            for es in self.state.get("empleado_sucursal", []):
                if int(es.get("id_empleado") or 0) == eid:
                    es_copy = copy.deepcopy(es)
                    sid = int(es_copy.get("id_sucursal") or 0)
                    if sid in sucs_by_id:
                        suc_copy = copy.deepcopy(sucs_by_id[sid])
                        gid = int(suc_copy.get("id_gimnasio") or 0)
                        if gid in gyms_by_id:
                            suc_copy["gimnasio"] = copy.deepcopy(gyms_by_id[gid])
                        es_copy["sucursal"] = suc_copy
                    ess.append(es_copy)
            enriched["empleado_sucursal"] = ess

        elif self.table_name == "socio":
            sid = int(enriched.get("id_socio") or 0)
            sgs = []
            for sg in self.state.get("socio_gimnasio", []):
                if int(sg.get("id_socio") or 0) == sid:
                    sg_copy = copy.deepcopy(sg)
                    gid = int(sg_copy.get("id_gimnasio") or 0)
                    if gid in gyms_by_id:
                        sg_copy["gimnasio"] = copy.deepcopy(gyms_by_id[gid])
                    sg_copy["membresia_socio"] = [
                        copy.deepcopy(m)
                        for m in self.state.get("membresia_socio", [])
                        if int(m.get("id_socio_gimnasio") or 0) == int(sg["id_socio_gimnasio"])
                    ]
                    sgs.append(sg_copy)
            enriched["socio_gimnasio"] = sgs

        elif self.table_name == "socio_gimnasio":
            sid = int(enriched.get("id_socio") or 0)
            gid = int(enriched.get("id_gimnasio") or 0)
            if sid in socios_by_id:
                enriched["socio"] = copy.deepcopy(socios_by_id[sid])
            if gid in gyms_by_id:
                enriched["gimnasio"] = copy.deepcopy(gyms_by_id[gid])

        elif self.table_name == "membresia_socio":
            sg_id = int(enriched.get("id_socio_gimnasio") or 0)
            plan_id = int(enriched.get("id_membresia_gimnasio") or 0)
            if sg_id in sgs_by_id:
                sg_copy = copy.deepcopy(sgs_by_id[sg_id])
                sid = int(sg_copy.get("id_socio") or 0)
                if sid in socios_by_id:
                    sg_copy["socio"] = copy.deepcopy(socios_by_id[sid])
                enriched["socio_gimnasio"] = sg_copy
            if plan_id in planes_by_id:
                enriched["membresia_gimnasio"] = copy.deepcopy(planes_by_id[plan_id])

        elif self.table_name == "pago":
            ms_id = int(enriched.get("id_membresia_socio") or 0)
            if ms_id in membs_by_id:
                ms_copy = copy.deepcopy(membs_by_id[ms_id])
                sg_id = int(ms_copy.get("id_socio_gimnasio") or 0)
                plan_id = int(ms_copy.get("id_membresia_gimnasio") or 0)
                if sg_id in sgs_by_id:
                    sg_copy = copy.deepcopy(sgs_by_id[sg_id])
                    sid = int(sg_copy.get("id_socio") or 0)
                    if sid in socios_by_id:
                        sg_copy["socio"] = copy.deepcopy(socios_by_id[sid])
                    ms_copy["socio_gimnasio"] = sg_copy
                if plan_id in planes_by_id:
                    ms_copy["membresia_gimnasio"] = copy.deepcopy(planes_by_id[plan_id])
                enriched["membresia_socio"] = ms_copy

        elif self.table_name == "asistencia_socio":
            sid = int(enriched.get("id_socio") or 0)
            suc_id = int(enriched.get("id_sucursal") or 0)
            if sid in socios_by_id:
                enriched["socio"] = copy.deepcopy(socios_by_id[sid])
            if suc_id in sucs_by_id:
                suc_copy = copy.deepcopy(sucs_by_id[suc_id])
                gid = int(suc_copy.get("id_gimnasio") or 0)
                if gid in gyms_by_id:
                    suc_copy["gimnasio"] = copy.deepcopy(gyms_by_id[gid])
                enriched["sucursal"] = suc_copy

        elif self.table_name == "rutina_socio":
            rs_id = int(enriched.get("id_rutina_socio") or 0)
            items = []
            for it in self.state.get("rutina_ejercicio_socio", []):
                if int(it.get("id_rutina_socio") or 0) == rs_id:
                    it_copy = copy.deepcopy(it)
                    eid = int(it_copy.get("id_ejercicio_socio") or 0)
                    if eid in ej_socio_by_id:
                        it_copy["ejercicio_socio"] = copy.deepcopy(ej_socio_by_id[eid])
                    items.append(it_copy)
            enriched["rutina_ejercicio_socio"] = items

        elif self.table_name == "rutina_gimnasio":
            rg_id = int(enriched.get("id_rutina_gimnasio") or 0)
            items = []
            for it in self.state.get("rutina_ejercicio_gimnasio", []):
                if int(it.get("id_rutina_gimnasio") or 0) == rg_id:
                    it_copy = copy.deepcopy(it)
                    eid = int(it_copy.get("id_ejercicio_gimnasio") or 0)
                    if eid in ej_gym_by_id:
                        it_copy["ejercicio_gimnasio"] = copy.deepcopy(ej_gym_by_id[eid])
                    items.append(it_copy)
            enriched["rutina_ejercicio_gimnasio"] = items

        elif self.table_name == "rutinas_asignadas_socio_gimnasio":
            rg_id = int(enriched.get("id_rutina_gimnasio") or 0)
            sg_id = int(enriched.get("id_socio_gimnasio") or 0)
            emp_id = enriched.get("id_empleado_responsable")
            if rg_id in rg_by_id:
                rg_copy = copy.deepcopy(rg_by_id[rg_id])
                rg_copy["rutina_ejercicio_gimnasio"] = [
                    {
                        **copy.deepcopy(it),
                        "ejercicio_gimnasio": copy.deepcopy(ej_gym_by_id.get(int(it["id_ejercicio_gimnasio"]), {})),
                    }
                    for it in self.state.get("rutina_ejercicio_gimnasio", [])
                    if int(it.get("id_rutina_gimnasio") or 0) == rg_id
                ]
                enriched["rutina_gimnasio"] = rg_copy
            if sg_id in sgs_by_id:
                sg_copy = copy.deepcopy(sgs_by_id[sg_id])
                sid = int(sg_copy.get("id_socio") or 0)
                if sid in socios_by_id:
                    sg_copy["socio"] = copy.deepcopy(socios_by_id[sid])
                enriched["socio_gimnasio"] = sg_copy
            if emp_id is not None and int(emp_id) in emps_by_id:
                enriched["empleado"] = copy.deepcopy(emps_by_id[int(emp_id)])

        return enriched

    def execute(self) -> FakeSupabaseResponse:
        if self.table_name in ("progreso", "plan_membresia", "membresia", "asistencia", "ejercicio", "rutina", "rutina_ejercicio"):
            raise AssertionError(f"Tabla legacy/eliminada consultada: {self.table_name}")

        rows = self.state.setdefault(self.table_name, [])

        if self._insert_payload is not None:
            if self.table_name in ("asistencia_socio", "membresia_socio", "pago", "rutinas_asignadas_socio_gimnasio"):
                raise AssertionError(f"INSERT directo prohibido sobre {self.table_name}: debe usarse la RPC autorizada.")
            pk_map = {
                "ejercicio_socio": ("id_ejercicio_socio", 1000),
                "ejercicio_gimnasio": ("id_ejercicio_gimnasio", 2000),
                "rutina_socio": ("id_rutina_socio", 3000),
                "rutina_gimnasio": ("id_rutina_gimnasio", 4000),
                "rutina_ejercicio_socio": ("id_rutina_ejercicio_socio", 5000),
                "rutina_ejercicio_gimnasio": ("id_rutina_ejercicio_gimnasio", 6000),
            }
            inserted = []
            for item in self._insert_payload:
                new_row = copy.deepcopy(item)
                if self.table_name in pk_map:
                    pk_col, base = pk_map[self.table_name]
                    if pk_col not in new_row:
                        new_row[pk_col] = max([int(r[pk_col]) for r in rows], default=base) + 1
                rows.append(new_row)
                inserted.append(self._enrich_row(new_row))
            return FakeSupabaseResponse(inserted, count=len(inserted))

        matched_refs = []
        for r in rows:
            ok = True
            for col, val in self.filters_eq.items():
                if r.get(col) != val:
                    ok = False
                    break
            if not ok:
                continue
            for col, vals in self.filters_in.items():
                if r.get(col) not in vals:
                    ok = False
                    break
            if ok:
                matched_refs.append(r)

        if self._delete_requested:
            if self.table_name in ("asistencia_socio", "membresia_socio", "pago", "rutinas_asignadas_socio_gimnasio"):
                raise AssertionError(f"DELETE físico prohibido sobre {self.table_name}.")
            deleted = [self._enrich_row(r) for r in matched_refs]
            for r in matched_refs:
                rows.remove(r)
            return FakeSupabaseResponse(deleted, count=len(deleted))

        if self._update_payload is not None:
            if self.table_name in ("asistencia_socio", "membresia_socio", "pago", "rutinas_asignadas_socio_gimnasio"):
                raise AssertionError(f"UPDATE directo prohibido sobre {self.table_name}: debe usarse la RPC autorizada.")
            updated_out = []
            for r in matched_refs:
                r.update(copy.deepcopy(self._update_payload))
                updated_out.append(self._enrich_row(r))
            return FakeSupabaseResponse(updated_out, count=len(updated_out))

        enriched_out = [self._enrich_row(r) for r in matched_refs]
        if self.is_single:
            return FakeSupabaseResponse(enriched_out[0] if enriched_out else None)
        return FakeSupabaseResponse(enriched_out, count=len(enriched_out))


class FakeIntegralRpcBuilder:
    # Firmas oficiales del SQL definitivo para validar parámetros exactos en los mocks
    SQL_RPC_PARAMS: Dict[str, tuple[set[str], set[str]]] = {
        "rpc_registrar_ingreso_asistencia": ({"p_id_sucursal"}, {"p_id_sucursal", "p_id_socio"}),
        "rpc_registrar_salida_asistencia": (set(), {"p_id_asistencia_socio", "p_id_socio"}),
        "rpc_anular_asistencia": ({"p_id_asistencia_socio"}, {"p_id_asistencia_socio", "p_motivo"}),
        "rpc_asignar_rutina_gimnasio_socio": (
            {"p_id_rutina_gimnasio", "p_id_socio_gimnasio"},
            {"p_id_rutina_gimnasio", "p_id_socio_gimnasio", "p_fecha_finalizacion", "p_observaciones", "p_id_empleado_responsable"},
        ),
        "rpc_reasignar_responsable_rutina_asignada": (
            {"p_id_rutina_asignada", "p_id_empleado_responsable"},
            {"p_id_rutina_asignada", "p_id_empleado_responsable", "p_observaciones"},
        ),
        "rpc_finalizar_rutina_asignada": (
            {"p_id_rutina_asignada"},
            {"p_id_rutina_asignada", "p_fecha_finalizacion", "p_observaciones"},
        ),
        "rpc_reordenar_rutina_socio_ejercicios": (
            {"p_id_rutina_socio", "p_ids_ordenados"},
            {"p_id_rutina_socio", "p_ids_ordenados"},
        ),
        "rpc_reordenar_rutina_gimnasio_ejercicios": (
            {"p_id_rutina_gimnasio", "p_ids_ordenados"},
            {"p_id_rutina_gimnasio", "p_ids_ordenados"},
        ),
        "rpc_contratar_membresia": (
            {"p_id_socio", "p_id_membresia_gimnasio"},
            {
                "p_id_socio", "p_id_membresia_gimnasio", "p_fecha_inicio", "p_estado",
                "p_registrar_pago", "p_metodo_pago", "p_estado_pago", "p_comprobante", "p_monto_override",
            },
        ),
        "rpc_registrar_pago": (
            {"p_id_membresia_socio", "p_monto", "p_metodo_pago"},
            {"p_id_membresia_socio", "p_monto", "p_metodo_pago", "p_estado", "p_comprobante", "p_fecha_pago"},
        ),
    }

    def __init__(self, fn_name: str, params: Dict[str, Any], client: "FakeIntegralSupabaseClient"):
        self.fn_name = fn_name
        self.params = params
        self.client = client

    def execute(self) -> FakeSupabaseResponse:
        if self.fn_name in self.SQL_RPC_PARAMS:
            required, allowed = self.SQL_RPC_PARAMS[self.fn_name]
            sent = set(self.params.keys())
            unknown = sent - allowed
            missing = required - sent
            if unknown or missing:
                raise AssertionError(
                    f"Firma RPC incompatible para {self.fn_name}: unknown={sorted(unknown)}, missing={sorted(missing)}"
                )
        self.client.rpc_calls.append((self.fn_name, copy.deepcopy(self.params)))
        handler = getattr(self.client, f"_{self.fn_name}", None)
        if handler is None:
            raise RuntimeError(f"RPC no soportada en mock integral: {self.fn_name}")
        return FakeSupabaseResponse(handler(self.params))


class FakeIntegralSupabaseClient:
    """Simulador en memoria fiel al contrato SQL definitivo para las RPCs de todos los módulos."""

    def __init__(self, state: Dict[str, List[Dict[str, Any]]], actor_uid: Optional[str] = None):
        self.state = state
        self.actor_uid = actor_uid
        self.rpc_calls: List[tuple[str, Dict[str, Any]]] = []

    def from_(self, table_name: str) -> FakeIntegralQueryBuilder:
        return FakeIntegralQueryBuilder(table_name, self.state)

    def rpc(self, fn_name: str, params: Optional[Dict[str, Any]] = None) -> FakeIntegralRpcBuilder:
        return FakeIntegralRpcBuilder(fn_name, params or {}, self)

    # --- Asistencias RPCs ---
    def _rpc_registrar_ingreso_asistencia(self, params: Dict[str, Any]) -> Dict[str, Any]:
        suc_id = int(params["p_id_sucursal"])
        sid = int(params["p_id_socio"])
        suc = next((s for s in self.state.get("sucursal", []) if int(s["id_sucursal"]) == suc_id), None)
        if not suc or suc.get("estado") is not True:
            raise ValueError("La sucursal seleccionada no existe o se encuentra inactiva.")
        gid = int(suc["id_gimnasio"])

        sg = next(
            (a for a in self.state.get("socio_gimnasio", []) if int(a["id_socio"]) == sid and int(a["id_gimnasio"]) == gid),
            None,
        )
        if not sg or sg.get("estado") is not True:
            raise ValueError("El socio no posee una asociación socio_gimnasio activa en el gimnasio de esta sucursal.")

        today = date.today()
        tiene_vigente = any(
            int(m["id_socio_gimnasio"]) == int(sg["id_socio_gimnasio"])
            and int(m["id_gimnasio"]) == gid
            and m.get("estado") == "activa"
            and date.fromisoformat(str(m["fecha_inicio"])) <= today <= date.fromisoformat(str(m["fecha_vencimiento"]))
            for m in self.state.get("membresia_socio", [])
        )
        if not tiene_vigente:
            raise ValueError("El socio no cuenta con una membresía activa y vigente en el gimnasio de esta sucursal.")

        sucs_gym = {int(s["id_sucursal"]) for s in self.state.get("sucursal", []) if int(s["id_gimnasio"]) == gid}
        asists = self.state.setdefault("asistencia_socio", [])

        abierta = next(
            (
                a for a in asists
                if int(a["id_socio"]) == sid
                and int(a["id_sucursal"]) in sucs_gym
                and a.get("estado") == "presente"
                and a.get("hora_salida") is None
            ),
            None,
        )
        if abierta:
            raise ValueError(
                f"Ya existe un ingreso en curso en este gimnasio (asistencia #{abierta['id_asistencia_socio']}). Debes registrar la salida antes de volver a ingresar."
            )

        hoy_str = today.isoformat()
        cnt_hoy = sum(
            1
            for a in asists
            if int(a["id_socio"]) == sid
            and int(a["id_sucursal"]) in sucs_gym
            and a.get("estado") != "anulada"
            and str(a.get("hora_ingreso") or "").startswith(hoy_str)
        )
        if cnt_hoy >= 3:
            raise ValueError("El socio ya alcanzó el límite máximo de 3 asistencias diarias en este gimnasio.")

        new_id = max([int(a["id_asistencia_socio"]) for a in asists], default=9000) + 1
        row = {
            "id_asistencia_socio": new_id,
            "id_socio": sid,
            "id_sucursal": suc_id,
            "hora_ingreso": datetime.now(timezone.utc).isoformat(),
            "hora_salida": None,
            "estado": "presente",
            "anulada_por": None,
            "anulada_en": None,
            "motivo_anulacion": None,
        }
        asists.append(row)
        return copy.deepcopy(row)

    def _actor_can_manage_sucursal_asistencia(self, suc_id: int, target_socio_id: int) -> bool:
        if not self.actor_uid:
            return True
        # Propio socio
        caller_socio = next((s for s in self.state.get("socio", []) if s.get("auth_user_id") == self.actor_uid), None)
        if caller_socio and int(caller_socio["id_socio"]) == int(target_socio_id):
            return True
        suc = next((s for s in self.state.get("sucursal", []) if int(s["id_sucursal"]) == int(suc_id)), None)
        if not suc:
            return False
        gid = int(suc["id_gimnasio"])
        # Dueño del gimnasio
        dueno = next((d for d in self.state.get("dueno", []) if d.get("auth_user_id") == self.actor_uid), None)
        if dueno and any(int(g["id_gimnasio"]) == gid and int(g["id_dueno"]) == int(dueno["id_dueno"]) for g in self.state.get("gimnasio", [])):
            return True
        # Admin org activo
        adm = next((a for a in self.state.get("administrador", []) if a.get("auth_user_id") == self.actor_uid and a.get("estado") is True), None)
        if adm and any(int(ag["id_administrador"]) == int(adm["id_administrador"]) and int(ag["id_gimnasio"]) == gid and ag.get("estado") is True for ag in self.state.get("administrador_gimnasio", [])):
            return True
        # Empleado activo en ESA sucursal con rol administrador o recepcionista (nunca entrenador)
        emp = next((e for e in self.state.get("empleado", []) if e.get("auth_user_id") == self.actor_uid and e.get("estado") is True), None)
        if emp and any(
            int(es["id_empleado"]) == int(emp["id_empleado"])
            and int(es["id_sucursal"]) == int(suc_id)
            and es.get("estado") is True
            and str(es.get("rol") or "").lower() in ("administrador", "recepcionista")
            for es in self.state.get("empleado_sucursal", [])
        ):
            return True
        return False

    def _rpc_registrar_salida_asistencia(self, params: Dict[str, Any]) -> Dict[str, Any]:
        asist_id = params.get("p_id_asistencia_socio")
        sid = params.get("p_id_socio")
        asists = self.state.get("asistencia_socio", [])

        if asist_id is not None:
            actual = next((a for a in asists if int(a["id_asistencia_socio"]) == int(asist_id)), None)
        else:
            actual = next(
                (
                    a for a in reversed(asists)
                    if int(a["id_socio"]) == int(sid) and a.get("estado") == "presente" and a.get("hora_salida") is None
                ),
                None,
            )
        if not actual:
            raise ValueError("No se encontró una asistencia abierta o no tienes permisos para cerrarla.")
        if not self._actor_can_manage_sucursal_asistencia(int(actual["id_sucursal"]), int(actual["id_socio"])):
            raise ValueError("No tienes autorización para registrar la salida de esta asistencia.")
        if actual.get("estado") != "presente" or actual.get("hora_salida") is not None:
            raise ValueError(f"Transición inválida: la asistencia #{actual['id_asistencia_socio']} no está abierta.")

        actual["hora_salida"] = datetime.now(timezone.utc).isoformat()
        actual["estado"] = "finalizada"
        return copy.deepcopy(actual)

    def _rpc_anular_asistencia(self, params: Dict[str, Any]) -> Dict[str, Any]:
        asist_id = int(params["p_id_asistencia_socio"])
        motivo = params.get("p_motivo")
        actual = next((a for a in self.state.get("asistencia_socio", []) if int(a["id_asistencia_socio"]) == asist_id), None)
        if not actual:
            raise ValueError(f"La asistencia #{asist_id} no existe.")
        if actual.get("estado") == "anulada":
            raise ValueError(f"La asistencia #{asist_id} ya fue anulada previamente.")
        actual["estado"] = "anulada"
        actual["anulada_por"] = self.actor_uid
        actual["anulada_en"] = datetime.now(timezone.utc).isoformat()
        actual["motivo_anulacion"] = motivo
        return copy.deepcopy(actual)

    # --- Rutinas RPCs ---
    def _validate_active_responsible_emp(self, emp_id: int, gym_id: int):
        emp = next((e for e in self.state.get("empleado", []) if int(e["id_empleado"]) == emp_id), None)
        if not emp or emp.get("estado") is not True:
            raise ValueError("Empleado responsable inválido o inactivo.")
        sucs_gym = {int(s["id_sucursal"]) for s in self.state.get("sucursal", []) if int(s["id_gimnasio"]) == gym_id}
        ok = any(
            int(es["id_empleado"]) == emp_id
            and es.get("estado") is True
            and int(es["id_sucursal"]) in sucs_gym
            and str(es.get("rol") or "").lower() in ("entrenador", "administrador")
            for es in self.state.get("empleado_sucursal", [])
        )
        if not ok:
            raise ValueError("Empleado responsable debe tener asignación activa como entrenador o administrador en este gimnasio.")

    def _rpc_asignar_rutina_gimnasio_socio(self, params: Dict[str, Any]) -> Dict[str, Any]:
        rg_id = int(params["p_id_rutina_gimnasio"])
        sg_id = int(params["p_id_socio_gimnasio"])
        emp_resp = params.get("p_id_empleado_responsable")

        rg = next((r for r in self.state.get("rutina_gimnasio", []) if int(r["id_rutina_gimnasio"]) == rg_id), None)
        if not rg or rg.get("estado") is not True:
            raise ValueError(f"La rutina de gimnasio ({rg_id}) no existe o está inactiva.")
        sg = next((a for a in self.state.get("socio_gimnasio", []) if int(a["id_socio_gimnasio"]) == sg_id), None)
        if not sg or sg.get("estado") is not True:
            raise ValueError(f"La asociación socio_gimnasio ({sg_id}) no existe o está inactiva.")
        if int(rg["id_gimnasio"]) != int(sg["id_gimnasio"]):
            raise ValueError("No se puede asignar una rutina de otro gimnasio.")

        gid = int(rg["id_gimnasio"])
        if emp_resp is not None:
            self._validate_active_responsible_emp(int(emp_resp), gid)

        asigs = self.state.setdefault("rutinas_asignadas_socio_gimnasio", [])
        if any(
            int(a["id_rutina_gimnasio"]) == rg_id and int(a["id_socio_gimnasio"]) == sg_id and a.get("estado") is True
            for a in asigs
        ):
            raise ValueError("La rutina ya se encuentra asignada activamente a esta asociación socio-gimnasio.")

        new_id = max([int(a["id_rutina_asignada"]) for a in asigs], default=7000) + 1
        row = {
            "id_rutina_asignada": new_id,
            "id_rutina_gimnasio": rg_id,
            "id_socio_gimnasio": sg_id,
            "id_gimnasio": gid,
            "id_empleado_responsable": int(emp_resp) if emp_resp is not None else None,
            "fecha_asignacion": date.today().isoformat(),
            "fecha_finalizacion": params.get("p_fecha_finalizacion"),
            "estado": True,
            "observaciones": params.get("p_observaciones"),
            "creado_en": datetime.now(timezone.utc).isoformat(),
        }
        asigs.append(row)
        return copy.deepcopy(row)

    def _rpc_reasignar_responsable_rutina_asignada(self, params: Dict[str, Any]) -> Dict[str, Any]:
        asig_id = int(params["p_id_rutina_asignada"])
        emp_id = int(params["p_id_empleado_responsable"])
        asig = next(
            (a for a in self.state.get("rutinas_asignadas_socio_gimnasio", []) if int(a["id_rutina_asignada"]) == asig_id),
            None,
        )
        if not asig:
            raise ValueError(f"La asignación #{asig_id} no existe.")
        if asig.get("estado") is not True:
            raise ValueError("No se puede reasignar el responsable de una rutina ya finalizada.")
        self._validate_active_responsible_emp(emp_id, int(asig["id_gimnasio"]))
        asig["id_empleado_responsable"] = emp_id
        if params.get("p_observaciones"):
            asig["observaciones"] = params["p_observaciones"]
        return copy.deepcopy(asig)

    def _rpc_finalizar_rutina_asignada(self, params: Dict[str, Any]) -> Dict[str, Any]:
        asig_id = int(params["p_id_rutina_asignada"])
        asig = next(
            (a for a in self.state.get("rutinas_asignadas_socio_gimnasio", []) if int(a["id_rutina_asignada"]) == asig_id),
            None,
        )
        if not asig:
            raise ValueError(f"La asignación #{asig_id} no existe.")
        # Conserva intacto id_empleado_responsable histórico incluso si el empleado está desactivado
        asig["estado"] = False
        asig["fecha_finalizacion"] = params.get("p_fecha_finalizacion") or date.today().isoformat()
        if params.get("p_observaciones"):
            asig["observaciones"] = params["p_observaciones"]
        return copy.deepcopy(asig)

    def _rpc_reordenar_rutina_socio_ejercicios(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        rs_id = int(params["p_id_rutina_socio"])
        ids = [int(x) for x in params["p_ids_ordenados"]]
        items = [it for it in self.state.get("rutina_ejercicio_socio", []) if int(it["id_rutina_socio"]) == rs_id]
        if len(items) != len(ids) or len(set(ids)) != len(ids):
            raise ValueError("Cantidad de ejercicios distinta al total de la rutina o IDs duplicados.")
        by_id = {int(it["id_rutina_ejercicio_socio"]): it for it in items}
        for idx, item_id in enumerate(ids, start=1):
            by_id[item_id]["orden"] = idx
        return [copy.deepcopy(by_id[item_id]) for item_id in ids]

    def _rpc_reordenar_rutina_gimnasio_ejercicios(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        rg_id = int(params["p_id_rutina_gimnasio"])
        ids = [int(x) for x in params["p_ids_ordenados"]]
        items = [it for it in self.state.get("rutina_ejercicio_gimnasio", []) if int(it["id_rutina_gimnasio"]) == rg_id]
        if len(items) != len(ids) or len(set(ids)) != len(ids):
            raise ValueError("Cantidad de ejercicios distinta al total de la rutina o IDs duplicados.")
        by_id = {int(it["id_rutina_ejercicio_gimnasio"]): it for it in items}
        for idx, item_id in enumerate(ids, start=1):
            by_id[item_id]["orden"] = idx
        return [copy.deepcopy(by_id[item_id]) for item_id in ids]

    # --- Membresías y Pagos RPCs (para flujo integral multi-módulo) ---
    def _rpc_contratar_membresia(self, params: Dict[str, Any]) -> Dict[str, Any]:
        sid = int(params["p_id_socio"])
        plan_id = int(params["p_id_membresia_gimnasio"])
        plan = next((p for p in self.state.get("membresia_gimnasio", []) if int(p["id_membresia_gimnasio"]) == plan_id), None)
        gid = int(plan["id_gimnasio"])
        sg = next(a for a in self.state.get("socio_gimnasio", []) if int(a["id_socio"]) == sid and int(a["id_gimnasio"]) == gid)
        membs = self.state.setdefault("membresia_socio", [])
        f_ini = date.fromisoformat(str(params.get("p_fecha_inicio") or date.today().isoformat()))
        f_ven = f_ini + timedelta(days=int(plan["duracion_dias"]))
        new_id = max([int(m["id_membresia_socio"]) for m in membs], default=800) + 1
        contrato = {
            "id_membresia_socio": new_id,
            "id_socio_gimnasio": int(sg["id_socio_gimnasio"]),
            "id_membresia_gimnasio": plan_id,
            "id_gimnasio": gid,
            "precio": float(plan["precio"]),
            "duracion_dias_contratados": int(plan["duracion_dias"]),
            "limite_asistencias_semanales": None,
            "limite_asistencias_totales": None,
            "fecha_inicio": f_ini.isoformat(),
            "fecha_vencimiento": f_ven.isoformat(),
            "estado": str(params.get("p_estado") or "activa"),
            "creado_en": datetime.now(timezone.utc).isoformat(),
        }
        membs.append(contrato)
        return {"socio_gimnasio": copy.deepcopy(sg), "membresia_socio": copy.deepcopy(contrato), "pago": None}

    def _rpc_registrar_pago(self, params: Dict[str, Any]) -> Dict[str, Any]:
        ms_id = int(params["p_id_membresia_socio"])
        contrato = next(m for m in self.state.get("membresia_socio", []) if int(m["id_membresia_socio"]) == ms_id)
        pagos = self.state.setdefault("pago", [])
        new_id = max([int(p["id_pago"]) for p in pagos], default=900) + 1
        pago = {
            "id_pago": new_id,
            "fecha_pago": params.get("p_fecha_pago") or datetime.now(timezone.utc).isoformat(),
            "monto": float(params["p_monto"]),
            "metodo_pago": params["p_metodo_pago"],
            "estado": params.get("p_estado") or "aprobado",
            "comprobante": params.get("p_comprobante"),
            "id_membresia_socio": ms_id,
        }
        pagos.append(pago)
        if pago["estado"] == "aprobado" and contrato["estado"] == "pendiente" and pago["monto"] >= float(contrato["precio"]):
            contrato["estado"] = "activa"
        return copy.deepcopy(pago)


class TestIntegralAsistenciasRutinasDashboards(unittest.TestCase):
    """
    Suite integral de pruebas unitarias para:
      - Módulo 2: Asistencias (asistencia_socio + 3 RPCs, multi-sucursal, límite 3/día, 1 abierta/gym).
      - Módulo 3: Ejercicios y Rutinas separados (personal vs gimnasio, asignaciones, responsable histórico).
      - Módulo 4: Dashboards organizacional y socio (aislamiento, sin `progreso`, restricción financiera a entrenador).
      - Módulo 5: Flujo de integración multi-módulo con cliente RLS por petición.
    """

    @classmethod
    def setUpClass(cls):
        cls.ec_private_key = ec.generate_private_key(ec.SECP256R1())
        cls.ec_public_key = cls.ec_private_key.public_key()
        cls.issuer = "https://qdlkljSb.supabase.co/auth/v1"
        cls.audience = "authenticated"

        cls.uid_dueno_1 = "33333333-3333-4333-8333-000000000001"
        cls.uid_dueno_2 = "33333333-3333-4333-8333-000000000002"
        cls.uid_recep_101 = "33333333-3333-4333-8333-000000000003"
        cls.uid_trainer_101 = "33333333-3333-4333-8333-000000000004"
        cls.uid_socio_70 = "33333333-3333-4333-8333-000000000005"
        cls.uid_socio_71 = "33333333-3333-4333-8333-000000000006"
        cls.uid_socio_73_sin_memb = "33333333-3333-4333-8333-000000000007"

        cls.client = TestClient(main_module.app)

    def _make_token(self, sub: str, email: str = "test@gymtrack.test") -> str:
        now = datetime.now(timezone.utc)
        payload = {
            "sub": sub,
            "email": email,
            "role": "authenticated",
            "aud": self.audience,
            "iss": self.issuer,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
            "email_confirmed_at": now.isoformat(),
            "app_metadata": {"provider": "email"},
            "user_metadata": {"email_verified": True},
        }
        return jwt.encode(payload, self.ec_private_key, algorithm="ES256", headers={"kid": "test-kid-int"})

    def _build_initial_state(self) -> Dict[str, List[Dict[str, Any]]]:
        today = date.today()
        return {
            "dueno": [
                {"id_dueno": 1, "auth_user_id": self.uid_dueno_1, "nombre": "Martín", "apellido": "Rivas", "email": "dueno1@gymtrack.test"},
                {"id_dueno": 2, "auth_user_id": self.uid_dueno_2, "nombre": "Valeria", "apellido": "Gómez", "email": "dueno2@gymtrack.test"},
            ],
            "gimnasio": [
                {"id_gimnasio": 10, "nombre": "Norte Fit", "telefono": "111", "email": "norte@test", "fecha_registro": "2026-01-01", "estado": True, "id_dueno": 1},
                {"id_gimnasio": 20, "nombre": "Sur Power", "telefono": "222", "email": "sur@test", "fecha_registro": "2026-01-01", "estado": True, "id_dueno": 2},
            ],
            "sucursal": [
                {"id_sucursal": 101, "direccion": "Av. Norte 1", "telefono": "111-1", "descripcion": "Norte Sede A", "estado": True, "id_gimnasio": 10},
                {"id_sucursal": 102, "direccion": "Av. Norte 2", "telefono": "111-2", "descripcion": "Norte Sede B", "estado": True, "id_gimnasio": 10},
                {"id_sucursal": 201, "direccion": "Av. Sur 1", "telefono": "222-1", "descripcion": "Sur Sede A", "estado": True, "id_gimnasio": 20},
            ],
            "administrador": [],
            "administrador_gimnasio": [],
            "empleado": [
                {"id_empleado": 30, "auth_user_id": self.uid_recep_101, "nombre": "Sofía", "apellido": "Recep", "dni": "301", "email": "recep@test", "estado": True},
                {"id_empleado": 31, "auth_user_id": self.uid_trainer_101, "nombre": "Julián", "apellido": "Trainer", "dni": "302", "email": "trainer@test", "estado": True},
                {"id_empleado": 32, "auth_user_id": None, "nombre": "Ex", "apellido": "Entrenador", "dni": "303", "email": "ex@test", "estado": False},
            ],
            "empleado_sucursal": [
                {"id_empleado_sucursal": 301, "rol": "recepcionista", "id_empleado": 30, "id_sucursal": 101, "estado": True},
                {"id_empleado_sucursal": 302, "rol": "entrenador", "id_empleado": 31, "id_sucursal": 101, "estado": True},
                {"id_empleado_sucursal": 303, "rol": "entrenador", "id_empleado": 32, "id_sucursal": 101, "estado": False},
            ],
            "socio": [
                {"id_socio": 70, "auth_user_id": self.uid_socio_70, "dni": "40000070", "telefono": "11-70", "fecha_alta": "2026-01-01", "estado": True, "nombre": "Lucía", "apellido": "Pereyra", "email": "s70@test"},
                {"id_socio": 71, "auth_user_id": self.uid_socio_71, "dni": "40000071", "telefono": "11-71", "fecha_alta": "2026-01-01", "estado": True, "nombre": "Carlos", "apellido": "Multi", "email": "s71@test"},
                {"id_socio": 73, "auth_user_id": self.uid_socio_73_sin_memb, "dni": "40000073", "telefono": "11-73", "fecha_alta": "2026-01-01", "estado": True, "nombre": "Diego", "apellido": "SinMemb", "email": "s73@test"},
            ],
            "socio_gimnasio": [
                {"id_socio_gimnasio": 701, "id_socio": 70, "id_gimnasio": 10, "fecha_alta": today.isoformat(), "estado": True},
                {"id_socio_gimnasio": 702, "id_socio": 71, "id_gimnasio": 10, "fecha_alta": "2026-01-05", "estado": True},
                {"id_socio_gimnasio": 703, "id_socio": 71, "id_gimnasio": 20, "fecha_alta": "2026-01-15", "estado": True},
                {"id_socio_gimnasio": 705, "id_socio": 73, "id_gimnasio": 10, "fecha_alta": "2026-02-01", "estado": True},
            ],
            "membresia_gimnasio": [
                {"id_membresia_gimnasio": 501, "nombre": "Plan Norte 30d", "descripcion": "Full", "precio": 30000.0, "duracion_dias": 30, "limite_asistencias_semanales": None, "limite_asistencias_totales": None, "estado": True, "id_gimnasio": 10},
                {"id_membresia_gimnasio": 503, "nombre": "Plan Sur 30d", "descripcion": "Full", "precio": 40000.0, "duracion_dias": 30, "limite_asistencias_semanales": None, "limite_asistencias_totales": None, "estado": True, "id_gimnasio": 20},
            ],
            "membresia_socio": [
                {"id_membresia_socio": 801, "id_socio_gimnasio": 701, "id_membresia_gimnasio": 501, "id_gimnasio": 10, "precio": 30000.0, "duracion_dias_contratados": 30, "fecha_inicio": (today - timedelta(days=5)).isoformat(), "fecha_vencimiento": (today + timedelta(days=25)).isoformat(), "estado": "activa"},
                {"id_membresia_socio": 802, "id_socio_gimnasio": 702, "id_membresia_gimnasio": 501, "id_gimnasio": 10, "precio": 30000.0, "duracion_dias_contratados": 30, "fecha_inicio": (today - timedelta(days=5)).isoformat(), "fecha_vencimiento": (today + timedelta(days=25)).isoformat(), "estado": "activa"},
                {"id_membresia_socio": 803, "id_socio_gimnasio": 703, "id_membresia_gimnasio": 503, "id_gimnasio": 20, "precio": 40000.0, "duracion_dias_contratados": 30, "fecha_inicio": (today - timedelta(days=5)).isoformat(), "fecha_vencimiento": (today + timedelta(days=25)).isoformat(), "estado": "activa"},
            ],
            "pago": [
                {"id_pago": 901, "fecha_pago": datetime.now(timezone.utc).isoformat(), "monto": 30000.0, "metodo_pago": "efectivo", "estado": "aprobado", "comprobante": "C-10-1", "id_membresia_socio": 801},
                {"id_pago": 902, "fecha_pago": datetime.now(timezone.utc).isoformat(), "monto": 40000.0, "metodo_pago": "transferencia", "estado": "aprobado", "comprobante": "C-20-1", "id_membresia_socio": 803},
            ],
            "asistencia_socio": [],
            "ejercicio_socio": [
                {"id_ejercicio_socio": 1001, "id_socio": 70, "nombre": "Flexiones casa", "descripcion": "Peso corporal", "grupo_muscular": "Pecho", "dificultad": "Baja", "estado": True},
                {"id_ejercicio_socio": 1002, "id_socio": 70, "nombre": "Plancha casa", "descripcion": "Core", "grupo_muscular": "Abdomen", "dificultad": "Media", "estado": True},
                {"id_ejercicio_socio": 1003, "id_socio": 71, "nombre": "Ejercicio Ajeno", "descripcion": "De Socio 71", "grupo_muscular": "Piernas", "dificultad": "Alta", "estado": True},
            ],
            "ejercicio_gimnasio": [
                {"id_ejercicio_gimnasio": 2001, "id_gimnasio": 10, "nombre": "Press Banca Norte", "descripcion": "Barra olímpica", "grupo_muscular": "Pecho", "dificultad": "Intermedia", "estado": True},
                {"id_ejercicio_gimnasio": 2002, "id_gimnasio": 10, "nombre": "Sentadilla Norte", "descripcion": "Rack", "grupo_muscular": "Piernas", "dificultad": "Alta", "estado": True},
                {"id_ejercicio_gimnasio": 2003, "id_gimnasio": 20, "nombre": "Remo Sur", "descripcion": "Gym 20", "grupo_muscular": "Espalda", "dificultad": "Media", "estado": True},
            ],
            "rutina_socio": [],
            "rutina_gimnasio": [
                {"id_rutina_gimnasio": 4001, "id_gimnasio": 10, "nombre": "Hipertrofia Norte", "descripcion": "Base", "nivel": "Intermedio", "estado": True},
            ],
            "rutina_ejercicio_socio": [],
            "rutina_ejercicio_gimnasio": [
                {"id_rutina_ejercicio_gimnasio": 6001, "id_rutina_gimnasio": 4001, "id_gimnasio": 10, "id_ejercicio_gimnasio": 2001, "series": 4, "repeticiones": 10, "peso": 60.0, "descanso": 90, "orden": 1},
                {"id_rutina_ejercicio_gimnasio": 6002, "id_rutina_gimnasio": 4001, "id_gimnasio": 10, "id_ejercicio_gimnasio": 2002, "series": 4, "repeticiones": 8, "peso": 80.0, "descanso": 120, "orden": 2},
            ],
            "rutinas_asignadas_socio_gimnasio": [
                # Asignación previa con empleado 32 (hoy desactivado) como responsable histórico
                {
                    "id_rutina_asignada": 7001,
                    "id_rutina_gimnasio": 4001,
                    "id_socio_gimnasio": 701,
                    "id_gimnasio": 10,
                    "id_empleado_responsable": 32,
                    "fecha_asignacion": "2026-10-01",
                    "fecha_finalizacion": None,
                    "estado": True,
                    "observaciones": "Asignada antes de desactivar al entrenador 32",
                }
            ],
        }

    def _patch_env(self, state: Optional[Dict[str, List[Dict[str, Any]]]] = None):
        shared_state = state if state is not None else self._build_initial_state()
        fake_db = FakeIntegralSupabaseClient(shared_state)
        token_calls: List[str] = []

        def _fake_verify(token: str):
            claims = verify_supabase_access_token(
                token,
                verification_key=self.ec_public_key,
                expected_issuer=self.issuer,
                expected_audience=self.audience,
            )
            fake_db.actor_uid = claims.get("sub")
            return claims

        def _fake_create_client(access_token: str):
            token_calls.append(access_token)
            return fake_db

        p1 = patch("app.core.dependencies.verify_supabase_access_token", side_effect=_fake_verify)
        p2 = patch("app.core.dependencies.create_supabase_client_for_token", side_effect=_fake_create_client)
        return p1, p2, fake_db, token_calls

    # =========================================================================
    # MÓDULO 2: ASISTENCIAS (Tests 1 - 4)
    # =========================================================================

    def test_01_asistencia_ingreso_salida_via_rpc_y_sin_campos_manipulables(self):
        token_recep = self._make_token(self.uid_recep_101, "recep@test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            # Bloquea campos manipulables (hora_ingreso, id_gimnasio, id_membresia_socio) -> 403
            res_tamper = self.client.post(
                "/api/asistencias",
                json={"id_sucursal": 101, "id_socio": 70, "hora_ingreso": "2020-01-01T00:00:00Z"},
                headers={"Authorization": f"Bearer {token_recep}"},
            )
            self.assertEqual(res_tamper.status_code, 403)

            # Registro por DNI en Sucursal 101 vía rpc_registrar_ingreso_asistencia
            res_in = self.client.post(
                "/api/asistencias",
                json={"id_sucursal": 101, "dni": "40000070"},
                headers={"Authorization": f"Bearer {token_recep}"},
            )
            self.assertEqual(res_in.status_code, 201)
            asist = res_in.json()["asistencia"]
            self.assertEqual(asist["id_socio"], 70)
            self.assertEqual(asist["id_sucursal"], 101)
            self.assertEqual(asist["estado"], "presente")
            self.assertNotIn("id_gimnasio", asist)
            self.assertNotIn("id_membresia_socio", asist)
            self.assertEqual(fake_db.rpc_calls[-1][0], "rpc_registrar_ingreso_asistencia")

            # Salida vía rpc_registrar_salida_asistencia
            res_out = self.client.patch(
                f"/api/asistencias/{asist['id_asistencia_socio']}/checkout",
                headers={"Authorization": f"Bearer {token_recep}"},
            )
            self.assertEqual(res_out.status_code, 200)
            self.assertEqual(res_out.json()["asistencia"]["estado"], "finalizada")
            self.assertIsNotNone(res_out.json()["asistencia"]["hora_salida"])
            self.assertEqual(fake_db.rpc_calls[-1][0], "rpc_registrar_salida_asistencia")

    def test_02_asistencia_una_sola_abierta_por_gimnasio_409(self):
        token_socio71 = self._make_token(self.uid_socio_71, "s71@test")
        p1, p2, _, _ = self._patch_env()
        with p1, p2:
            # Ingreso en Sucursal 101 (Gym 10)
            r1 = self.client.post(
                "/api/asistencias",
                json={"id_sucursal": 101},
                headers={"Authorization": f"Bearer {token_socio71}"},
            )
            self.assertEqual(r1.status_code, 201)
            # Segundo ingreso en otra sucursal del MISMO gimnasio (Sucursal 102, Gym 10) sin cerrar -> 409
            r2 = self.client.post(
                "/api/asistencias",
                json={"id_sucursal": 102},
                headers={"Authorization": f"Bearer {token_socio71}"},
            )
            self.assertEqual(r2.status_code, 409)

    def test_03_asistencia_limite_3_diarias_sumando_sucursales_y_multigimnasio(self):
        token_socio71 = self._make_token(self.uid_socio_71, "s71@test")
        p1, p2, _, _ = self._patch_env()
        with p1, p2:
            # 3 ingresos + salidas en el mismo día repartidos entre Sucursal 101 y Sucursal 102 (ambas Gym 10)
            for suc_id in (101, 102, 101):
                r_in = self.client.post(
                    "/api/asistencias",
                    json={"id_sucursal": suc_id},
                    headers={"Authorization": f"Bearer {token_socio71}"},
                )
                self.assertEqual(r_in.status_code, 201)
                asist_id = r_in.json()["asistencia"]["id_asistencia_socio"]
                r_out = self.client.patch(
                    f"/api/asistencias/{asist_id}/salida",
                    headers={"Authorization": f"Bearer {token_socio71}"},
                )
                self.assertEqual(r_out.status_code, 200)

            # 4to ingreso del día en Gym 10 (Sucursal 102) -> 409 Conflict
            r_4th = self.client.post(
                "/api/asistencias",
                json={"id_sucursal": 102},
                headers={"Authorization": f"Bearer {token_socio71}"},
            )
            self.assertEqual(r_4th.status_code, 409)

            # En otro gimnasio donde también tiene membresía vigente (Gym 20, Sucursal 201) SÍ puede ingresar
            r_gym20 = self.client.post(
                "/api/asistencias",
                json={"id_sucursal": 201},
                headers={"Authorization": f"Bearer {token_socio71}"},
            )
            self.assertEqual(r_gym20.status_code, 201)

    def test_04_asistencia_sin_membresia_permisos_sucursal_y_anulacion_logica(self):
        token_recep101 = self._make_token(self.uid_recep_101, "recep@test")
        token_trainer101 = self._make_token(self.uid_trainer_101, "trainer@test")
        token_dueno1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            # Socio 73 no tiene membresía vigente -> 400
            r_nomemb = self.client.post(
                "/api/asistencias",
                json={"id_sucursal": 101, "id_socio": 73},
                headers={"Authorization": f"Bearer {token_recep101}"},
            )
            self.assertEqual(r_nomemb.status_code, 400)

            # Recepcionista de Sucursal 101 NO puede registrar ingresos en Sucursal 102 -> 403
            r_other_suc = self.client.post(
                "/api/asistencias",
                json={"id_sucursal": 102, "id_socio": 70},
                headers={"Authorization": f"Bearer {token_recep101}"},
            )
            self.assertEqual(r_other_suc.status_code, 403)

            # Entrenador NO puede registrar ingresos -> 403
            r_trainer = self.client.post(
                "/api/asistencias",
                json={"id_sucursal": 101, "id_socio": 70},
                headers={"Authorization": f"Bearer {token_trainer101}"},
            )
            self.assertEqual(r_trainer.status_code, 403)

            # Dueño 1 registra ingreso y luego lo anula lógicamente vía DELETE -> rpc_anular_asistencia
            r_in = self.client.post(
                "/api/asistencias",
                json={"id_sucursal": 101, "id_socio": 70},
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_in.status_code, 201)
            asist_id = r_in.json()["asistencia"]["id_asistencia_socio"]

            # Entrenador no puede anular -> 403
            self.assertEqual(
                self.client.delete(
                    f"/api/asistencias/{asist_id}",
                    headers={"Authorization": f"Bearer {token_trainer101}"},
                ).status_code,
                403,
            )

            # Dueño anula lógicamente conservando el historial
            r_anul = self.client.delete(
                f"/api/asistencias/{asist_id}?motivo=Error+de+DNI",
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_anul.status_code, 200)
            self.assertEqual(r_anul.json()["asistencia"]["estado"], "anulada")
            self.assertEqual(r_anul.json()["asistencia"]["motivo_anulacion"], "Error de DNI")
            self.assertEqual(fake_db.rpc_calls[-1][0], "rpc_anular_asistencia")
            self.assertEqual(len(fake_db.state["asistencia_socio"]), 1)

    # =========================================================================
    # MÓDULO 3: EJERCICIOS Y RUTINAS (Tests 5 - 8)
    # =========================================================================

    def test_05_ejercicios_personales_y_organizacionales_separados(self):
        token_socio70 = self._make_token(self.uid_socio_70, "s70@test")
        token_trainer = self._make_token(self.uid_trainer_101, "trainer@test")
        token_recep = self._make_token(self.uid_recep_101, "recep@test")
        p1, p2, _, _ = self._patch_env()
        with p1, p2:
            # Socio 70 crea su ejercicio personal en ejercicio_socio
            r_s = self.client.post(
                "/api/socio/ejercicios",
                json={"nombre": "Dominadas puerta", "grupo_muscular": "Espalda"},
                headers={"Authorization": f"Bearer {token_socio70}"},
            )
            self.assertEqual(r_s.status_code, 201)
            self.assertEqual(r_s.json()["ejercicio"]["id_socio"], 70)

            # Socio 70 NO puede editar ejercicio_socio 1003 de Socio 71 -> 403
            self.assertEqual(
                self.client.put(
                    "/api/socio/ejercicios/1003",
                    json={"nombre": "Hack"},
                    headers={"Authorization": f"Bearer {token_socio70}"},
                ).status_code,
                403,
            )

            # Entrenador SÍ crea ejercicio_gimnasio en Gym 10
            r_g = self.client.post(
                "/api/ejercicios",
                json={"nombre": "Peso Muerto Norte", "grupo_muscular": "Espalda"},
                headers={"Authorization": f"Bearer {token_trainer}"},
            )
            self.assertEqual(r_g.status_code, 201)
            self.assertEqual(r_g.json()["ejercicio"]["id_gimnasio"], 10)

            # Recepcionista (aunque ve socios) NO puede crear/editar ejercicios de gimnasio -> 403
            self.assertEqual(
                self.client.post(
                    "/api/ejercicios",
                    json={"nombre": "No permitido"},
                    headers={"Authorization": f"Bearer {token_recep}"},
                ).status_code,
                403,
            )
            # Entrenador no puede transferir ejercicio_gimnasio a otro gimnasio -> 403
            self.assertEqual(
                self.client.put(
                    "/api/ejercicios/2001",
                    json={"id_gimnasio": 20, "nombre": "Transferido"},
                    headers={"Authorization": f"Bearer {token_trainer}"},
                ).status_code,
                403,
            )

    def test_06_rutinas_personales_sin_mezcla_y_reordenamiento_rpc(self):
        token_socio70 = self._make_token(self.uid_socio_70, "s70@test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            r_cr = self.client.post(
                "/api/rutinas/personales",
                json={"nombre": "Mi rutina en casa"},
                headers={"Authorization": f"Bearer {token_socio70}"},
            )
            self.assertEqual(r_cr.status_code, 201)
            rs_id = r_cr.json()["rutina"]["id_rutina_socio"]

            # Intento de mezclar ejercicio_gimnasio en rutina_socio -> 400
            self.assertEqual(
                self.client.post(
                    f"/api/rutinas/personales/{rs_id}/ejercicios",
                    json={"id_ejercicio_socio": 1001, "id_ejercicio_gimnasio": 2001, "orden": 1},
                    headers={"Authorization": f"Bearer {token_socio70}"},
                ).status_code,
                400,
            )
            # Intento de agregar ejercicio_socio 1003 de otro socio -> 403
            self.assertEqual(
                self.client.post(
                    f"/api/rutinas/personales/{rs_id}/ejercicios",
                    json={"id_ejercicio_socio": 1003, "orden": 1},
                    headers={"Authorization": f"Bearer {token_socio70}"},
                ).status_code,
                403,
            )

            # Agregar dos ejercicios propios (1001 y 1002) y reordenar vía rpc_reordenar_rutina_socio_ejercicios
            it1 = self.client.post(
                f"/api/rutinas/personales/{rs_id}/ejercicios",
                json={"id_ejercicio_socio": 1001, "series": 3, "repeticiones": 15, "orden": 1},
                headers={"Authorization": f"Bearer {token_socio70}"},
            ).json()["item"]["id_rutina_ejercicio_socio"]
            it2 = self.client.post(
                f"/api/rutinas/personales/{rs_id}/ejercicios",
                json={"id_ejercicio_socio": 1002, "series": 3, "repeticiones": 30, "orden": 2},
                headers={"Authorization": f"Bearer {token_socio70}"},
            ).json()["item"]["id_rutina_ejercicio_socio"]

            r_reord = self.client.put(
                f"/api/rutinas/personales/{rs_id}/reordenar",
                json={"ids_ordenados": [it2, it1]},
                headers={"Authorization": f"Bearer {token_socio70}"},
            )
            self.assertEqual(r_reord.status_code, 200)
            self.assertEqual(fake_db.rpc_calls[-1][0], "rpc_reordenar_rutina_socio_ejercicios")
            self.assertEqual([x["id_rutina_ejercicio_socio"] for x in r_reord.json()["ejercicios"]], [it2, it1])

    def test_07_rutinas_gimnasio_asignacion_sin_duplicar_y_permisos_recepcionista(self):
        token_trainer = self._make_token(self.uid_trainer_101, "trainer@test")
        token_recep = self._make_token(self.uid_recep_101, "recep@test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            # Recepcionista bloqueado (403) en crear rutina de gimnasio o asignar
            self.assertEqual(
                self.client.post(
                    "/api/rutinas/gimnasio",
                    json={"nombre": "No permitido"},
                    headers={"Authorization": f"Bearer {token_recep}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.post(
                    "/api/rutinas/asignaciones",
                    json={"id_rutina_gimnasio": 4001, "id_socio_gimnasio": 702},
                    headers={"Authorization": f"Bearer {token_recep}"},
                ).status_code,
                403,
            )

            # Intento de mezclar ejercicio_socio en rutina_gimnasio -> 400
            self.assertEqual(
                self.client.post(
                    "/api/rutinas/gimnasio/4001/ejercicios",
                    json={"id_ejercicio_gimnasio": 2001, "id_ejercicio_socio": 1001, "orden": 3},
                    headers={"Authorization": f"Bearer {token_trainer}"},
                ).status_code,
                400,
            )

            # Reordenar rutina_gimnasio 4001 vía rpc_reordenar_rutina_gimnasio_ejercicios
            r_reord = self.client.put(
                "/api/rutinas/gimnasio/4001/reordenar",
                json={"ids_ordenados": [6002, 6001]},
                headers={"Authorization": f"Bearer {token_trainer}"},
            )
            self.assertEqual(r_reord.status_code, 200)
            self.assertEqual(fake_db.rpc_calls[-1][0], "rpc_reordenar_rutina_gimnasio_ejercicios")

            # Asignar rutina 4001 a Socio 71 (sg 702) sin duplicar rutina_gimnasio
            total_plantillas_antes = len(fake_db.state["rutina_gimnasio"])
            r_asig = self.client.post(
                "/api/rutinas/asignaciones",
                json={"id_rutina_gimnasio": 4001, "id_socio_gimnasio": 702, "id_empleado_responsable": 31},
                headers={"Authorization": f"Bearer {token_trainer}"},
            )
            self.assertEqual(r_asig.status_code, 201)
            self.assertEqual(fake_db.rpc_calls[-1][0], "rpc_asignar_rutina_gimnasio_socio")
            self.assertEqual(len(fake_db.state["rutina_gimnasio"]), total_plantillas_antes)

    def test_08_responsable_historico_empleado_desactivado_y_reasignacion(self):
        token_dueno1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            # Empleado 32 está DESACTIVADO: NO puede recibir nuevas asignaciones ni reasignaciones -> 400
            r_new_inact = self.client.post(
                "/api/rutinas/asignaciones",
                json={"id_rutina_gimnasio": 4001, "id_socio_gimnasio": 702, "id_empleado_responsable": 32},
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_new_inact.status_code, 400)

            r_reasig_inact = self.client.patch(
                "/api/rutinas/asignaciones/7001/responsable",
                json={"id_empleado_responsable": 32},
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_reasig_inact.status_code, 400)

            # Sin embargo, la asignación previa 7001 (responsable histórico 32) SÍ puede finalizarse conservando id_empleado_responsable = 32
            r_fin = self.client.patch(
                "/api/rutinas/asignaciones/7001/finalizar",
                json={"observaciones": "Ciclo completado"},
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_fin.status_code, 200)
            asig = r_fin.json()["asignacion"]
            self.assertFalse(asig["estado"])
            self.assertEqual(asig["id_empleado_responsable"], 32)
            self.assertEqual(fake_db.rpc_calls[-1][0], "rpc_finalizar_rutina_asignada")

    # =========================================================================
    # MÓDULO 4 y 5: DASHBOARDS, SIN PROGRESO E INTEGRACIÓN (Tests 9 - 11)
    # =========================================================================

    def test_09_dashboard_admin_aislamiento_y_restriccion_financiera_entrenador(self):
        token_dueno1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        token_trainer = self._make_token(self.uid_trainer_101, "trainer@test")
        token_socio70 = self._make_token(self.uid_socio_70, "s70@test")
        p1, p2, _, _ = self._patch_env()
        with p1, p2:
            # Dueño 1 ve métricas completas de Gym 10 (incluyendo finanzas y rutinas) y NO Gym 20
            r_d1 = self.client.get("/api/dashboard/admin", headers={"Authorization": f"Bearer {token_dueno1}"})
            self.assertEqual(r_d1.status_code, 200)
            body = r_d1.json()
            self.assertEqual(body["gimnasio"]["id_gimnasio"], 10)
            self.assertTrue(body["alcance"]["acceso_financiero"])
            self.assertEqual(body["kpis"]["ingresos"]["mesActual"], 30000.0)
            self.assertEqual(body["kpis"]["rutinas"]["asignacionesActivas"], 1)

            # Dueño 1 bloqueado en Gym 20 -> 403
            self.assertEqual(
                self.client.get("/api/dashboard/admin?id_gimnasio=20", headers={"Authorization": f"Bearer {token_dueno1}"}).status_code,
                403,
            )

            # Entrenador NO recibe datos financieros (pago_select_authorized) pero sí rutinas y su sucursal
            r_tr = self.client.get("/api/dashboard/admin", headers={"Authorization": f"Bearer {token_trainer}"})
            self.assertEqual(r_tr.status_code, 200)
            tr_body = r_tr.json()
            self.assertFalse(tr_body["alcance"]["acceso_financiero"])
            self.assertIsNone(tr_body["kpis"]["ingresos"])
            self.assertEqual(tr_body["actividadReciente"]["pagos"], [])
            self.assertEqual(tr_body["alcance"]["sucursales_autorizadas"], [101])
            self.assertTrue(tr_body["alcance"]["acceso_rutinas"])

            # Socio bloqueado en dashboard organizacional -> 403
            self.assertEqual(
                self.client.get("/api/dashboard/admin", headers={"Authorization": f"Bearer {token_socio70}"}).status_code,
                403,
            )

    def test_10_dashboard_socio_sin_progreso_multigimnasio_y_checkin_rpc(self):
        token_socio71 = self._make_token(self.uid_socio_71, "s71@test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            # Dashboard del socio multi-gimnasio filtrado a Gym 20: SIN bloque progreso
            r_dash = self.client.get(
                "/api/socio/dashboard?id_gimnasio=20",
                headers={"Authorization": f"Bearer {token_socio71}"},
            )
            self.assertEqual(r_dash.status_code, 200)
            data = r_dash.json()
            self.assertNotIn("progreso", data)
            self.assertNotIn("id_gimnasio", data["socio"])
            self.assertEqual(data["gimnasio_activo"]["id_gimnasio"], 20)
            self.assertEqual(data["membresia"]["id_membresia_socio"], 803)
            self.assertIn("rutinas_personales", data)
            self.assertIn("rutinas_asignadas", data)

            # Endpoint legacy /api/socio/progreso eliminado (404)
            self.assertEqual(
                self.client.post(
                    "/api/socio/progreso",
                    json={"peso": 75},
                    headers={"Authorization": f"Bearer {token_socio71}"},
                ).status_code,
                404,
            )

            # Check-in desde panel del socio usa rpc_registrar_ingreso_asistencia
            r_ck = self.client.post(
                "/api/socio/checkin",
                json={"id_sucursal": 201},
                headers={"Authorization": f"Bearer {token_socio71}"},
            )
            self.assertEqual(r_ck.status_code, 201)
            self.assertEqual(fake_db.rpc_calls[-1][0], "rpc_registrar_ingreso_asistencia")

    def test_11_integracion_flujo_completo_multimodulo_y_cliente_rls(self):
        token_dueno1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        token_socio73 = self._make_token(self.uid_socio_73_sin_memb, "s73@test")
        p1, p2, fake_db, token_calls = self._patch_env()
        with p1, p2:
            # 1. Socio 73 solicita membresía pendiente vía panel socio (rpc_contratar_membresia)
            r_sol = self.client.post(
                "/api/socio/membresia/solicitar",
                json={"id_membresia_gimnasio": 501},
                headers={"Authorization": f"Bearer {token_socio73}"},
            )
            self.assertEqual(r_sol.status_code, 201)
            ms_id = r_sol.json()["membresia_socio"]["id_membresia_socio"]
            self.assertEqual(r_sol.json()["membresia_socio"]["estado"], "pendiente")

            # 2. Dueño 1 registra el pago aprobado vía /api/pagos (rpc_registrar_pago -> activa contrato)
            r_pago = self.client.post(
                "/api/pagos",
                json={"id_membresia_socio": ms_id, "metodo_pago": "transferencia", "comprobante": "INT-73-01"},
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_pago.status_code, 201)

            # 3. Ahora Socio 73 SÍ puede registrar ingreso en Sucursal 101 y salida vía RPC
            r_in = self.client.post(
                "/api/asistencias",
                json={"id_sucursal": 101},
                headers={"Authorization": f"Bearer {token_socio73}"},
            )
            self.assertEqual(r_in.status_code, 201)
            r_out = self.client.post(
                "/api/asistencias/salida",
                headers={"Authorization": f"Bearer {token_socio73}"},
            )
            self.assertEqual(r_out.status_code, 200)

            # 4. Dueño 1 consulta dashboard y refleja el pago + asistencia del flujo completo
            r_dash = self.client.get("/api/dashboard/admin", headers={"Authorization": f"Bearer {token_dueno1}"})
            self.assertEqual(r_dash.status_code, 200)
            self.assertEqual(r_dash.json()["kpis"]["ingresos"]["mesActual"], 60000.0)
            self.assertEqual(r_dash.json()["kpis"]["asistencias"]["hoy"], 1)

            # Verificar que cada petición invocó create_supabase_client_for_token con el JWT del actor
            self.assertEqual(
                token_calls,
                [token_socio73, token_dueno1, token_socio73, token_socio73, token_dueno1],
            )


if __name__ == "__main__":
    unittest.main()
