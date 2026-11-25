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


class FakeB23QueryBuilder:
    def __init__(self, table_name: str, state: Dict[str, List[Dict[str, Any]]]):
        self.table_name = table_name
        self.state = state
        self.filters_eq: Dict[str, Any] = {}
        self.filters_in: Dict[str, List[Any]] = {}
        self.is_single = False
        self._update_payload: Optional[Dict[str, Any]] = None
        self._insert_payload: Optional[List[Dict[str, Any]]] = None
        self._delete_requested = False

    def select(self, *_args, **_kwargs) -> "FakeB23QueryBuilder":
        return self

    def eq(self, col: str, val: Any) -> "FakeB23QueryBuilder":
        self.filters_eq[col] = val
        return self

    def in_(self, col: str, vals: List[Any]) -> "FakeB23QueryBuilder":
        self.filters_in[col] = list(vals)
        return self

    def order(self, *_args, **_kwargs) -> "FakeB23QueryBuilder":
        return self

    def maybe_single(self) -> "FakeB23QueryBuilder":
        self.is_single = True
        return self

    def single(self) -> "FakeB23QueryBuilder":
        self.is_single = True
        return self

    def update(self, payload: Dict[str, Any]) -> "FakeB23QueryBuilder":
        self._update_payload = payload
        return self

    def insert(self, rows: List[Dict[str, Any]]) -> "FakeB23QueryBuilder":
        self._insert_payload = rows
        return self

    def delete(self) -> "FakeB23QueryBuilder":
        self._delete_requested = True
        return self

    def _enrich_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        enriched = copy.deepcopy(row)
        gyms_by_id = {int(g["id_gimnasio"]): g for g in self.state.get("gimnasio", [])}
        sucs_by_id = {int(s["id_sucursal"]): s for s in self.state.get("sucursal", [])}
        socios_by_id = {int(s["id_socio"]): s for s in self.state.get("socio", [])}
        sgs_by_id = {int(sg["id_socio_gimnasio"]): sg for sg in self.state.get("socio_gimnasio", [])}
        planes_by_id = {
            int(p["id_membresia_gimnasio"]): p
            for p in self.state.get("membresia_gimnasio", [])
        }
        membs_by_id = {
            int(m["id_membresia_socio"]): m
            for m in self.state.get("membresia_socio", [])
        }

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

        return enriched

    def execute(self) -> FakeSupabaseResponse:
        if self._delete_requested:
            raise AssertionError(
                f"Prohibido ejecutar DELETE físico directo sobre {self.table_name} en el modelo definitivo."
            )

        # Vista de catálogo controlado `vw_catalogo_planes` (solo planes activos de gimnasios activos)
        if self.table_name == "vw_catalogo_planes":
            active_gyms = {
                int(g["id_gimnasio"])
                for g in self.state.get("gimnasio", [])
                if g.get("estado") is True
            }
            rows = [
                p
                for p in self.state.get("membresia_gimnasio", [])
                if p.get("estado") is True and int(p.get("id_gimnasio") or 0) in active_gyms
            ]
        else:
            rows = self.state.setdefault(self.table_name, [])

        if self._insert_payload is not None:
            if self.table_name in ("membresia_socio", "pago"):
                raise AssertionError(
                    f"Escritura directa sobre {self.table_name} prohibida: debe usarse la RPC autorizada."
                )
            inserted = []
            for item in self._insert_payload:
                new_row = copy.deepcopy(item)
                if self.table_name == "membresia_gimnasio" and "id_membresia_gimnasio" not in new_row:
                    new_id = max([int(r["id_membresia_gimnasio"]) for r in rows], default=500) + 1
                    new_row["id_membresia_gimnasio"] = new_id
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

        if self._update_payload is not None:
            if self.table_name in ("membresia_socio", "pago"):
                raise AssertionError(
                    f"UPDATE directo sobre {self.table_name} prohibido: debe usarse la RPC autorizada."
                )
            updated_out = []
            for r in matched_refs:
                r.update(copy.deepcopy(self._update_payload))
                updated_out.append(self._enrich_row(r))
            return FakeSupabaseResponse(updated_out, count=len(updated_out))

        enriched_out = [self._enrich_row(r) for r in matched_refs]
        if self.is_single:
            return FakeSupabaseResponse(enriched_out[0] if enriched_out else None)
        return FakeSupabaseResponse(enriched_out, count=len(enriched_out))


class FakeB23RpcBuilder:
    def __init__(self, fn_name: str, params: Dict[str, Any], client: "FakeB23SupabaseClient"):
        self.fn_name = fn_name
        self.params = params
        self.client = client

    def execute(self) -> FakeSupabaseResponse:
        self.client.rpc_calls.append((self.fn_name, copy.deepcopy(self.params)))
        if self.fn_name == "rpc_contratar_membresia":
            return FakeSupabaseResponse(self.client._rpc_contratar_membresia(self.params))
        if self.fn_name == "rpc_actualizar_estado_membresia":
            return FakeSupabaseResponse(self.client._rpc_actualizar_estado_membresia(self.params))
        if self.fn_name == "rpc_registrar_pago":
            return FakeSupabaseResponse(self.client._rpc_registrar_pago(self.params))
        if self.fn_name == "rpc_actualizar_estado_pago":
            return FakeSupabaseResponse(self.client._rpc_actualizar_estado_pago(self.params))
        raise RuntimeError(f"RPC no soportada en mock B2.3: {self.fn_name}")


