import copy
import unittest
from datetime import datetime, timedelta, timezone
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


class FakeSociosQueryBuilder:
    def __init__(self, table_name: str, state: Dict[str, List[Dict[str, Any]]]):
        self.table_name = table_name
        self.state = state
        self.filters_eq: Dict[str, Any] = {}
        self.filters_in: Dict[str, List[Any]] = {}
        self.is_single = False
        self._update_payload: Optional[Dict[str, Any]] = None
        self._insert_payload: Optional[List[Dict[str, Any]]] = None

    def select(self, *_args, **_kwargs) -> "FakeSociosQueryBuilder":
        return self

    def eq(self, col: str, val: Any) -> "FakeSociosQueryBuilder":
        self.filters_eq[col] = val
        return self

    def in_(self, col: str, vals: List[Any]) -> "FakeSociosQueryBuilder":
        self.filters_in[col] = list(vals)
        return self

    def order(self, *_args, **_kwargs) -> "FakeSociosQueryBuilder":
        return self

    def maybe_single(self) -> "FakeSociosQueryBuilder":
        self.is_single = True
        return self

    def single(self) -> "FakeSociosQueryBuilder":
        self.is_single = True
        return self

    def update(self, payload: Dict[str, Any]) -> "FakeSociosQueryBuilder":
        self._update_payload = payload
        return self

    def insert(self, rows: List[Dict[str, Any]]) -> "FakeSociosQueryBuilder":
        self._insert_payload = rows
        return self

    def _enrich_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        enriched = copy.deepcopy(row)
        gyms_by_id = {int(g["id_gimnasio"]): g for g in self.state.get("gimnasio", [])}
        sucs_by_id = {int(s["id_sucursal"]): s for s in self.state.get("sucursal", [])}
        socios_by_id = {int(s["id_socio"]): s for s in self.state.get("socio", [])}

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
                    sg_copy["membresia_socio"] = []
                    sgs.append(sg_copy)
            enriched["socio_gimnasio"] = sgs

        elif self.table_name == "socio_gimnasio":
            sid = int(enriched.get("id_socio") or 0)
            gid = int(enriched.get("id_gimnasio") or 0)
            if sid in socios_by_id:
                enriched["socio"] = copy.deepcopy(socios_by_id[sid])
            if gid in gyms_by_id:
                enriched["gimnasio"] = copy.deepcopy(gyms_by_id[gid])

        return enriched

    def execute(self) -> FakeSupabaseResponse:
        rows = self.state.setdefault(self.table_name, [])

        if self._insert_payload is not None:
            inserted = []
            for item in self._insert_payload:
                new_row = copy.deepcopy(item)
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
            updated_out = []
            for r in matched_refs:
                r.update(copy.deepcopy(self._update_payload))
                updated_out.append(self._enrich_row(r))
            return FakeSupabaseResponse(updated_out, count=len(updated_out))

        enriched_out = [self._enrich_row(r) for r in matched_refs]
        if self.is_single:
            return FakeSupabaseResponse(enriched_out[0] if enriched_out else None)
        return FakeSupabaseResponse(enriched_out, count=len(enriched_out))


class FakeSociosRpcBuilder:
    def __init__(self, fn_name: str, params: Dict[str, Any], client: "FakeSociosSupabaseClient"):
        self.fn_name = fn_name
        self.params = params
        self.client = client

    def execute(self) -> FakeSupabaseResponse:
        self.client.rpc_calls.append((self.fn_name, copy.deepcopy(self.params)))
        if self.fn_name == "rpc_registrar_socio_gimnasio":
            return FakeSupabaseResponse(self.client._rpc_registrar_socio_gimnasio(self.params))
        if self.fn_name == "rpc_asociar_socio_gimnasio":
            return FakeSupabaseResponse(self.client._rpc_asociar_socio_gimnasio(self.params))
        if self.fn_name == "rpc_cambiar_estado_socio_gimnasio":
            return FakeSupabaseResponse(self.client._rpc_cambiar_estado_socio_gimnasio(self.params))
        if self.fn_name == "rpc_actualizar_estado_membresia":
            return FakeSupabaseResponse(self.client._rpc_actualizar_estado_membresia(self.params))
        raise RuntimeError(f"RPC no soportada en mock: {self.fn_name}")


class FakeSociosSupabaseClient:
    def __init__(self, state: Dict[str, List[Dict[str, Any]]]):
        self.state = state
        self.rpc_calls: List[tuple[str, Dict[str, Any]]] = []

    def from_(self, table_name: str) -> FakeSociosQueryBuilder:
        return FakeSociosQueryBuilder(table_name, self.state)

    def rpc(self, fn_name: str, params: Optional[Dict[str, Any]] = None) -> FakeSociosRpcBuilder:
        return FakeSociosRpcBuilder(fn_name, params or {}, self)

    def _rpc_asociar_socio_gimnasio(self, params: Dict[str, Any]) -> Dict[str, Any]:
        gid = int(params["p_id_gimnasio"])
        sid = int(params["p_id_socio"])

        gym = next((g for g in self.state.get("gimnasio", []) if int(g["id_gimnasio"]) == gid), None)
        if not gym or not gym.get("estado"):
            raise ValueError(f"El gimnasio {gid} no existe o se encuentra inactivo.")

        socio = next((s for s in self.state.get("socio", []) if int(s["id_socio"]) == sid), None)
        if not socio or not socio.get("estado"):
            raise ValueError(f"El socio {sid} no existe o se encuentra inactivo.")

        sgs = self.state.setdefault("socio_gimnasio", [])
        existing = next(
            (r for r in sgs if int(r["id_socio"]) == sid and int(r["id_gimnasio"]) == gid),
            None,
        )
        if existing is not None:
            existing["estado"] = True
            return copy.deepcopy(existing)

        new_id = max([int(r["id_socio_gimnasio"]) for r in sgs], default=700) + 1
        row = {
            "id_socio_gimnasio": new_id,
            "id_socio": sid,
            "id_gimnasio": gid,
            "fecha_alta": "2026-11-23",
            "estado": True,
            "creado_en": "2026-11-23T12:00:00Z",
        }
        sgs.append(row)
        return copy.deepcopy(row)

    def _rpc_registrar_socio_gimnasio(self, params: Dict[str, Any]) -> Dict[str, Any]:
        gid = int(params["p_id_gimnasio"])
        dni = str(params["p_dni"]).strip() if params.get("p_dni") else None
        email = str(params["p_email"]).strip().lower() if params.get("p_email") else None

        if not dni and not email:
            raise ValueError("Debes proporcionar al menos DNI o email para identificar o registrar al socio.")

        socios = self.state.setdefault("socio", [])
        by_dni = next((s for s in socios if dni and str(s.get("dni") or "").strip() == dni), None)
        by_email = next(
            (s for s in socios if email and str(s.get("email") or "").strip().lower() == email),
            None,
        )

        if by_dni and by_email and int(by_dni["id_socio"]) != int(by_email["id_socio"]):
            raise ValueError("Conflicto de identidad: el DNI y el email pertenecen a socios diferentes.")

        socio = by_dni or by_email
        creado = False
        if socio is None:
            if not params.get("p_nombre") or not params.get("p_apellido"):
                raise ValueError("Nombre y apellido son obligatorios para registrar un nuevo socio.")
            new_id = max([int(s["id_socio"]) for s in socios], default=70) + 1
            socio = {
                "id_socio": new_id,
                "auth_user_id": None,
                "dni": dni,
                "telefono": params.get("p_telefono"),
                "fecha_alta": "2026-11-23",
                "estado": True,
                "nombre": params["p_nombre"],
                "apellido": params["p_apellido"],
                "email": email,
            }
            socios.append(socio)
            creado = True

        assoc = self._rpc_asociar_socio_gimnasio(
            {"p_id_gimnasio": gid, "p_id_socio": int(socio["id_socio"])}
        )
        return {
            "creado": creado,
            "socio": copy.deepcopy(socio),
            "socio_gimnasio": assoc,
            "membresia_socio": None,
            "pago": None,
        }

    def _rpc_cambiar_estado_socio_gimnasio(self, params: Dict[str, Any]) -> Dict[str, Any]:
        sg_id = int(params["p_id_socio_gimnasio"])
        nuevo_estado = bool(params["p_estado"])

        sgs = self.state.get("socio_gimnasio", [])
        row = next((r for r in sgs if int(r["id_socio_gimnasio"]) == sg_id), None)
        if not row:
            raise ValueError(f"La asociación socio_gimnasio #{sg_id} no existe.")
        row["estado"] = nuevo_estado
        return copy.deepcopy(row)

    def _rpc_actualizar_estado_membresia(self, params: Dict[str, Any]) -> Dict[str, Any]:
        mid = int(params["p_id_membresia_socio"])
        nuevo_estado = params.get("p_estado")
        nueva_fecha_venc = params.get("p_fecha_vencimiento")

        membs = self.state.get("membresia_socio", [])
        row = next((m for m in membs if int(m["id_membresia_socio"]) == mid), None)
        if not row:
            raise ValueError(f"La contratación #{mid} no existe.")
        if nuevo_estado is not None:
            row["estado"] = str(nuevo_estado).strip().lower()
        if nueva_fecha_venc is not None:
            row["fecha_vencimiento"] = nueva_fecha_venc
        return copy.deepcopy(row)