class FakeB23SupabaseClient:
    """
    Simulador en memoria del contrato SQL definitivo (`gymtrack_instalacion_definitiva.sql`)
    para `membresia_gimnasio`, `vw_catalogo_planes`, `membresia_socio`, `pago` y sus 4 RPCs.
    """

    def __init__(self, state: Dict[str, List[Dict[str, Any]]], simulate_concurrent_overlap: bool = False):
        self.state = state
        self.rpc_calls: List[tuple[str, Dict[str, Any]]] = []
        self.simulate_concurrent_overlap = simulate_concurrent_overlap

    def from_(self, table_name: str) -> FakeB23QueryBuilder:
        return FakeB23QueryBuilder(table_name, self.state)

    def rpc(self, fn_name: str, params: Optional[Dict[str, Any]] = None) -> FakeB23RpcBuilder:
        return FakeB23RpcBuilder(fn_name, params or {}, self)

    def _rpc_contratar_membresia(self, params: Dict[str, Any]) -> Dict[str, Any]:
        sid = int(params["p_id_socio"])
        plan_id = int(params["p_id_membresia_gimnasio"])
        estado_solicitado = str(params.get("p_estado") or "activa").strip().lower()
        registrar_pago = bool(params.get("p_registrar_pago", False))
        metodo_pago = str(params.get("p_metodo_pago") or "efectivo").strip().lower()
        estado_pago = str(params.get("p_estado_pago") or "aprobado").strip().lower()
        comprobante = params.get("p_comprobante")
        monto_override = params.get("p_monto_override")

        plan = next(
            (p for p in self.state.get("membresia_gimnasio", []) if int(p["id_membresia_gimnasio"]) == plan_id),
            None,
        )
        if not plan:
            raise ValueError(f"El plan de membresía ({plan_id}) no existe.")
        if plan.get("estado") is not True:
            raise ValueError(f'El plan "{plan["nombre"]}" se encuentra inactivo y no admite nuevas contrataciones.')

        gid = int(plan["id_gimnasio"])
        gym = next((g for g in self.state.get("gimnasio", []) if int(g["id_gimnasio"]) == gid), None)
        if not gym or gym.get("estado") is not True:
            raise ValueError("El gimnasio asociado al plan se encuentra inactivo.")

        socio = next((s for s in self.state.get("socio", []) if int(s["id_socio"]) == sid), None)
        if not socio or socio.get("estado") is not True:
            raise ValueError(f"El socio ({sid}) no existe o se encuentra inactivo.")

        assoc = next(
            (
                sg
                for sg in self.state.get("socio_gimnasio", [])
                if int(sg["id_socio"]) == sid and int(sg["id_gimnasio"]) == gid
            ),
            None,
        )
        if not assoc or assoc.get("estado") is not True:
            raise ValueError(
                f"El socio no posee una asociación socio_gimnasio activa con el gimnasio {gid}."
            )

        sg_id = int(assoc["id_socio_gimnasio"])
        membs = self.state.setdefault("membresia_socio", [])

        if estado_solicitado == "pendiente":
            if any(
                int(m["id_socio_gimnasio"]) == sg_id
                and int(m["id_gimnasio"]) == gid
                and m.get("estado") == "pendiente"
                for m in membs
            ):
                raise ValueError(
                    "Ya existe una solicitud de membresía pendiente para esta asociación socio-gimnasio."
                )

        if registrar_pago and estado_pago != "aprobado" and estado_solicitado == "activa":
            raise ValueError(
                f'No se puede activar una contratación si el pago inicial registrado tiene estado "{estado_pago}".'
            )

        today = date.today()
        fecha_inicio = date.fromisoformat(str(params.get("p_fecha_inicio") or today.isoformat()))

        if self.simulate_concurrent_overlap:
            raise ValueError(
                f"Ya existe una membresía activa en el rango {fecha_inicio} a {fecha_inicio + timedelta(days=int(plan['duracion_dias']))} para esta asociación."
            )

        if estado_solicitado == "activa":
            vigentes = [
                date.fromisoformat(str(m["fecha_vencimiento"]))
                for m in membs
                if int(m["id_socio_gimnasio"]) == sg_id
                and int(m["id_gimnasio"]) == gid
                and m.get("estado") == "activa"
                and date.fromisoformat(str(m["fecha_vencimiento"])) >= today
            ]
            if vigentes:
                max_venc = max(vigentes)
                if fecha_inicio <= max_venc:
                    fecha_inicio = max_venc + timedelta(days=1)

        duracion = int(plan["duracion_dias"])
        fecha_vencimiento = fecha_inicio + timedelta(days=duracion)

        if estado_solicitado == "activa":
            for m in membs:
                if (
                    int(m["id_socio_gimnasio"]) == sg_id
                    and int(m["id_gimnasio"]) == gid
                    and m.get("estado") == "activa"
                ):
                    m_ini = date.fromisoformat(str(m["fecha_inicio"]))
                    m_ven = date.fromisoformat(str(m["fecha_vencimiento"]))
                    if m_ini <= fecha_vencimiento and m_ven >= fecha_inicio:
                        raise ValueError(
                            f"Ya existe una membresía activa en el rango {fecha_inicio} a {fecha_vencimiento} para esta asociación."
                        )

        precio = round(float(monto_override if monto_override is not None else plan["precio"]), 2)
        estado_inicial = "pendiente" if (registrar_pago and estado_pago != "aprobado") else estado_solicitado
        new_id = max([int(m["id_membresia_socio"]) for m in membs], default=800) + 1
        contrato = {
            "id_membresia_socio": new_id,
            "id_socio_gimnasio": sg_id,
            "id_membresia_gimnasio": plan_id,
            "id_gimnasio": gid,
            "precio": precio,
            "duracion_dias_contratados": duracion,
            "limite_asistencias_semanales": plan.get("limite_asistencias_semanales"),
            "limite_asistencias_totales": plan.get("limite_asistencias_totales"),
            "fecha_inicio": fecha_inicio.isoformat(),
            "fecha_vencimiento": fecha_vencimiento.isoformat(),
            "estado": estado_inicial,
            "creado_en": datetime.now(timezone.utc).isoformat(),
        }
        membs.append(contrato)

        pago_creado = None
        if registrar_pago:
            pago_creado = self._rpc_registrar_pago(
                {
                    "p_id_membresia_socio": new_id,
                    "p_monto": precio,
                    "p_metodo_pago": metodo_pago,
                    "p_estado": estado_pago,
                    "p_comprobante": comprobante,
                    "p_fecha_pago": datetime.now(timezone.utc).isoformat(),
                }
            )

        return {
            "socio_gimnasio": copy.deepcopy(assoc),
            "membresia_socio": copy.deepcopy(contrato),
            "pago": pago_creado,
        }

    def _rpc_actualizar_estado_membresia(self, params: Dict[str, Any]) -> Dict[str, Any]:
        ms_id = int(params["p_id_membresia_socio"])
        nuevo_estado = params.get("p_estado")
        nueva_fecha_venc = params.get("p_fecha_vencimiento")

        membs = self.state.get("membresia_socio", [])
        contrato = next((m for m in membs if int(m["id_membresia_socio"]) == ms_id), None)
        if not contrato:
            raise ValueError(f"La membresía ({ms_id}) no existe.")

        if contrato.get("estado") == "cancelada" and nuevo_estado and nuevo_estado != "cancelada":
            raise ValueError("Una contratación cancelada no puede reactivarse.")

        if nuevo_estado == "activa" and contrato.get("estado") == "pendiente":
            total_aprobado = sum(
                float(p.get("monto") or 0)
                for p in self.state.get("pago", [])
                if int(p.get("id_membresia_socio") or 0) == ms_id and p.get("estado") == "aprobado"
            )
            if round(total_aprobado, 2) < round(float(contrato["precio"]), 2):
                raise ValueError("No se puede activar una membresía pendiente sin pagos aprobados que cubran el precio.")

        if nueva_fecha_venc is not None:
            if date.fromisoformat(str(nueva_fecha_venc)) < date.fromisoformat(str(contrato["fecha_inicio"])):
                raise ValueError("La fecha_vencimiento no puede ser anterior a fecha_inicio.")
            contrato["fecha_vencimiento"] = str(nueva_fecha_venc)

        if nuevo_estado is not None:
            contrato["estado"] = str(nuevo_estado)

        return copy.deepcopy(contrato)

    def _rpc_registrar_pago(self, params: Dict[str, Any]) -> Dict[str, Any]:
        ms_id = int(params["p_id_membresia_socio"])
        monto = round(float(params["p_monto"]), 2)
        metodo = str(params["p_metodo_pago"]).strip().lower()
        estado = str(params.get("p_estado") or "aprobado").strip().lower()
        comprobante = params.get("p_comprobante")
        fecha_pago = params.get("p_fecha_pago") or datetime.now(timezone.utc).isoformat()

        contrato = next(
            (m for m in self.state.get("membresia_socio", []) if int(m["id_membresia_socio"]) == ms_id),
            None,
        )
        if not contrato:
            raise ValueError(f"La contratación ({ms_id}) no existe.")
        if contrato.get("estado") == "cancelada":
            raise ValueError("No se pueden registrar pagos sobre una contratación cancelada.")

        gid = int(contrato["id_gimnasio"])
        pagos = self.state.setdefault("pago", [])

        if comprobante:
            comp_norm = str(comprobante).strip().lower()
            membs_by_id = {int(m["id_membresia_socio"]): m for m in self.state.get("membresia_socio", [])}
            for p in pagos:
                if p.get("estado") == "anulado" or not p.get("comprobante"):
                    continue
                pm = membs_by_id.get(int(p["id_membresia_socio"]))
                if pm and int(pm["id_gimnasio"]) == gid and str(p["comprobante"]).strip().lower() == comp_norm:
                    raise ValueError(f"Ya existe un pago activo con el comprobante '{comprobante}' en este gimnasio.")

        precio = round(float(contrato["precio"]), 2)
        if precio > 0 and monto == 0 and estado == "aprobado":
            raise ValueError("No se puede aprobar un pago de monto 0 en un plan con precio mayor a 0.")

        total_aprobado = round(
            sum(
                float(p["monto"])
                for p in pagos
                if int(p["id_membresia_socio"]) == ms_id and p.get("estado") == "aprobado"
            ),
            2,
        )
        if estado in ("pendiente", "aprobado") and round(total_aprobado + monto, 2) > precio:
            raise ValueError("El pago supera el importe contratado (sobrepago prohibido).")

        new_id = max([int(p["id_pago"]) for p in pagos], default=900) + 1
        pago = {
            "id_pago": new_id,
            "fecha_pago": fecha_pago,
            "monto": monto,
            "metodo_pago": metodo,
            "estado": estado,
            "comprobante": comprobante,
            "id_membresia_socio": ms_id,
        }
        pagos.append(pago)

        # Activación automática de contrato pendiente al cubrir el precio contratado
        if estado == "aprobado" and contrato.get("estado") == "pendiente":
            if round(total_aprobado + monto, 2) >= precio:
                contrato["estado"] = "activa"

        return copy.deepcopy(pago)

    def _rpc_actualizar_estado_pago(self, params: Dict[str, Any]) -> Dict[str, Any]:
        pid = int(params["p_id_pago"])
        nuevo_estado = str(params["p_estado"]).strip().lower()

        pagos = self.state.get("pago", [])
        pago = next((p for p in pagos if int(p["id_pago"]) == pid), None)
        if not pago:
            raise ValueError(f"El pago ({pid}) no existe.")

        actual = str(pago["estado"]).lower()
        if actual == "anulado" and nuevo_estado != "anulado":
            raise ValueError("Un pago anulado es inmutable.")
        if actual == "aprobado" and nuevo_estado not in ("aprobado", "anulado"):
            raise ValueError("Un pago aprobado solo puede transicionar a anulado.")
        if actual == "rechazado" and nuevo_estado not in ("rechazado", "anulado"):
            raise ValueError("Un pago rechazado solo puede transicionar a anulado.")

        pago["estado"] = nuevo_estado

        # Recalcular estado del contrato si correspondía
        ms_id = int(pago["id_membresia_socio"])
        contrato = next(
            (m for m in self.state.get("membresia_socio", []) if int(m["id_membresia_socio"]) == ms_id),
            None,
        )
        if contrato:
            precio = round(float(contrato["precio"]), 2)
            total_aprobado = round(
                sum(
                    float(p["monto"])
                    for p in pagos
                    if int(p["id_membresia_socio"]) == ms_id and p.get("estado") == "aprobado"
                ),
                2,
            )
            if actual == "pendiente" and nuevo_estado == "aprobado" and contrato["estado"] == "pendiente":
                if total_aprobado >= precio:
                    contrato["estado"] = "activa"
            elif actual == "aprobado" and nuevo_estado == "anulado" and contrato["estado"] == "activa":
                if total_aprobado < precio:
                    venc = date.fromisoformat(str(contrato["fecha_vencimiento"]))
                    contrato["estado"] = "vencida" if venc < date.today() else "pendiente"

        return copy.deepcopy(pago)