class TestSociosB22(unittest.TestCase):
    """
    Suite de pruebas unitarias de la Fase B2.2: Adaptación de Socios y Asociaciones Multi-Gimnasio.
    Cubre los 14 escenarios obligatorios contra el contrato de `gymtrack_instalacion_definitiva.sql`.
    """

    @classmethod
    def setUpClass(cls):
        cls.ec_private_key = ec.generate_private_key(ec.SECP256R1())
        cls.ec_public_key = cls.ec_private_key.public_key()
        cls.issuer = "https://qdlkljSb.supabase.co/auth/v1"
        cls.audience = "authenticated"

        cls.uid_dueno_1 = "11111111-1111-4111-8111-000000000001"
        cls.uid_dueno_2 = "11111111-1111-4111-8111-000000000002"
        cls.uid_recep_1 = "11111111-1111-4111-8111-000000000003"
        cls.uid_trainer_1 = "11111111-1111-4111-8111-000000000004"
        cls.uid_socio_unico = "11111111-1111-4111-8111-000000000005"
        cls.uid_socio_multi = "11111111-1111-4111-8111-000000000006"
        cls.uid_socio_baja = "11111111-1111-4111-8111-000000000007"
        cls.uid_sin_perfil_socio = "11111111-1111-4111-8111-000000000008"

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
        return jwt.encode(payload, self.ec_private_key, algorithm="ES256", headers={"kid": "test-kid-b22"})

    def _build_initial_state(self) -> Dict[str, List[Dict[str, Any]]]:
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
            "administrador": [],
            "administrador_gimnasio": [],
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
                # Socio 70: una sola asociación (Gym 10)
                {
                    "id_socio": 70,
                    "auth_user_id": self.uid_socio_unico,
                    "dni": "40111002",
                    "telefono": "+54 9 11 6000-0002",
                    "fecha_alta": "2026-01-10",
                    "estado": True,
                    "nombre": "Lucía",
                    "apellido": "Pereyra",
                    "email": "socio_norte@gymtrack.test",
                },
                # Socio 71: múltiples gimnasios (Gym 10 y Gym 20)
                {
                    "id_socio": 71,
                    "auth_user_id": self.uid_socio_multi,
                    "dni": "40111001",
                    "telefono": "+54 9 11 6000-0001",
                    "fecha_alta": "2026-01-05",
                    "estado": True,
                    "nombre": "Carlos",
                    "apellido": "MultiGym",
                    "email": "socio_multi@gymtrack.test",
                },
                # Socio 72: identidad activa pero su única asociación (Gym 10) está inactiva (estado = False)
                {
                    "id_socio": 72,
                    "auth_user_id": self.uid_socio_baja,
                    "dni": "40111004",
                    "telefono": "+54 9 11 6000-0004",
                    "fecha_alta": "2026-01-01",
                    "estado": True,
                    "nombre": "Paula",
                    "apellido": "BajaGym",
                    "email": "socio_baja@gymtrack.test",
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
        }

    def _patch_env(self, state: Optional[Dict[str, List[Dict[str, Any]]]] = None):
        fake_db = FakeSociosSupabaseClient(state if state is not None else self._build_initial_state())

        def _fake_verify(token: str):
            return verify_supabase_access_token(
                token,
                verification_key=self.ec_public_key,
                expected_issuer=self.issuer,
                expected_audience=self.audience,
            )

        p1 = patch("app.core.dependencies.verify_supabase_access_token", side_effect=_fake_verify)
        p2 = patch("app.core.dependencies.create_supabase_client_for_token", return_value=fake_db)
        return p1, p2, fake_db

    # 1. Socio con una sola asociación
    def test_01_socio_con_una_sola_asociacion(self):
        token = self._make_token(self.uid_socio_unico, "socio_norte@gymtrack.test")
        p1, p2, _ = self._patch_env()
        with p1, p2:
            res = self.client.get("/api/socios/perfil", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertEqual(body["socio"]["id_socio"], 70)
            self.assertNotIn("id_gimnasio", body["socio"])
            self.assertNotIn("contrasena", body["socio"])
            self.assertEqual(body["total_gimnasios_activos"], 1)
            self.assertEqual(body["gimnasios"][0]["id_gimnasio"], 10)

            res_upd = self.client.put(
                "/api/socios/perfil",
                json={"telefono": "+54 9 11 9999-0000"},
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(res_upd.status_code, 200)
            self.assertEqual(res_upd.json()["socio"]["telefono"], "+54 9 11 9999-0000")

    # 2. Socio asociado a múltiples gimnasios
    def test_02_socio_asociado_a_multiples_gimnasios(self):
        token = self._make_token(self.uid_socio_multi, "socio_multi@gymtrack.test")
        p1, p2, _ = self._patch_env()
        with p1, p2:
            res = self.client.get("/api/socios/mis-gimnasios", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertEqual(body["total_activas"], 2)
            self.assertEqual(sorted([g["id_gimnasio"] for g in body["gimnasios_activos"]]), [10, 20])

            # Puede consultar su asociación activa en Gym 10 y en Gym 20
            self.assertEqual(
                self.client.get(
                    "/api/socios/mis-gimnasios/10",
                    headers={"Authorization": f"Bearer {token}", "X-Gimnasio-Id": "10"},
                ).status_code,
                200,
            )
            self.assertEqual(
                self.client.get(
                    "/api/socios/mis-gimnasios/20",
                    headers={"Authorization": f"Bearer {token}", "X-Gimnasio-Id": "20"},
                ).status_code,
                200,
            )

    # 3. Gimnasio que registra un socio nuevo mediante rpc_registrar_socio_gimnasio
    def test_03_gimnasio_registra_socio_nuevo_via_rpc(self):
        token_recep = self._make_token(self.uid_recep_1, "recep1@gymtrack.test")
        p1, p2, fake_db = self._patch_env()
        with p1, p2:
            res = self.client.post(
                "/api/socios",
                json={
                    "nombre": "Nuevo",
                    "apellido": "Socio",
                    "dni": "45000111",
                    "email": "nuevo_socio@gymtrack.test",
                    "telefono": "1144445555",
                },
                headers={"Authorization": f"Bearer {token_recep}"},
            )
            self.assertEqual(res.status_code, 201)
            body = res.json()
            self.assertTrue(body["creado"])
            self.assertEqual(body["id_gimnasio"], 10)
            self.assertNotIn("id_gimnasio", body["socio"])
            self.assertNotIn("contrasena", body["socio"])
            self.assertEqual(body["socio_gimnasio"]["id_gimnasio"], 10)
            self.assertTrue(body["socio_gimnasio"]["estado"])
            self.assertEqual(fake_db.rpc_calls[-1][0], "rpc_registrar_socio_gimnasio")
            self.assertIsNone(fake_db.rpc_calls[-1][1]["p_id_membresia_gimnasio"])

    # 4 y 5. Gimnasio que incorpora un socio ya existente reutilizando su identidad sin duplicar registros
    def test_04_y_05_reutilizacion_identidad_socio_existente_sin_duplicar(self):
        token_dueno_2 = self._make_token(self.uid_dueno_2, "dueno2@gymtrack.test")
        p1, p2, fake_db = self._patch_env()
        with p1, p2:
            total_socios_antes = len(fake_db.state["socio"])

            # Dueño 2 (Gym 20) registra a Lucía (Socio 70, ya asociada en Gym 10) mediante su DNI
            res_reuse = self.client.post(
                "/api/socios",
                json={"dni": "40111002", "email": "socio_norte@gymtrack.test"},
                headers={"Authorization": f"Bearer {token_dueno_2}"},
            )
            self.assertEqual(res_reuse.status_code, 201)
            body = res_reuse.json()
            self.assertFalse(body["creado"])
            self.assertEqual(body["socio"]["id_socio"], 70)
            self.assertEqual(body["socio"]["auth_user_id"], self.uid_socio_unico)
            self.assertEqual(body["socio_gimnasio"]["id_gimnasio"], 20)
            self.assertEqual(fake_db.rpc_calls[-1][0], "rpc_registrar_socio_gimnasio")
            # No se creó ninguna fila duplicada en public.socio
            self.assertEqual(len(fake_db.state["socio"]), total_socios_antes)

            # También se puede asociar explícitamente un socio existente mediante rpc_asociar_socio_gimnasio
            # (Ej.: Dueño 2 asocia a Paula, Socio 72, en Gym 20)
            res_assoc = self.client.post(
                "/api/socios/asociar",
                json={"id_socio": 72},
                headers={"Authorization": f"Bearer {token_dueno_2}"},
            )
            self.assertEqual(res_assoc.status_code, 201)
            self.assertEqual(fake_db.rpc_calls[-1][0], "rpc_asociar_socio_gimnasio")
            self.assertEqual(res_assoc.json()["socio_gimnasio"]["id_socio"], 72)
            self.assertEqual(res_assoc.json()["socio_gimnasio"]["id_gimnasio"], 20)

    # 6. Asociación duplicada activa -> 409 Conflict
    def test_06_asociacion_duplicada_activa_409(self):
        token_dueno_1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        p1, p2, _ = self._patch_env()
        with p1, p2:
            # Socio 70 ya tiene asociación activa (701) en Gym 10
            res_dup_reg = self.client.post(
                "/api/socios",
                json={"dni": "40111002", "email": "socio_norte@gymtrack.test"},
                headers={"Authorization": f"Bearer {token_dueno_1}"},
            )
            self.assertEqual(res_dup_reg.status_code, 409)

            res_dup_assoc = self.client.post(
                "/api/socios/asociar",
                json={"id_socio": 70},
                headers={"Authorization": f"Bearer {token_dueno_1}"},
            )
            self.assertEqual(res_dup_assoc.status_code, 409)

    # 7 y 8. Desactivación de una asociación vía RPC y conservación de la identidad del socio
    def test_07_y_08_desactivar_asociacion_conserva_identidad_y_otras_asociaciones(self):
        token_dueno_1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        token_socio_multi = self._make_token(self.uid_socio_multi, "socio_multi@gymtrack.test")
        p1, p2, fake_db = self._patch_env()
        with p1, p2:
            # Dueño 1 desactiva la asociación 702 (Socio 71 en Gym 10) mediante DELETE -> rpc_cambiar_estado_socio_gimnasio
            res_desact = self.client.delete(
                "/api/socios/asociaciones/702",
                headers={"Authorization": f"Bearer {token_dueno_1}"},
            )
            self.assertEqual(res_desact.status_code, 200)
            self.assertFalse(res_desact.json()["socio_gimnasio"]["estado"])
            self.assertEqual(fake_db.rpc_calls[-1][0], "rpc_cambiar_estado_socio_gimnasio")
            self.assertEqual(fake_db.rpc_calls[-1][1], {"p_id_socio_gimnasio": 702, "p_estado": False})

            # La identidad del Socio 71 sigue intacta en public.socio y sigue activo en Gym 20
            socio_71 = next((s for s in fake_db.state["socio"] if s["id_socio"] == 71), None)
            self.assertIsNotNone(socio_71)
            self.assertTrue(socio_71["estado"])

            res_perfil = self.client.get(
                "/api/socios/perfil",
                headers={"Authorization": f"Bearer {token_socio_multi}"},
            )
            self.assertEqual(res_perfil.status_code, 200)
            self.assertEqual(res_perfil.json()["socio"]["id_socio"], 71)
            self.assertEqual([g["id_gimnasio"] for g in res_perfil.json()["gimnasios"]], [20])

    # 9. Socio que intenta consultar o modificar datos de otro socio -> 403
    def test_09_socio_intenta_consultar_o_modificar_otro_socio_403(self):
        token_socio_70 = self._make_token(self.uid_socio_unico, "socio_norte@gymtrack.test")
        p1, p2, _ = self._patch_env()
        with p1, p2:
            self.assertEqual(
                self.client.get(
                    "/api/socios/perfil?id_socio=71",
                    headers={"Authorization": f"Bearer {token_socio_70}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.get(
                    "/api/socios/71",
                    headers={"Authorization": f"Bearer {token_socio_70}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.get(
                    "/api/socios/asociaciones/702",
                    headers={"Authorization": f"Bearer {token_socio_70}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.put(
                    "/api/socios/perfil",
                    json={"id_socio": 71, "nombre": "Hack"},
                    headers={"Authorization": f"Bearer {token_socio_70}"},
                ).status_code,
                403,
            )

    # 10. Gimnasio que intenta consultar o modificar socios de otra organización -> 403
    def test_10_gimnasio_intenta_consultar_o_modificar_socios_ajenos_403(self):
        token_dueno_2 = self._make_token(self.uid_dueno_2, "dueno2@gymtrack.test")
        p1, p2, _ = self._patch_env()
        with p1, p2:
            # Socio 70 solo está asociado en Gym 10; Dueño 2 (solo Gym 20) no puede consultarlo ni editarlo
            self.assertEqual(
                self.client.get("/api/socios?id_gimnasio=10", headers={"Authorization": f"Bearer {token_dueno_2}"}).status_code,
                403,
            )
            self.assertEqual(
                self.client.get("/api/socios/70", headers={"Authorization": f"Bearer {token_dueno_2}"}).status_code,
                403,
            )
            self.assertEqual(
                self.client.put(
                    "/api/socios/70",
                    json={"nombre": "Hack"},
                    headers={"Authorization": f"Bearer {token_dueno_2}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.patch(
                    "/api/socios/asociaciones/701/estado",
                    json={"estado": False},
                    headers={"Authorization": f"Bearer {token_dueno_2}"},
                ).status_code,
                403,
            )

    # 11. Empleado sin permisos suficientes (Entrenador no puede registrar/asociar/modificar/desactivar socios; Recepcionista no puede cambiar socio.estado global)
    def test_11_empleado_sin_permisos_suficientes_403(self):
        token_trainer = self._make_token(self.uid_trainer_1, "trainer1@gymtrack.test")
        token_recep = self._make_token(self.uid_recep_1, "recep1@gymtrack.test")
        p1, p2, _ = self._patch_env()
        with p1, p2:
            # Entrenador SÍ puede listar/ver socios de su gimnasio (fn_staff_puede_ver_socio)
            self.assertEqual(
                self.client.get("/api/socios", headers={"Authorization": f"Bearer {token_trainer}"}).status_code,
                200,
            )

            # Entrenador NO puede registrar, asociar, actualizar ni desactivar asociaciones de socios -> 403
            self.assertEqual(
                self.client.post(
                    "/api/socios",
                    json={"nombre": "A", "apellido": "B", "dni": "99887766"},
                    headers={"Authorization": f"Bearer {token_trainer}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.post(
                    "/api/socios/asociar",
                    json={"id_socio": 72},
                    headers={"Authorization": f"Bearer {token_trainer}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.patch(
                    "/api/socios/asociaciones/701/estado",
                    json={"estado": False},
                    headers={"Authorization": f"Bearer {token_trainer}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.put(
                    "/api/socios/70",
                    json={"telefono": "11223344"},
                    headers={"Authorization": f"Bearer {token_trainer}"},
                ).status_code,
                403,
            )

            # Recepcionista NO puede cambiar socio.estado global (solo dueño/admin org/admin sucursal) -> 403
            self.assertEqual(
                self.client.put(
                    "/api/socios/70",
                    json={"estado": False},
                    headers={"Authorization": f"Bearer {token_recep}"},
                ).status_code,
                403,
            )

    # 12. Asociación inactiva que intenta utilizarse -> 403
    def test_12_asociacion_inactiva_no_puede_utilizarse_403(self):
        token_baja = self._make_token(self.uid_socio_baja, "socio_baja@gymtrack.test")
        p1, p2, _ = self._patch_env()
        with p1, p2:
            # Paula (Socio 72) conserva su identidad y puede ver su perfil y el historial de asociaciones
            res_mis = self.client.get("/api/socios/mis-gimnasios", headers={"Authorization": f"Bearer {token_baja}"})
            self.assertEqual(res_mis.status_code, 200)
            self.assertEqual(res_mis.json()["total"], 1)
            self.assertEqual(res_mis.json()["total_activas"], 0)

            # Pero NO puede operar usando su asociación inactiva con Gym 10 -> 403
            self.assertEqual(
                self.client.get(
                    "/api/socios/mis-gimnasios/10",
                    headers={"Authorization": f"Bearer {token_baja}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.get(
                    "/api/socios/perfil",
                    headers={"Authorization": f"Bearer {token_baja}", "X-Gimnasio-Id": "10"},
                ).status_code,
                403,
            )

    # 13. Usuario autenticado sin perfil de socio -> 403
    def test_13_usuario_autenticado_sin_perfil_de_socio_403(self):
        # Caso A: Usuario Auth sin ningún perfil de negocio
        token_sin_perfil = self._make_token(self.uid_sin_perfil_socio, "sin_perfil@gymtrack.test")
        # Caso B: Dueño autenticado sin perfil de socio intentando llamar rutas exclusivas de socio
        token_dueno_1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")

        p1, p2, _ = self._patch_env()
        with p1, p2:
            self.assertEqual(
                self.client.get("/api/socios/perfil", headers={"Authorization": f"Bearer {token_sin_perfil}"}).status_code,
                403,
            )
            self.assertEqual(
                self.client.get("/api/socios/mis-gimnasios", headers={"Authorization": f"Bearer {token_dueno_1}"}).status_code,
                403,
            )
            self.assertEqual(
                self.client.put(
                    "/api/socios/perfil",
                    json={"nombre": "Intento"},
                    headers={"Authorization": f"Bearer {token_dueno_1}"},
                ).status_code,
                403,
            )

    # 14. Intentos de modificar identificadores y campos protegidos -> 403 / 400
    def test_14_intentos_de_modificar_identificadores_y_campos_protegidos(self):
        token_socio_70 = self._make_token(self.uid_socio_unico, "socio_norte@gymtrack.test")
        token_dueno_1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        p1, p2, _ = self._patch_env()
        with p1, p2:
            # Socio intentando alterar auth_user_id, fecha_alta, estado o id_gimnasio
            for payload in (
                {"auth_user_id": "22222222-2222-4222-8222-000000000099"},
                {"fecha_alta": "2020-01-01"},
                {"estado": False},
                {"id_gimnasio": 20},
                {"rol": "dueño"},
            ):
                res = self.client.put(
                    "/api/socios/perfil",
                    json=payload,
                    headers={"Authorization": f"Bearer {token_socio_70}"},
                )
                self.assertEqual(res.status_code, 403, f"Se esperaba 403 para payload {payload}")

            # Socio intentando guardar contraseña en tabla socio -> 400
            res_pass = self.client.put(
                "/api/socios/perfil",
                json={"contrasena": "secreta123"},
                headers={"Authorization": f"Bearer {token_socio_70}"},
            )
            self.assertEqual(res_pass.status_code, 400)

            # Staff intentando alterar auth_user_id o transferir una asociación socio_gimnasio -> 403
            self.assertEqual(
                self.client.put(
                    "/api/socios/70",
                    json={"auth_user_id": "22222222-2222-4222-8222-000000000099"},
                    headers={"Authorization": f"Bearer {token_dueno_1}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.patch(
                    "/api/socios/asociaciones/701/estado",
                    json={"estado": False, "id_gimnasio": 20},
                    headers={"Authorization": f"Bearer {token_dueno_1}"},
                ).status_code,
                403,
            )

    # 15. Baja de socio desactiva su estado en el gimnasio y cancela su membresía activa sin afectar otros gimnasios
    def test_15_baja_socio_desactiva_estado_y_membresia_activa(self):
        token_dueno_1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        state = self._build_initial_state()
        state["membresia_socio"] = [
            # Membresía activa de Socio 71 en Gym 10 (sg 702) -> debe cancelarse
            {
                "id_membresia_socio": 901,
                "id_socio_gimnasio": 702,
                "id_membresia_gimnasio": 501,
                "id_gimnasio": 10,
                "precio": 25000.0,
                "duracion_dias_contratados": 30,
                "limite_asistencias_semanales": None,
                "limite_asistencias_totales": None,
                "fecha_inicio": "2026-11-01",
                "fecha_vencimiento": "2026-12-01",
                "estado": "activa",
                "creado_en": "2026-11-01T10:00:00Z",
            },
            # Membresía activa del mismo Socio 71 en Gym 20 (sg 703) -> debe mantenerse activa
            {
                "id_membresia_socio": 902,
                "id_socio_gimnasio": 703,
                "id_membresia_gimnasio": 502,
                "id_gimnasio": 20,
                "precio": 30000.0,
                "duracion_dias_contratados": 30,
                "limite_asistencias_semanales": None,
                "limite_asistencias_totales": None,
                "fecha_inicio": "2026-11-01",
                "fecha_vencimiento": "2026-12-01",
                "estado": "activa",
                "creado_en": "2026-11-01T10:00:00Z",
            },
            # Membresía activa de Socio 70 en Gym 10 (sg 701) -> debe cancelarse vía PATCH /estado
            {
                "id_membresia_socio": 903,
                "id_socio_gimnasio": 701,
                "id_membresia_gimnasio": 501,
                "id_gimnasio": 10,
                "precio": 25000.0,
                "duracion_dias_contratados": 30,
                "limite_asistencias_semanales": None,
                "limite_asistencias_totales": None,
                "fecha_inicio": "2026-11-10",
                "fecha_vencimiento": "2026-12-10",
                "estado": "activa",
                "creado_en": "2026-11-10T10:00:00Z",
            },
        ]

        p1, p2, fake_db = self._patch_env(state=state)
        with p1, p2:
            # 1) Baja vía DELETE /api/socios/71
            res_baja = self.client.delete(
                "/api/socios/71?id_gimnasio=10",
                headers={"Authorization": f"Bearer {token_dueno_1}", "X-Gimnasio-Id": "10"},
            )
            self.assertEqual(res_baja.status_code, 200, res_baja.text)
            body_baja = res_baja.json()
            self.assertFalse(body_baja["socio_gimnasio"]["estado"])
            self.assertEqual(body_baja["membresias_desactivadas"], [901])

            m901 = next(m for m in fake_db.state["membresia_socio"] if m["id_membresia_socio"] == 901)
            m902 = next(m for m in fake_db.state["membresia_socio"] if m["id_membresia_socio"] == 902)
            self.assertEqual(m901["estado"], "cancelada")
            self.assertEqual(m902["estado"], "activa")

            # 2) El listado y detalle del gimnasio ahora muestran estado=False y membresía cancelada
            res_list = self.client.get(
                "/api/socios?estado=inactivo&id_gimnasio=10",
                headers={"Authorization": f"Bearer {token_dueno_1}", "X-Gimnasio-Id": "10"},
            )
            self.assertEqual(res_list.status_code, 200, res_list.text)
            socio_71_list = next(s for s in res_list.json()["socios"] if s["id_socio"] == 71)
            self.assertIs(socio_71_list["estado"], False)
            self.assertIs(socio_71_list["estado_asociacion"], False)
            self.assertIs(socio_71_list["estado_global"], True)
            self.assertIs(socio_71_list["tiene_contrasena"], True)
            self.assertEqual(socio_71_list["membresia_actual"]["estado"], "cancelada")

            res_det = self.client.get(
                "/api/socios/71?id_gimnasio=10",
                headers={"Authorization": f"Bearer {token_dueno_1}", "X-Gimnasio-Id": "10"},
            )
            self.assertEqual(res_det.status_code, 200, res_det.text)
            self.assertIs(res_det.json()["socio"]["estado"], False)
            self.assertIs(res_det.json()["socio"]["estado_asociacion"], False)
            self.assertIs(res_det.json()["socio"]["tiene_contrasena"], True)
            self.assertEqual(res_det.json()["membresia_actual"]["estado"], "cancelada")

            # 3) Desactivar vía PATCH /api/socios/70/estado también cancela la membresía activa 903
            res_patch = self.client.patch(
                "/api/socios/70/estado",
                json={"estado": False, "id_gimnasio": 10},
                headers={"Authorization": f"Bearer {token_dueno_1}", "X-Gimnasio-Id": "10"},
            )
            self.assertEqual(res_patch.status_code, 200, res_patch.text)
            self.assertFalse(res_patch.json()["socio_gimnasio"]["estado"])
            self.assertEqual(res_patch.json()["membresias_desactivadas"], [903])
            m903 = next(m for m in fake_db.state["membresia_socio"] if m["id_membresia_socio"] == 903)
            self.assertEqual(m903["estado"], "cancelada")


if __name__ == "__main__":
    unittest.main()