class TestPlanesMembresiasPagosB23(unittest.TestCase):
    """
    Suite de pruebas unitarias de la Fase B2.3: Planes, Membresías y Pagos.
    Cubre los 20 casos obligatorios del contrato de `gymtrack_instalacion_definitiva.sql`.
    """

    @classmethod
    def setUpClass(cls):
        cls.ec_private_key = ec.generate_private_key(ec.SECP256R1())
        cls.ec_public_key = cls.ec_private_key.public_key()
        cls.issuer = "https://qdlkljSb.supabase.co/auth/v1"
        cls.audience = "authenticated"

        cls.uid_dueno_1 = "22222222-2222-4222-8222-000000000001"
        cls.uid_dueno_2 = "22222222-2222-4222-8222-000000000002"
        cls.uid_admin_org_1 = "22222222-2222-4222-8222-000000000003"
        cls.uid_recep_1 = "22222222-2222-4222-8222-000000000004"
        cls.uid_trainer_1 = "22222222-2222-4222-8222-000000000005"
        cls.uid_socio_70 = "22222222-2222-4222-8222-000000000006"
        cls.uid_socio_71 = "22222222-2222-4222-8222-000000000007"
        cls.uid_socio_72_baja = "22222222-2222-4222-8222-000000000008"

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
        return jwt.encode(payload, self.ec_private_key, algorithm="ES256", headers={"kid": "test-kid-b23"})

    def _build_initial_state(self) -> Dict[str, List[Dict[str, Any]]]:
        today = date.today()
        return {
            "dueno": [
                {
                    "id_dueno": 1,
                    "auth_user_id": self.uid_dueno_1,
                    "nombre": "Martín",
                    "apellido": "Rivas",
                    "email": "dueno1@gymtrack.test",
                    "fecha_creacion": "2026-01-01T00:00:00Z",
                },
                {
                    "id_dueno": 2,
                    "auth_user_id": self.uid_dueno_2,
                    "nombre": "Valeria",
                    "apellido": "Gómez",
                    "email": "dueno2@gymtrack.test",
                    "fecha_creacion": "2026-01-01T00:00:00Z",
                },
            ],
            "gimnasio": [
                {
                    "id_gimnasio": 10,
                    "nombre": "Gimnasio Norte Fit",
                    "telefono": "+54 11 4000-1100",
                    "email": "norte@gymtrack.test",
                    "fecha_registro": "2026-01-01",
                    "estado": True,
                    "id_dueno": 1,
                },
                {
                    "id_gimnasio": 20,
                    "nombre": "Gimnasio Sur Power",
                    "telefono": "+54 11 4000-2200",
                    "email": "sur@gymtrack.test",
                    "fecha_registro": "2026-01-01",
                    "estado": True,
                    "id_dueno": 2,
                },
            ],
            "sucursal": [
                {
                    "id_sucursal": 101,
                    "direccion": "Av. Libertador 1250",
                    "telefono": "+54 11 4000-1101",
                    "descripcion": "Sede Principal Norte",
                    "fecha_registro": "2026-01-01",
                    "estado": True,
                    "id_gimnasio": 10,
                },
                {
                    "id_sucursal": 201,
                    "direccion": "Av. Costanera 900",
                    "telefono": "+54 11 4000-2201",
                    "descripcion": "Sede Única Sur",
                    "fecha_registro": "2026-01-01",
                    "estado": True,
                    "id_gimnasio": 20,
                },
            ],
            "administrador": [
                {
                    "id_administrador": 15,
                    "auth_user_id": self.uid_admin_org_1,
                    "nombre": "Laura",
                    "apellido": "Admin",
                    "email": "admin1@gymtrack.test",
                    "estado": True,
                    "fecha_creacion": "2026-01-01T00:00:00Z",
                }
            ],
            "administrador_gimnasio": [
                {
                    "id_administrador_gimnasio": 150,
                    "id_administrador": 15,
                    "id_gimnasio": 10,
                    "estado": True,
                    "fecha_asignacion": "2026-01-01T00:00:00Z",
                }
            ],
            "empleado": [
                {
                    "id_empleado": 30,
                    "auth_user_id": self.uid_recep_1,
                    "nombre": "Sofía",
                    "apellido": "Paz",
                    "dni": "33100102",
                    "email": "recep1@gymtrack.test",
                    "telefono": "+54 9 11 5555-0102",
                    "estado": True,
                    "fecha_creacion": "2026-01-01T00:00:00Z",
                },
                {
                    "id_empleado": 31,
                    "auth_user_id": self.uid_trainer_1,
                    "nombre": "Julián",
                    "apellido": "Castro",
                    "dni": "34100103",
                    "email": "trainer1@gymtrack.test",
                    "telefono": "+54 9 11 5555-0103",
                    "estado": True,
                    "fecha_creacion": "2026-01-01T00:00:00Z",
                },
            ],
            "empleado_sucursal": [
                {
                    "id_empleado_sucursal": 301,
                    "rol": "recepcionista",
                    "id_empleado": 30,
                    "id_sucursal": 101,
                    "estado": True,
                    "fecha_asignacion": "2026-01-01T00:00:00Z",
                },
                {
                    "id_empleado_sucursal": 302,
                    "rol": "entrenador",
                    "id_empleado": 31,
                    "id_sucursal": 101,
                    "estado": True,
                    "fecha_asignacion": "2026-01-01T00:00:00Z",
                },
            ],
            "socio": [
                {
                    "id_socio": 70,
                    "auth_user_id": self.uid_socio_70,
                    "dni": "40111002",
                    "telefono": "+54 9 11 6000-0002",
                    "fecha_alta": "2026-01-10",
                    "estado": True,
                    "nombre": "Lucía",
                    "apellido": "Pereyra",
                    "email": "socio70@gymtrack.test",
                },
                {
                    "id_socio": 71,
                    "auth_user_id": self.uid_socio_71,
                    "dni": "40111001",
                    "telefono": "+54 9 11 6000-0001",
                    "fecha_alta": "2026-01-05",
                    "estado": True,
                    "nombre": "Carlos",
                    "apellido": "MultiGym",
                    "email": "socio71@gymtrack.test",
                },
                {
                    "id_socio": 72,
                    "auth_user_id": self.uid_socio_72_baja,
                    "dni": "40111004",
                    "telefono": "+54 9 11 6000-0004",
                    "fecha_alta": "2026-01-01",
                    "estado": True,
                    "nombre": "Paula",
                    "apellido": "BajaGym",
                    "email": "socio72@gymtrack.test",
                },
            ],
            "socio_gimnasio": [
                {
                    "id_socio_gimnasio": 701,
                    "id_socio": 70,
                    "id_gimnasio": 10,
                    "fecha_alta": "2026-01-10",
                    "estado": True,
                    "creado_en": "2026-01-10T10:00:00Z",
                },
                {
                    "id_socio_gimnasio": 702,
                    "id_socio": 71,
                    "id_gimnasio": 10,
                    "fecha_alta": "2026-01-05",
                    "estado": True,
                    "creado_en": "2026-01-05T10:00:00Z",
                },
                {
                    "id_socio_gimnasio": 703,
                    "id_socio": 71,
                    "id_gimnasio": 20,
                    "fecha_alta": "2026-01-15",
                    "estado": True,
                    "creado_en": "2026-01-15T10:00:00Z",
                },
                {
                    "id_socio_gimnasio": 704,
                    "id_socio": 72,
                    "id_gimnasio": 10,
                    "fecha_alta": "2026-01-01",
                    "estado": False,
                    "creado_en": "2026-01-01T10:00:00Z",
                },
            ],
            "membresia_gimnasio": [
                {
                    "id_membresia_gimnasio": 501,
                    "nombre": "Plan Full Mensual",
                    "descripcion": "Acceso libre 30 días",
                    "precio": 30000.0,
                    "duracion_dias": 30,
                    "limite_asistencias_semanales": None,
                    "limite_asistencias_totales": None,
                    "estado": True,
                    "id_gimnasio": 10,
                },
                {
                    "id_membresia_gimnasio": 502,
                    "nombre": "Plan Inactivo Histórico",
                    "descripcion": "Ya no se comercializa",
                    "precio": 15000.0,
                    "duracion_dias": 30,
                    "limite_asistencias_semanales": 3,
                    "limite_asistencias_totales": 12,
                    "estado": False,
                    "id_gimnasio": 10,
                },
                {
                    "id_membresia_gimnasio": 503,
                    "nombre": "Plan Sur Elite",
                    "descripcion": "Acceso en Gimnasio Sur",
                    "precio": 40000.0,
                    "duracion_dias": 30,
                    "limite_asistencias_semanales": None,
                    "limite_asistencias_totales": None,
                    "estado": True,
                    "id_gimnasio": 20,
                },
            ],
            "membresia_socio": [
                # Contrato 801: Socio 70 en Gym 10, ACTIVO con 10 días restantes
                {
                    "id_membresia_socio": 801,
                    "id_socio_gimnasio": 701,
                    "id_membresia_gimnasio": 501,
                    "id_gimnasio": 10,
                    "precio": 30000.0,
                    "duracion_dias_contratados": 30,
                    "limite_asistencias_semanales": None,
                    "limite_asistencias_totales": None,
                    "fecha_inicio": (today - timedelta(days=20)).isoformat(),
                    "fecha_vencimiento": (today + timedelta(days=10)).isoformat(),
                    "estado": "activa",
                    "creado_en": "2026-11-01T10:00:00Z",
                },
                # Contrato 802: Socio 71 en Gym 10, VENCIDO hace 5 días
                {
                    "id_membresia_socio": 802,
                    "id_socio_gimnasio": 702,
                    "id_membresia_gimnasio": 501,
                    "id_gimnasio": 10,
                    "precio": 30000.0,
                    "duracion_dias_contratados": 30,
                    "limite_asistencias_semanales": None,
                    "limite_asistencias_totales": None,
                    "fecha_inicio": (today - timedelta(days=35)).isoformat(),
                    "fecha_vencimiento": (today - timedelta(days=5)).isoformat(),
                    "estado": "vencida",
                    "creado_en": "2026-10-01T10:00:00Z",
                },
                # Contrato 803: Socio 71 en Gym 20, PENDIENTE de pago
                {
                    "id_membresia_socio": 803,
                    "id_socio_gimnasio": 703,
                    "id_membresia_gimnasio": 503,
                    "id_gimnasio": 20,
                    "precio": 40000.0,
                    "duracion_dias_contratados": 30,
                    "limite_asistencias_semanales": None,
                    "limite_asistencias_totales": None,
                    "fecha_inicio": today.isoformat(),
                    "fecha_vencimiento": (today + timedelta(days=30)).isoformat(),
                    "estado": "pendiente",
                    "creado_en": "2026-11-20T10:00:00Z",
                },
            ],
            "pago": [
                # Pago 901: Pago APROBADO del contrato 801 (Gym 10, Socio 70)
                {
                    "id_pago": 901,
                    "fecha_pago": "2026-11-01T10:05:00Z",
                    "monto": 30000.0,
                    "metodo_pago": "efectivo",
                    "estado": "aprobado",
                    "comprobante": "REC-10-001",
                    "id_membresia_socio": 801,
                },
                # Pago 902: Pago PENDIENTE parcial del contrato 803 (Gym 20, Socio 71)
                {
                    "id_pago": 902,
                    "fecha_pago": "2026-11-20T10:05:00Z",
                    "monto": 10000.0,
                    "metodo_pago": "transferencia",
                    "estado": "pendiente",
                    "comprobante": "REC-20-001",
                    "id_membresia_socio": 803,
                },
            ],
        }

    def _patch_env(
        self,
        state: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        simulate_concurrent_overlap: bool = False,
    ):
        fake_db = FakeB23SupabaseClient(
            state if state is not None else self._build_initial_state(),
            simulate_concurrent_overlap=simulate_concurrent_overlap,
        )
        token_calls: List[str] = []

        def _fake_verify(token: str):
            return verify_supabase_access_token(
                token,
                verification_key=self.ec_public_key,
                expected_issuer=self.issuer,
                expected_audience=self.audience,
            )

        def _fake_create_client(access_token: str):
            token_calls.append(access_token)
            return fake_db

        p1 = patch("app.core.dependencies.verify_supabase_access_token", side_effect=_fake_verify)
        p2 = patch("app.core.dependencies.create_supabase_client_for_token", side_effect=_fake_create_client)
        return p1, p2, fake_db, token_calls

    # =========================================================================
    # PLANES (Tests 1 - 5)
    # =========================================================================

    # 1. Crear plan para gimnasio autorizado + consultar catálogo controlado
    def test_01_crear_plan_autorizado_y_catalogo(self):
        token_dueno1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        token_admin1 = self._make_token(self.uid_admin_org_1, "admin1@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            res = self.client.post(
                "/api/planes-membresia",
                json={
                    "nombre": "Plan Trimestral VIP",
                    "descripcion": "90 días + pase libre",
                    "precio": 75000,
                    "duracion_dias": 90,
                    "limite_asistencias_semanales": 6,
                },
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(res.status_code, 201)
            plan = res.json()["plan"]
            self.assertEqual(plan["id_gimnasio"], 10)
            self.assertEqual(plan["nombre"], "Plan Trimestral VIP")
            self.assertEqual(plan["precio"], 75000.0)
            self.assertTrue(plan["estado"])

            # Administrador organizacional también puede desactivar lógicamente el plan sin borrarlo
            res_del = self.client.delete(
                f"/api/planes-membresia/{plan['id_membresia_gimnasio']}",
                headers={"Authorization": f"Bearer {token_admin1}"},
            )
            self.assertEqual(res_del.status_code, 200)
            self.assertFalse(res_del.json()["plan"]["estado"])
            # Catálogo controlado (`vw_catalogo_planes`) solo expone planes activos
            res_cat = self.client.get(
                "/api/planes-membresia/catalogo",
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(res_cat.status_code, 200)
            cat_ids = {p["id_membresia_gimnasio"] for p in res_cat.json()["planes"]}
            self.assertIn(501, cat_ids)
            self.assertNotIn(502, cat_ids)
            self.assertNotIn(plan["id_membresia_gimnasio"], cat_ids)

    # 2. Rechazar creación de plan por usuario sin permisos (recepcionista, entrenador, socio)
    def test_02_rechazar_creacion_plan_sin_permisos_403(self):
        payload = {"nombre": "Plan No Autorizado", "precio": 10000, "duracion_dias": 30}
        for uid, email in [
            (self.uid_recep_1, "recep1@gymtrack.test"),
            (self.uid_trainer_1, "trainer1@gymtrack.test"),
            (self.uid_socio_70, "socio70@gymtrack.test"),
        ]:
            token = self._make_token(uid, email)
            p1, p2, _, _ = self._patch_env()
            with p1, p2:
                res = self.client.post(
                    "/api/planes-membresia",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
                self.assertEqual(res.status_code, 403, msg=f"Esperado 403 para {email}, obtenido {res.status_code}")

    # 3. Impedir consultar o modificar planes de otro gimnasio y transferencia de gimnasio
    def test_03_impedir_consultar_o_modificar_planes_otro_gimnasio_403(self):
        token_dueno1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        p1, p2, _, _ = self._patch_env()
        with p1, p2:
            # Listar planes de Gym 20 siendo Dueño 1 -> 403
            self.assertEqual(
                self.client.get(
                    "/api/planes-membresia?id_gimnasio=20",
                    headers={"Authorization": f"Bearer {token_dueno1}"},
                ).status_code,
                403,
            )
            # Consultar plan 503 (Gym 20) -> 403
            self.assertEqual(
                self.client.get(
                    "/api/planes-membresia/503",
                    headers={"Authorization": f"Bearer {token_dueno1}"},
                ).status_code,
                403,
            )
            # Modificar plan 503 (Gym 20) -> 403
            self.assertEqual(
                self.client.put(
                    "/api/planes-membresia/503",
                    json={"precio": 1},
                    headers={"Authorization": f"Bearer {token_dueno1}"},
                ).status_code,
                403,
            )
            # Transferir plan propio 501 a Gym 20 -> 403
            self.assertEqual(
                self.client.put(
                    "/api/planes-membresia/501",
                    json={"id_gimnasio": 20, "precio": 31000},
                    headers={"Authorization": f"Bearer {token_dueno1}"},
                ).status_code,
                403,
            )

    # 4. Impedir contratar un plan inactivo
    def test_04_impedir_contratar_plan_inactivo_400(self):
        token_recep = self._make_token(self.uid_recep_1, "recep1@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            res = self.client.post(
                "/api/membresias",
                json={"id_socio": 71, "id_membresia_gimnasio": 502, "estado": "activa"},
                headers={"Authorization": f"Bearer {token_recep}"},
            )
            self.assertEqual(res.status_code, 400)
            self.assertEqual(len(fake_db.rpc_calls), 0)

    # 5. Impedir cruzar el gimnasio de un plan con el de una contratación
    def test_05_impedir_cruzar_gimnasio_plan_y_contratacion_403(self):
        token_dueno1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            # Dueño 1 (Gym 10) intenta contratar el plan 503 (Gym 20) declarando id_gimnasio=10 -> 403
            res_body = self.client.post(
                "/api/membresias",
                json={"id_socio": 71, "id_membresia_gimnasio": 503, "id_gimnasio": 10},
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(res_body.status_code, 403)
            # Mismo intento usando header X-Gimnasio-Id: 10 -> 403
            res_hdr = self.client.post(
                "/api/membresias",
                json={"id_socio": 71, "id_membresia_gimnasio": 503},
                headers={"Authorization": f"Bearer {token_dueno1}", "X-Gimnasio-Id": "10"},
            )
            self.assertEqual(res_hdr.status_code, 403)
            self.assertEqual(len(fake_db.rpc_calls), 0)

    # =========================================================================
    # MEMBRESÍAS Y RENOVACIONES (Tests 6 - 14)
    # =========================================================================

    # 6. Contratar membresía vía rpc_contratar_membresia (y rechazar manipulación de precio)
    def test_06_contratar_membresia_via_rpc_y_bloquear_override_precio(self):
        token_recep = self._make_token(self.uid_recep_1, "recep1@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            # Intento de imponer precio desde el cliente -> 403
            res_tamper = self.client.post(
                "/api/membresias",
                json={"id_socio": 71, "id_membresia_gimnasio": 501, "precio": 1},
                headers={"Authorization": f"Bearer {token_recep}"},
            )
            self.assertEqual(res_tamper.status_code, 403)

            # Contratación legítima para Socio 71 (asociación activa 702 en Gym 10) con pago inmediato
            res = self.client.post(
                "/api/membresias",
                json={
                    "id_socio": 71,
                    "id_membresia_gimnasio": 501,
                    "estado": "activa",
                    "registrar_pago": True,
                    "metodo_pago": "transferencia",
                    "estado_pago": "aprobado",
                    "comprobante": "TRX-71-001",
                },
                headers={"Authorization": f"Bearer {token_recep}"},
            )
            self.assertEqual(res.status_code, 201)
            body = res.json()
            self.assertEqual(fake_db.rpc_calls[-1][0], "rpc_contratar_membresia")
            self.assertIsNone(fake_db.rpc_calls[-1][1]["p_monto_override"])
            self.assertEqual(body["membresia_socio"]["precio"], 30000.0)
            self.assertEqual(body["membresia_socio"]["id_gimnasio"], 10)
            self.assertEqual(body["membresia_socio"]["estado"], "activa")
            self.assertIsNotNone(body["pago"])
            self.assertEqual(body["pago"]["monto"], 30000.0)

    # 7. Rechazar contratación sin asociación socio_gimnasio activa
    def test_07_rechazar_contratacion_sin_asociacion_activa_400(self):
        token_recep = self._make_token(self.uid_recep_1, "recep1@gymtrack.test")
        token_dueno2 = self._make_token(self.uid_dueno_2, "dueno2@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            # Socio 72 tiene asociación 704 INACTIVA (estado=False) en Gym 10 -> 400
            res_inactiva = self.client.post(
                "/api/membresias",
                json={"id_socio": 72, "id_membresia_gimnasio": 501},
                headers={"Authorization": f"Bearer {token_recep}"},
            )
            self.assertEqual(res_inactiva.status_code, 400)

            # Socio 70 NO posee ninguna asociación en Gym 20 -> 400
            res_inexistente = self.client.post(
                "/api/membresias",
                json={"id_socio": 70, "id_membresia_gimnasio": 503},
                headers={"Authorization": f"Bearer {token_dueno2}"},
            )
            self.assertEqual(res_inexistente.status_code, 400)
            self.assertEqual(len(fake_db.rpc_calls), 0)

    # 8. Rechazar contrataciones duplicadas (segunda contratación pendiente en el mismo gimnasio)
    def test_08_rechazar_contratacion_duplicada_pendiente_409(self):
        token_dueno2 = self._make_token(self.uid_dueno_2, "dueno2@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            # Socio 71 ya tiene el contrato 803 en estado 'pendiente' en Gym 20
            res_dup = self.client.post(
                "/api/membresias",
                json={"id_socio": 71, "id_membresia_gimnasio": 503, "estado": "pendiente"},
                headers={"Authorization": f"Bearer {token_dueno2}"},
            )
            self.assertEqual(res_dup.status_code, 409)
            self.assertEqual(fake_db.rpc_calls[-1][0], "rpc_contratar_membresia")

    # 9. Consultar historial de contrataciones (staff y socio multi-gimnasio)
    def test_09_consultar_historial_contrataciones(self):
        token_dueno1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        token_socio71 = self._make_token(self.uid_socio_71, "socio71@gymtrack.test")
        p1, p2, _, _ = self._patch_env()
        with p1, p2:
            # Dueño 1 ve únicamente contratos de Gym 10 (801 y 802, NO 803 de Gym 20)
            res_staff = self.client.get(
                "/api/membresias",
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(res_staff.status_code, 200)
            ids_gym10 = {m["id_membresia_socio"] for m in res_staff.json()["membresias"]}
            self.assertEqual(ids_gym10, {801, 802})

            # Socio 71 ve su historial multi-gimnasio (802 en Gym 10 y 803 en Gym 20, NO 801 de Socio 70)
            res_socio = self.client.get(
                "/api/membresias/mis-membresias",
                headers={"Authorization": f"Bearer {token_socio71}"},
            )
            self.assertEqual(res_socio.status_code, 200)
            ids_socio71 = {m["id_membresia_socio"] for m in res_socio.json()["membresias"]}
            self.assertEqual(ids_socio71, {802, 803})

    # 10. Renovar antes del vencimiento cancelando la anterior y sumando días restantes
    def test_10_renovar_antes_del_vencimiento_conserva_dias_restantes(self):
        token_recep = self._make_token(self.uid_recep_1, "recep1@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            hoy = date.today()
            prev_801 = copy.deepcopy(
                next(m for m in fake_db.state["membresia_socio"] if m["id_membresia_socio"] == 801)
            )
            venc_anterior = date.fromisoformat(prev_801["fecha_vencimiento"])
            dias_restantes = max(0, (venc_anterior - hoy).days)

            res = self.client.post(
                "/api/membresias/renovar",
                json={
                    "id_socio": 70,
                    "id_membresia_gimnasio": 501,
                    "estado": "activa",
                    "registrar_pago": True,
                    "metodo_pago": "efectivo",
                    "comprobante": "REN-70-ANTICIPADA",
                },
                headers={"Authorization": f"Bearer {token_recep}"},
            )
            self.assertEqual(res.status_code, 201)
            nueva = res.json()["membresia_socio"]
            self.assertEqual(nueva["fecha_inicio"], hoy.isoformat())
            self.assertEqual(nueva["fecha_vencimiento"], (hoy + timedelta(days=30 + dias_restantes)).isoformat())
            self.assertEqual(nueva["estado"], "activa")
            self.assertEqual(res.json()["dias_transferidos"], dias_restantes)
            self.assertNotEqual(nueva["id_membresia_socio"], 801)
            post_801 = next(m for m in fake_db.state["membresia_socio"] if m["id_membresia_socio"] == 801)
            self.assertEqual(post_801["estado"], "cancelada")

    # 11. Renovar después del vencimiento iniciando desde la fecha actual
    def test_11_renovar_despues_del_vencimiento_inicia_hoy(self):
        token_recep = self._make_token(self.uid_recep_1, "recep1@gymtrack.test")
        p1, p2, _, _ = self._patch_env()
        with p1, p2:
            # Socio 71 tiene contrato 802 vencido hace 5 días en Gym 10
            res = self.client.post(
                "/api/membresias/renovar",
                json={
                    "id_socio": 71,
                    "id_membresia_gimnasio": 501,
                    "estado": "activa",
                },
                headers={"Authorization": f"Bearer {token_recep}"},
            )
            self.assertEqual(res.status_code, 201)
            nueva = res.json()["membresia_socio"]
            today = date.today()
            self.assertEqual(nueva["fecha_inicio"], today.isoformat())
            self.assertEqual(nueva["fecha_vencimiento"], (today + timedelta(days=30)).isoformat())

    # 12. Crear nueva contratación sin modificar la anterior (inmutabilidad histórica)
    def test_12_renovacion_crea_nueva_fila_sin_modificar_contratacion_anterior(self):
        token_dueno1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            snapshot_801 = copy.deepcopy(
                next(m for m in fake_db.state["membresia_socio"] if m["id_membresia_socio"] == 801)
            )
            total_antes = len(fake_db.state["membresia_socio"])

            res_ren = self.client.post(
                "/api/membresias/renovar",
                json={"id_socio": 70, "id_membresia_gimnasio": 501, "estado": "activa"},
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(res_ren.status_code, 201)
            self.assertEqual(len(fake_db.state["membresia_socio"]), total_antes + 1)

            actual_801 = next(m for m in fake_db.state["membresia_socio"] if m["id_membresia_socio"] == 801)
            # La fila anterior se cancela lógicamente al activarse la nueva, conservando todas sus condiciones históricas intactas
            self.assertEqual(actual_801["estado"], "cancelada")
            for campo_inmutable in ("id_membresia_socio", "id_socio_gimnasio", "id_membresia_gimnasio", "precio", "fecha_inicio", "fecha_vencimiento"):
                self.assertEqual(actual_801[campo_inmutable], snapshot_801[campo_inmutable])

            # Intentar mutar precio o fecha_inicio de una contratación histórica -> 403
            res_mut = self.client.patch(
                "/api/membresias/801",
                json={"precio": 5000, "estado": "activa"},
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(res_mut.status_code, 403)

            # Cancelación vía DELETE sobre la nueva contratación es lógica (rpc_actualizar_estado_membresia) y no elimina la fila
            nuevo_id = res_ren.json()["membresia_socio"]["id_membresia_socio"]
            res_cancel = self.client.delete(
                f"/api/membresias/{nuevo_id}",
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(res_cancel.status_code, 200)
            self.assertEqual(res_cancel.json()["membresia_socio"]["estado"], "cancelada")
            self.assertEqual(fake_db.rpc_calls[-1][0], "rpc_actualizar_estado_membresia")
            self.assertEqual(len(fake_db.state["membresia_socio"]), total_antes + 1)

    # 13. Impedir renovaciones concurrentes duplicadas (conflicto de rango -> 409)
    def test_13_impedir_renovaciones_concurrentes_duplicadas_409(self):
        token_recep = self._make_token(self.uid_recep_1, "recep1@gymtrack.test")
        p1, p2, _, _ = self._patch_env(simulate_concurrent_overlap=True)
        with p1, p2:
            res = self.client.post(
                "/api/membresias/renovar",
                json={"id_socio": 70, "id_membresia_gimnasio": 501, "estado": "activa"},
                headers={"Authorization": f"Bearer {token_recep}"},
            )
            self.assertEqual(res.status_code, 409)

    # 14. Impedir que un socio consulte contrataciones ajenas
    def test_14_impedir_socio_consultar_contrataciones_ajenas_403(self):
        token_socio70 = self._make_token(self.uid_socio_70, "socio70@gymtrack.test")
        p1, p2, _, _ = self._patch_env()
        with p1, p2:
            # Consultar listado filtrando por otro id_socio -> 403
            self.assertEqual(
                self.client.get(
                    "/api/membresias?id_socio=71",
                    headers={"Authorization": f"Bearer {token_socio70}"},
                ).status_code,
                403,
            )
            # Consultar detalle de contratación 802 (Socio 71) -> 403
            self.assertEqual(
                self.client.get(
                    "/api/membresias/802",
                    headers={"Authorization": f"Bearer {token_socio70}"},
                ).status_code,
                403,
            )
            # Intentar contratar en nombre de otro socio -> 403
            self.assertEqual(
                self.client.post(
                    "/api/membresias",
                    json={"id_socio": 71, "id_membresia_gimnasio": 501, "estado": "pendiente"},
                    headers={"Authorization": f"Bearer {token_socio70}"},
                ).status_code,
                403,
            )

    # =========================================================================
    # PAGOS (Tests 15 - 20)
    # =========================================================================

    # 15. Registrar un pago correctamente vía rpc_registrar_pago (activando contrato pendiente)
    def test_15_registrar_pago_correctamente_via_rpc(self):
        token_dueno2 = self._make_token(self.uid_dueno_2, "dueno2@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            # Contrato 803 (Gym 20) está 'pendiente' con precio 40000 y ningún pago aprobado aún
            # Si no se envía monto, el backend calcula automáticamente el saldo pendiente real (40000)
            res = self.client.post(
                "/api/pagos",
                json={
                    "id_membresia_socio": 803,
                    "metodo_pago": "mercadopago",
                    "estado": "aprobado",
                    "comprobante": "MP-20-803",
                },
                headers={"Authorization": f"Bearer {token_dueno2}"},
            )
            self.assertEqual(res.status_code, 201)
            pago = res.json()["pago"]
            self.assertEqual(pago["monto"], 40000.0)
            self.assertEqual(pago["estado"], "aprobado")
            self.assertEqual(fake_db.rpc_calls[-1][0], "rpc_registrar_pago")
            # El contrato 803 debe haberse activado automáticamente al cubrir el importe
            contrato_803 = next(m for m in fake_db.state["membresia_socio"] if m["id_membresia_socio"] == 803)
            self.assertEqual(contrato_803["estado"], "activa")

    # 16. Rechazar importes inválidos (negativo, sobrepago, monto 0 aprobado, comprobante duplicado)
    def test_16_rechazar_importes_invalidos_y_comprobante_duplicado(self):
        token_dueno2 = self._make_token(self.uid_dueno_2, "dueno2@gymtrack.test")
        p1, p2, _, _ = self._patch_env()
        with p1, p2:
            # Monto negativo -> 422 (validación Pydantic ge=0)
            self.assertEqual(
                self.client.post(
                    "/api/pagos",
                    json={"id_membresia_socio": 803, "monto": -100, "metodo_pago": "efectivo"},
                    headers={"Authorization": f"Bearer {token_dueno2}"},
                ).status_code,
                422,
            )
            # Sobrepago (precio contrato 803 es 40000, se envía 45000) -> 400
            self.assertEqual(
                self.client.post(
                    "/api/pagos",
                    json={"id_membresia_socio": 803, "monto": 45000, "metodo_pago": "efectivo"},
                    headers={"Authorization": f"Bearer {token_dueno2}"},
                ).status_code,
                400,
            )
            # Monto 0 aprobado sobre plan pago -> 400
            self.assertEqual(
                self.client.post(
                    "/api/pagos",
                    json={"id_membresia_socio": 803, "monto": 0, "estado": "aprobado", "metodo_pago": "efectivo"},
                    headers={"Authorization": f"Bearer {token_dueno2}"},
                ).status_code,
                400,
            )
            # Comprobante duplicado en el mismo gimnasio (REC-20-001 ya existe en pago 902 no anulado) -> 409
            self.assertEqual(
                self.client.post(
                    "/api/pagos",
                    json={
                        "id_membresia_socio": 803,
                        "monto": 20000,
                        "estado": "aprobado",
                        "metodo_pago": "transferencia",
                        "comprobante": "rec-20-001",
                    },
                    headers={"Authorization": f"Bearer {token_dueno2}"},
                ).status_code,
                409,
            )

    # 17. Rechazar pagos asociados a contrataciones ajenas / otro gimnasio
    def test_17_rechazar_pagos_contrataciones_ajenas_403(self):
        token_dueno1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            # Dueño 1 (Gym 10) intenta registrar pago sobre contrato 803 (Gym 20) -> 403
            res = self.client.post(
                "/api/pagos",
                json={"id_membresia_socio": 803, "monto": 10000, "metodo_pago": "efectivo"},
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(res.status_code, 403)
            # Dueño 1 intenta consultar pago 902 (Gym 20) -> 403
            self.assertEqual(
                self.client.get(
                    "/api/pagos/902",
                    headers={"Authorization": f"Bearer {token_dueno1}"},
                ).status_code,
                403,
            )
            self.assertEqual(len(fake_db.rpc_calls), 0)

    # 18. Impedir modificar pagos confirmados/aprobados arbitrariamente y prohibir borrado físico
    def test_18_impedir_modificar_pagos_aprobados_y_borrado_fisico(self):
        token_dueno1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        p1, p2, fake_db, _ = self._patch_env()
        with p1, p2:
            # Pago 901 está 'aprobado': no puede volver a 'pendiente' ni pasar a 'rechazado' -> 409
            self.assertEqual(
                self.client.patch(
                    "/api/pagos/901/estado",
                    json={"estado": "pendiente"},
                    headers={"Authorization": f"Bearer {token_dueno1}"},
                ).status_code,
                409,
            )
            self.assertEqual(
                self.client.patch(
                    "/api/pagos/901/estado",
                    json={"estado": "rechazado"},
                    headers={"Authorization": f"Bearer {token_dueno1}"},
                ).status_code,
                409,
            )
            # No se permite alterar monto/comprobante de un pago existente -> 403
            self.assertEqual(
                self.client.patch(
                    "/api/pagos/901/estado",
                    json={"estado": "anulado", "monto": 1},
                    headers={"Authorization": f"Bearer {token_dueno1}"},
                ).status_code,
                403,
            )

            # DELETE realiza anulación lógica vía rpc_actualizar_estado_pago sin borrar físicamente el registro
            total_pagos_antes = len(fake_db.state["pago"])
            res_del = self.client.delete(
                "/api/pagos/901",
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(res_del.status_code, 200)
            self.assertEqual(res_del.json()["pago"]["estado"], "anulado")
            self.assertEqual(fake_db.rpc_calls[-1][0], "rpc_actualizar_estado_pago")
            self.assertEqual(len(fake_db.state["pago"]), total_pagos_antes)

            # Un pago anulado es inmutable -> intentar reactivarlo como 'aprobado' devuelve 409
            self.assertEqual(
                self.client.patch(
                    "/api/pagos/901/estado",
                    json={"estado": "aprobado"},
                    headers={"Authorization": f"Bearer {token_dueno1}"},
                ).status_code,
                409,
            )

    # 19. Impedir acceso a operaciones financieras por roles sin autorización (entrenador y socio)
    def test_19_impedir_acceso_financiero_roles_sin_autorizacion_403(self):
        token_trainer = self._make_token(self.uid_trainer_1, "trainer1@gymtrack.test")
        token_socio70 = self._make_token(self.uid_socio_70, "socio70@gymtrack.test")
        p1, p2, _, _ = self._patch_env()
        with p1, p2:
            # Entrenador NO tiene acceso a ningún endpoint financiero de /api/pagos -> 403
            self.assertEqual(
                self.client.get("/api/pagos", headers={"Authorization": f"Bearer {token_trainer}"}).status_code,
                403,
            )
            self.assertEqual(
                self.client.get("/api/pagos/resumen", headers={"Authorization": f"Bearer {token_trainer}"}).status_code,
                403,
            )
            self.assertEqual(
                self.client.post(
                    "/api/pagos",
                    json={"id_membresia_socio": 801, "monto": 1000, "metodo_pago": "efectivo"},
                    headers={"Authorization": f"Bearer {token_trainer}"},
                ).status_code,
                403,
            )

            # Socio SÍ puede ver sus propios pagos (901), pero NO pagos ajenos (902) ni registrar/modificar/anular pagos
            res_propios = self.client.get(
                "/api/pagos/mis-pagos",
                headers={"Authorization": f"Bearer {token_socio70}"},
            )
            self.assertEqual(res_propios.status_code, 200)
            self.assertEqual([p["id_pago"] for p in res_propios.json()["datos"]], [901])

            self.assertEqual(
                self.client.get("/api/pagos/902", headers={"Authorization": f"Bearer {token_socio70}"}).status_code,
                403,
            )
            self.assertEqual(
                self.client.post(
                    "/api/pagos",
                    json={"id_membresia_socio": 801, "monto": 1000, "metodo_pago": "efectivo"},
                    headers={"Authorization": f"Bearer {token_socio70}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.patch(
                    "/api/pagos/901/estado",
                    json={"estado": "anulado"},
                    headers={"Authorization": f"Bearer {token_socio70}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.delete(
                    "/api/pagos/901",
                    headers={"Authorization": f"Bearer {token_socio70}"},
                ).status_code,
                403,
            )

    # 20. Verificar que todas las operaciones B2.3 utilizan el cliente RLS del usuario autenticado
    def test_20_todas_las_operaciones_usan_cliente_rls_del_usuario(self):
        token_dueno1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        token_dueno2 = self._make_token(self.uid_dueno_2, "dueno2@gymtrack.test")
        p1, p2, _, token_calls = self._patch_env()
        with p1, p2:
            # Plan (Dueño 1)
            r1 = self.client.get("/api/planes-membresia", headers={"Authorization": f"Bearer {token_dueno1}"})
            self.assertEqual(r1.status_code, 200)
            # Membresía (Dueño 1)
            r2 = self.client.get("/api/membresias", headers={"Authorization": f"Bearer {token_dueno1}"})
            self.assertEqual(r2.status_code, 200)
            # Pago (Dueño 2)
            r3 = self.client.get("/api/pagos/resumen", headers={"Authorization": f"Bearer {token_dueno2}"})
            self.assertEqual(r3.status_code, 200)

            # Verificar que create_supabase_client_for_token fue invocado con el Bearer token de cada usuario
            self.assertEqual(token_calls, [token_dueno1, token_dueno1, token_dueno2])


if __name__ == "__main__":
    unittest.main()
