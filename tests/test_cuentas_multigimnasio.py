"""
Suite de tests unitarios (con mocks controlados, sin llamadas a Supabase remoto)
para el sistema integral de cuentas globales, autenticación por rol, selector de gimnasio,
reutilización de identidades, contraseñas y seguridad multi-gimnasio (Sección 14).
"""

import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch, AsyncMock

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

import main as main_module
from app.services import account_provisioning


class FakeAuthAdmin:
    """Simulador en memoria de Supabase Auth Admin API + sign_in_with_password."""

    def __init__(self):
        self.users_by_email: Dict[str, Dict[str, Any]] = {}
        self.users_by_id: Dict[str, Dict[str, Any]] = {}
        self.create_calls: List[Dict[str, Any]] = []
        self.delete_calls: List[str] = []
        self.reset_calls: List[str] = []
        self.next_seq = 100

    def seed_user(self, uid: str, email: str, password: str, email_confirmed: bool = True):
        u = {
            "id": uid,
            "email": email.strip().lower(),
            "password": password,
            "email_confirmed_at": "2026-01-01T00:00:00Z" if email_confirmed else None,
        }
        self.users_by_email[u["email"]] = u
        self.users_by_id[uid] = u
        return u

    def list_users(self, page: int = 1, per_page: int = 200):
        return SimpleNamespace(
            users=[
                SimpleNamespace(id=u["id"], email=u["email"], email_confirmed_at=u["email_confirmed_at"])
                for u in self.users_by_id.values()
            ]
        )

    def create_user(self, payload: Dict[str, Any]):
        self.create_calls.append(dict(payload))
        email = str(payload["email"]).strip().lower()
        if email in self.users_by_email:
            raise Exception("User already registered")
        # Regla 3.3 / 11.1: JAMÁS email_confirm=True
        if payload.get("email_confirm") is True:
            raise AssertionError("VIOLACIÓN DE SEGURIDAD: email_confirm no puede ser True")
        self.next_seq += 1
        uid = f"99999999-9999-4999-8999-{self.next_seq:012d}"
        u = self.seed_user(uid, email, payload["password"], email_confirmed=False)
        return SimpleNamespace(user=SimpleNamespace(id=u["id"], email=u["email"], email_confirmed_at=None))

    def delete_user(self, uid: str):
        self.delete_calls.append(uid)
        u = self.users_by_id.pop(uid, None)
        if u:
            self.users_by_email.pop(u["email"], None)


class FakeGlobalDBQuery:
    def __init__(self, store: "FakeGlobalDB", table: str):
        self.store = store
        self.table = table
        self._eq: Dict[str, Any] = {}
        self._in: Dict[str, List[Any]] = {}
        self._update: Optional[Dict[str, Any]] = None
        self._single = False

    def select(self, _cols: str = "*"):
        return self

    def eq(self, col: str, val: Any):
        self._eq[col] = val
        return self

    def in_(self, col: str, vals: List[Any]):
        self._in[col] = list(vals)
        return self

    def order(self, *_args, **_kwargs):
        return self

    def maybe_single(self):
        self._single = True
        return self

    def update(self, payload: Dict[str, Any]):
        self._update = dict(payload)
        return self

    def _matches(self, row: Dict[str, Any]) -> bool:
        for k, v in self._eq.items():
            rv = row.get(k)
            if isinstance(rv, str) and isinstance(v, str):
                if rv.strip().lower() != v.strip().lower():
                    return False
            elif rv != v:
                return False
        for k, vals in self._in.items():
            if row.get(k) not in vals:
                return False
        return True

    def execute(self):
        rows = self.store.tables.get(self.table, [])
        if self._update is not None:
            if self.store.fail_update_on_table == self.table:
                raise RuntimeError("Fallo simulado al vincular auth_user_id")
            updated = []
            for r in rows:
                if self._matches(r):
                    r.update(self._update)
                    updated.append(copy.deepcopy(r))
            return SimpleNamespace(data=updated)

        matched = [self.store._hydrate(self.table, copy.deepcopy(r)) for r in rows if self._matches(r)]
        if self._single:
            return SimpleNamespace(data=matched[0] if matched else None)
        return SimpleNamespace(data=matched)


class FakeGlobalDB:
    """Base de datos en memoria con las RPCs oficiales y relación N:M multi-gimnasio."""

    def __init__(self, fake_auth: FakeAuthAdmin, is_admin_client: bool = False):
        self.tables: Dict[str, List[Dict[str, Any]]] = {
            "dueno": [],
            "gimnasio": [],
            "sucursal": [],
            "administrador": [],
            "administrador_gimnasio": [],
            "empleado": [],
            "empleado_sucursal": [],
            "socio": [],
            "socio_gimnasio": [],
            "membresia_socio": [],
        }
        self.fail_update_on_table: Optional[str] = None
        self.seq = 1000
        self._fake_auth = fake_auth
        self.auth = SimpleNamespace(admin=fake_auth) if is_admin_client else SimpleNamespace()

    def clone_as_admin(self) -> "FakeGlobalDB":
        adm = FakeGlobalDB(self._fake_auth, is_admin_client=True)
        adm.tables = self.tables
        return adm

    def _next_id(self) -> int:
        self.seq += 1
        return self.seq

    def _hydrate(self, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
        if table == "administrador":
            ags = []
            for ag in self.tables["administrador_gimnasio"]:
                if ag["id_administrador"] == row["id_administrador"]:
                    ag_c = copy.deepcopy(ag)
                    ag_c["gimnasio"] = copy.deepcopy(
                        next((g for g in self.tables["gimnasio"] if g["id_gimnasio"] == ag["id_gimnasio"]), None)
                    )
                    ags.append(ag_c)
            row["administrador_gimnasio"] = ags
        elif table == "administrador_gimnasio":
            row["administrador"] = copy.deepcopy(
                next((a for a in self.tables["administrador"] if a["id_administrador"] == row["id_administrador"]), None)
            )
            row["gimnasio"] = copy.deepcopy(
                next((g for g in self.tables["gimnasio"] if g["id_gimnasio"] == row["id_gimnasio"]), None)
            )
        elif table == "empleado":
            ess = []
            for es in self.tables["empleado_sucursal"]:
                if es["id_empleado"] == row["id_empleado"]:
                    es_c = copy.deepcopy(es)
                    suc = next((s for s in self.tables["sucursal"] if s["id_sucursal"] == es["id_sucursal"]), None)
                    if suc:
                        suc_c = copy.deepcopy(suc)
                        suc_c["gimnasio"] = copy.deepcopy(
                            next((g for g in self.tables["gimnasio"] if g["id_gimnasio"] == suc["id_gimnasio"]), None)
                        )
                        es_c["sucursal"] = suc_c
                    ess.append(es_c)
            row["empleado_sucursal"] = ess
        elif table == "empleado_sucursal":
            row["empleado"] = copy.deepcopy(
                next((e for e in self.tables["empleado"] if e["id_empleado"] == row["id_empleado"]), None)
            )
            row["sucursal"] = copy.deepcopy(
                next((s for s in self.tables["sucursal"] if s["id_sucursal"] == row["id_sucursal"]), None)
            )
        elif table == "socio":
            sgs = []
            for sg in self.tables["socio_gimnasio"]:
                if sg["id_socio"] == row["id_socio"]:
                    sg_c = copy.deepcopy(sg)
                    sg_c["gimnasio"] = copy.deepcopy(
                        next((g for g in self.tables["gimnasio"] if g["id_gimnasio"] == sg["id_gimnasio"]), None)
                    )
                    sg_c["membresia_socio"] = []
                    sgs.append(sg_c)
            row["socio_gimnasio"] = sgs
        elif table == "socio_gimnasio":
            row["socio"] = copy.deepcopy(
                next((s for s in self.tables["socio"] if s["id_socio"] == row["id_socio"]), None)
            )
            row["gimnasio"] = copy.deepcopy(
                next((g for g in self.tables["gimnasio"] if g["id_gimnasio"] == row["id_gimnasio"]), None)
            )
        return row

    def from_(self, table: str) -> FakeGlobalDBQuery:
        return FakeGlobalDBQuery(self, table)

    def rpc(self, fn_name: str, params: Dict[str, Any]):
        if fn_name == "rpc_registrar_socio_gimnasio":
            gid = int(params["p_id_gimnasio"])
            dni = (params.get("p_dni") or "").strip() or None
            email = (params.get("p_email") or "").strip().lower() or None
            existing = next(
                (
                    s for s in self.tables["socio"]
                    if (dni and s.get("dni") == dni) or (email and (s.get("email") or "").lower() == email)
                ),
                None,
            )
            creado = False
            if existing is None:
                existing = {
                    "id_socio": self._next_id(),
                    "auth_user_id": None,
                    "nombre": params.get("p_nombre"),
                    "apellido": params.get("p_apellido"),
                    "dni": dni,
                    "email": email,
                    "telefono": params.get("p_telefono"),
                    "fecha_alta": "2026-01-01",
                    "estado": True,
                }
                self.tables["socio"].append(existing)
                creado = True
            assoc = next(
                (
                    sg for sg in self.tables["socio_gimnasio"]
                    if sg["id_socio"] == existing["id_socio"] and sg["id_gimnasio"] == gid
                ),
                None,
            )
            if assoc is None:
                assoc = {
                    "id_socio_gimnasio": self._next_id(),
                    "id_socio": existing["id_socio"],
                    "id_gimnasio": gid,
                    "fecha_alta": "2026-01-01",
                    "estado": True,
                }
                self.tables["socio_gimnasio"].append(assoc)
            else:
                assoc["estado"] = True
            return SimpleNamespace(
                execute=lambda: SimpleNamespace(
                    data={
                        "creado": creado,
                        "socio": copy.deepcopy(existing),
                        "socio_gimnasio": copy.deepcopy(assoc),
                        "membresia_socio": None,
                        "pago": None,
                    }
                )
            )

        if fn_name == "rpc_registrar_empleado_sucursal":
            sid = int(params["p_id_sucursal"])
            rol = params["p_rol"]
            dni = (params.get("p_dni") or "").strip() or None
            email = (params.get("p_email") or "").strip().lower() or None
            emp_id = params.get("p_id_empleado")
            existing = next(
                (
                    e for e in self.tables["empleado"]
                    if (emp_id and e["id_empleado"] == emp_id)
                    or (dni and e.get("dni") == dni)
                    or (email and (e.get("email") or "").lower() == email)
                ),
                None,
            )
            creado = False
            if existing is None:
                existing = {
                    "id_empleado": self._next_id(),
                    "auth_user_id": None,
                    "nombre": params.get("p_nombre"),
                    "apellido": params.get("p_apellido"),
                    "dni": dni,
                    "email": email,
                    "telefono": params.get("p_telefono"),
                    "estado": True,
                    "fecha_creacion": "2026-01-01T00:00:00Z",
                }
                self.tables["empleado"].append(existing)
                creado = True
            asig = next(
                (
                    es for es in self.tables["empleado_sucursal"]
                    if es["id_empleado"] == existing["id_empleado"] and es["id_sucursal"] == sid
                ),
                None,
            )
            if asig is None:
                asig = {
                    "id_empleado_sucursal": self._next_id(),
                    "id_empleado": existing["id_empleado"],
                    "id_sucursal": sid,
                    "rol": rol,
                    "estado": True,
                    "fecha_asignacion": "2026-01-01",
                }
                self.tables["empleado_sucursal"].append(asig)
            else:
                asig["estado"] = True
                asig["rol"] = rol
            return SimpleNamespace(
                execute=lambda: SimpleNamespace(
                    data={
                        "creado": creado,
                        "empleado": copy.deepcopy(existing),
                        "empleado_sucursal": copy.deepcopy(asig),
                    }
                )
            )

        if fn_name == "rpc_registrar_administrador_gimnasio":
            gid = int(params["p_id_gimnasio"])
            dni = (params.get("p_dni") or "").strip() or None
            email = (params.get("p_email") or "").strip().lower() or None
            existing = next(
                (
                    a for a in self.tables["administrador"]
                    if (email and (a.get("email") or "").lower() == email) or (dni and a.get("dni") == dni)
                ),
                None,
            )
            creado = False
            if existing is None:
                existing = {
                    "id_administrador": self._next_id(),
                    "auth_user_id": None,
                    "nombre": params.get("p_nombre"),
                    "apellido": params.get("p_apellido"),
                    "dni": dni,
                    "email": email,
                    "telefono": params.get("p_telefono"),
                    "estado": True,
                    "fecha_creacion": "2026-01-01T00:00:00Z",
                }
                self.tables["administrador"].append(existing)
                creado = True
            ag = next(
                (
                    x for x in self.tables["administrador_gimnasio"]
                    if x["id_administrador"] == existing["id_administrador"] and x["id_gimnasio"] == gid
                ),
                None,
            )
            if ag is None:
                ag = {
                    "id_administrador_gimnasio": self._next_id(),
                    "id_administrador": existing["id_administrador"],
                    "id_gimnasio": gid,
                    "estado": True,
                    "fecha_asignacion": "2026-01-01",
                }
                self.tables["administrador_gimnasio"].append(ag)
            else:
                ag["estado"] = True
            return SimpleNamespace(
                execute=lambda: SimpleNamespace(
                    data={
                        "creado": creado,
                        "administrador": copy.deepcopy(existing),
                        "administrador_gimnasio": copy.deepcopy(ag),
                    }
                )
            )

        raise NotImplementedError(fn_name)


class TestCuentasYAutenticacionMultiGimnasio(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = ec.generate_private_key(ec.SECP256R1())
        cls.public_key = cls.private_key.public_key()
        cls.issuer = "https://mock-project.supabase.co/auth/v1"
        cls.audience = "authenticated"
        cls.client = TestClient(main_module.app)

    def setUp(self):
        account_provisioning.reset_lookup_rate_limit()
        self.fake_auth = FakeAuthAdmin()
        self.db = FakeGlobalDB(self.fake_auth, is_admin_client=False)
        self.admin_db = self.db.clone_as_admin()

        # UIDs de prueba
        self.uid_dueno_1 = "11111111-1111-4111-8111-000000000001"
        self.uid_dueno_2 = "11111111-1111-4111-8111-000000000002"
        self.uid_admin_multi = "22222222-2222-4222-8222-000000000001"
        self.uid_emp_multi = "33333333-3333-4333-8333-000000000001"
        self.uid_socio_multi = "44444444-4444-4444-8444-000000000001"
        self.uid_socio_unico = "44444444-4444-4444-8444-000000000002"

        self.fake_auth.seed_user(self.uid_dueno_1, "dueno1@gymtrack.test", "DuenoPass1!")
        self.fake_auth.seed_user(self.uid_dueno_2, "dueno2@gymtrack.test", "DuenoPass2!")
        self.fake_auth.seed_user(self.uid_admin_multi, "admin@gymtrack.test", "AdminPass1!")
        self.fake_auth.seed_user(self.uid_emp_multi, "empleado@gymtrack.test", "EmpPass1!")
        self.fake_auth.seed_user(self.uid_socio_multi, "socio.multi@gymtrack.test", "SocioPass1!")
        self.fake_auth.seed_user(self.uid_socio_unico, "socio.unico@gymtrack.test", "SocioUnico1!")

        self.db.tables["dueno"] = [
            {"id_dueno": 1, "auth_user_id": self.uid_dueno_1, "nombre": "Dueño", "apellido": "Uno", "email": "dueno1@gymtrack.test"},
            {"id_dueno": 2, "auth_user_id": self.uid_dueno_2, "nombre": "Dueño", "apellido": "Dos", "email": "dueno2@gymtrack.test"},
        ]
        self.db.tables["gimnasio"] = [
            {"id_gimnasio": 10, "nombre": "Gym Norte", "estado": True, "id_dueno": 1},
            {"id_gimnasio": 20, "nombre": "Gym Sur", "estado": True, "id_dueno": 2},
        ]
        self.db.tables["sucursal"] = [
            {"id_sucursal": 101, "direccion": "Av Norte 1", "descripcion": "Norte Central", "estado": True, "id_gimnasio": 10},
            {"id_sucursal": 102, "direccion": "Av Norte 2", "descripcion": "Norte Anexo", "estado": True, "id_gimnasio": 10},
            {"id_sucursal": 201, "direccion": "Av Sur 1", "descripcion": "Sur Central", "estado": True, "id_gimnasio": 20},
        ]
        self.db.tables["administrador"] = [
            {"id_administrador": 30, "auth_user_id": self.uid_admin_multi, "nombre": "Ana", "apellido": "Admin", "dni": "20111222", "email": "admin@gymtrack.test", "estado": True},
        ]
        self.db.tables["administrador_gimnasio"] = [
            {"id_administrador_gimnasio": 301, "id_administrador": 30, "id_gimnasio": 10, "estado": True},
            {"id_administrador_gimnasio": 302, "id_administrador": 30, "id_gimnasio": 20, "estado": True},
        ]
        self.db.tables["empleado"] = [
            {"id_empleado": 40, "auth_user_id": self.uid_emp_multi, "nombre": "Luis", "apellido": "Staff", "dni": "30111222", "email": "empleado@gymtrack.test", "estado": True},
        ]
        self.db.tables["empleado_sucursal"] = [
            {"id_empleado_sucursal": 401, "id_empleado": 40, "id_sucursal": 101, "rol": "recepcionista", "estado": True},
            {"id_empleado_sucursal": 402, "id_empleado": 40, "id_sucursal": 102, "rol": "entrenador", "estado": True},
            {"id_empleado_sucursal": 403, "id_empleado": 40, "id_sucursal": 201, "rol": "administrador", "estado": True},
        ]
        self.db.tables["socio"] = [
            {"id_socio": 70, "auth_user_id": self.uid_socio_multi, "nombre": "Sofía", "apellido": "Multi", "dni": "40111222", "email": "socio.multi@gymtrack.test", "estado": True},
            {"id_socio": 71, "auth_user_id": self.uid_socio_unico, "nombre": "Marcos", "apellido": "Único", "dni": "40999888", "email": "socio.unico@gymtrack.test", "estado": True},
        ]
        self.db.tables["socio_gimnasio"] = [
            {"id_socio_gimnasio": 701, "id_socio": 70, "id_gimnasio": 10, "estado": True},
            {"id_socio_gimnasio": 702, "id_socio": 70, "id_gimnasio": 20, "estado": True},
            {"id_socio_gimnasio": 703, "id_socio": 71, "id_gimnasio": 10, "estado": True},
        ]

    def _make_token(self, uid: str, email: str, expired: bool = False) -> str:
        now = datetime.now(timezone.utc)
        exp = now - timedelta(minutes=5) if expired else now + timedelta(hours=1)
        payload = {
            "sub": uid,
            "email": email,
            "role": "authenticated",
            "aud": self.audience,
            "iss": self.issuer,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "email_confirmed_at": "2026-01-01T00:00:00Z",
        }
        return jwt.encode(payload, self.private_key, algorithm="ES256", headers={"kid": "test-kid"})

    def _build_fake_anon(self):
        fake_auth = self.fake_auth

        def sign_in(creds: Dict[str, str]):
            email = str(creds.get("email") or "").strip().lower()
            pwd = str(creds.get("password") or "")
            u = fake_auth.users_by_email.get(email)
            if not u or u["password"] != pwd:
                raise Exception("Invalid login credentials")
            tok = self._make_token(u["id"], u["email"])
            return SimpleNamespace(
                session=SimpleNamespace(access_token=tok, refresh_token=f"ref-{u['id']}", expires_at=9999999999),
                user=SimpleNamespace(id=u["id"], email=u["email"], email_confirmed_at=u["email_confirmed_at"]),
            )

        def reset_pwd(email: str):
            fake_auth.reset_calls.append(email)
            return {}

        return SimpleNamespace(
            auth=SimpleNamespace(
                sign_in_with_password=sign_in,
                reset_password_for_email=reset_pwd,
            )
        )

    def _patch_env(self):
        fake_jwk = SimpleNamespace(
            get_signing_key_from_jwt=lambda _t: SimpleNamespace(key=self.public_key)
        )
        fake_anon = self._build_fake_anon()
        return (
            patch("app.core.security.get_jwk_client", return_value=fake_jwk),
            patch("app.core.security.SUPABASE_JWT_ISSUER", self.issuer),
            patch("app.core.security.SUPABASE_JWT_AUDIENCE", self.audience),
            patch("app.core.dependencies.create_supabase_client_for_token", return_value=self.db),
            patch("app.routers.auth.create_supabase_client_for_token", return_value=self.db),
            patch("app.routers.auth.supabase_anon", fake_anon),
            patch("app.services.account_provisioning.supabase_admin", self.admin_db),
            patch("app.services.account_provisioning.supabase_anon", fake_anon),
        )

    def _with_env(self, fn):
        patches = self._patch_env()
        for p in patches:
            p.start()
        try:
            return fn()
        finally:
            for p in reversed(patches):
                p.stop()

    # ------------------------------------------------------------------
    # AUTENTICACIÓN POR ROL Y SELECTOR DE GIMNASIO (Tests 1-8, 19-25)
    # ------------------------------------------------------------------

    def test_01_login_separado_por_rol_y_rechazo_cruce_formularios(self):
        def run():
            # 1. Socio inicia sesión -> OK y recibe selector porque tiene 2 gimnasios
            r_soc = self.client.post("/api/auth/login-socio", json={"email": "socio.multi@gymtrack.test", "contrasena": "SocioPass1!"})
            self.assertEqual(r_soc.status_code, 200)
            body_soc = r_soc.json()
            self.assertTrue(body_soc["requiere_seleccion_gimnasio"])
            self.assertEqual(len(body_soc["contextos_disponibles"]), 2)
            self.assertNotIn("contrasena", str(body_soc).lower())

            # Socio con 1 solo gimnasio -> NO requiere selector
            r_soc1 = self.client.post("/api/auth/login-socio", json={"email": "socio.unico@gymtrack.test", "contrasena": "SocioUnico1!"})
            self.assertEqual(r_soc1.status_code, 200)
            self.assertFalse(r_soc1.json()["requiere_seleccion_gimnasio"])
            self.assertEqual(len(r_soc1.json()["contextos_disponibles"]), 1)

            # 2. Dueño inicia sesión -> OK (1 gym -> directo)
            r_due = self.client.post("/api/auth/login-dueno", json={"email": "dueno1@gymtrack.test", "contrasena": "DuenoPass1!"})
            self.assertEqual(r_due.status_code, 200)
            self.assertEqual(r_due.json()["usuario"]["rol"], "dueño")
            self.assertFalse(r_due.json()["requiere_seleccion_gimnasio"])

            # 3. Administrador multi-gimnasio -> OK y requiere selector (2 gyms)
            r_adm = self.client.post("/api/auth/login-administrador", json={"email": "admin@gymtrack.test", "contrasena": "AdminPass1!"})
            self.assertEqual(r_adm.status_code, 200)
            self.assertEqual(r_adm.json()["usuario"]["rol"], "administrador")
            self.assertTrue(r_adm.json()["requiere_seleccion_gimnasio"])
            self.assertEqual(len(r_adm.json()["contextos_disponibles"]), 2)

            # 4. Empleado multi-sucursal/multi-gimnasio -> OK y recibe las 3 asignaciones (sucursal + subrol)
            r_emp = self.client.post("/api/auth/login-empleado", json={"email": "empleado@gymtrack.test", "contrasena": "EmpPass1!"})
            self.assertEqual(r_emp.status_code, 200)
            emp_ctx = r_emp.json()["contextos_disponibles"]
            self.assertTrue(r_emp.json()["requiere_seleccion_gimnasio"])
            self.assertEqual(len(emp_ctx), 3)
            self.assertEqual({c["id_sucursal"] for c in emp_ctx}, {101, 102, 201})

            # 5. Rechazo estricto al intentar entrar desde un formulario cuyo rol no posee
            self.assertEqual(
                self.client.post("/api/auth/login-dueno", json={"email": "socio.unico@gymtrack.test", "contrasena": "SocioUnico1!"}).status_code,
                403,
            )
            self.assertEqual(
                self.client.post("/api/auth/login-administrador", json={"email": "empleado@gymtrack.test", "contrasena": "EmpPass1!"}).status_code,
                403,
            )
            self.assertEqual(
                self.client.post("/api/auth/login-empleado", json={"email": "dueno1@gymtrack.test", "contrasena": "DuenoPass1!"}).status_code,
                403,
            )
            self.assertEqual(
                self.client.post("/api/auth/login-socio", json={"email": "dueno1@gymtrack.test", "contrasena": "DuenoPass1!"}).status_code,
                403,
            )

            # 7 y 8. Token inválido o expirado -> 401
            self.assertEqual(
                self.client.get("/api/auth/contextos", headers={"Authorization": "Bearer token-invalido"}).status_code,
                401,
            )
            exp_tok = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test", expired=True)
            self.assertEqual(
                self.client.get("/api/auth/contextos", headers={"Authorization": f"Bearer {exp_tok}"}).status_code,
                401,
            )

        self._with_env(run)

    # ------------------------------------------------------------------
    # CUENTAS GLOBALES, APROVISIONAMIENTO Y NO DUPLICACIÓN (Tests 9-18)
    # ------------------------------------------------------------------

    def test_02_crear_cuentas_nuevas_con_contrasena_inicial_y_reutilizar_existentes(self):
        def run():
            tok_d1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
            tok_d2 = self._make_token(self.uid_dueno_2, "dueno2@gymtrack.test")

            # 9. Dueño 1 crea socio NUEVO con contraseña inicial (sin email_confirm=True)
            res_new = self.client.post(
                "/api/socios",
                headers={"Authorization": f"Bearer {tok_d1}", "X-Gimnasio-Id": "10"},
                json={
                    "nombre": "Lucía",
                    "apellido": "Nueva",
                    "dni": "45123456",
                    "email": "lucia.nueva@gymtrack.test",
                    "telefono": "1155551111",
                    "contrasena_inicial": "Inicial123!",
                },
            )
            self.assertEqual(res_new.status_code, 201, res_new.text)
            body_new = res_new.json()
            self.assertTrue(body_new["creado"])
            self.assertTrue(body_new["cuenta_auth_creada"])
            self.assertNotIn("contrasena", body_new["socio"])
            self.assertNotIn("Inicial123!", res_new.text)
            self.assertEqual(len(self.fake_auth.create_calls), 1)
            self.assertIs(self.fake_auth.create_calls[0]["email_confirm"], False)

            # Verificar que la socia recién creada puede autenticarse con la contraseña inicial establecida
            r_login_new = self.client.post(
                "/api/auth/login-socio",
                json={"email": "lucia.nueva@gymtrack.test", "contrasena": "Inicial123!"},
            )
            self.assertEqual(r_login_new.status_code, 200)

            # 12 y 7.1. Dueño 2 detecta a la socia existente SIN recibir datos privados ni teléfono
            res_det = self.client.post(
                "/api/auth/identidades/verificar",
                headers={"Authorization": f"Bearer {tok_d2}", "X-Gimnasio-Id": "20"},
                json={"tipo_perfil": "socio", "dni": "45123456", "id_gimnasio": 20},
            )
            self.assertEqual(res_det.status_code, 200)
            det = res_det.json()
            self.assertTrue(det["existe_en_gymtrack"])
            self.assertTrue(det["tiene_cuenta_auth"])
            self.assertFalse(det["requiere_contrasena_inicial"])
            self.assertIn("¿Querés incorporarla a tu gimnasio?", det["mensaje"])
            self.assertNotIn("telefono", det)
            self.assertNotIn("auth_user_id", det)

            # 18. Dueño 2 intenta sobrescribir la contraseña de la cuenta existente -> 409 bloqueado!
            res_overwrite = self.client.post(
                "/api/socios",
                headers={"Authorization": f"Bearer {tok_d2}", "X-Gimnasio-Id": "20"},
                json={
                    "dni": "45123456",
                    "email": "lucia.nueva@gymtrack.test",
                    "contrasena_inicial": "HackerPass999!",
                },
            )
            self.assertEqual(res_overwrite.status_code, 409)
            self.assertEqual(self.fake_auth.users_by_email["lucia.nueva@gymtrack.test"]["password"], "Inicial123!")

            # 12, 15, 16. Dueño 2 incorpora a la socia existente sin contraseña -> reutiliza misma cuenta Auth y mismo id_socio
            res_inc = self.client.post(
                "/api/socios",
                headers={"Authorization": f"Bearer {tok_d2}", "X-Gimnasio-Id": "20"},
                json={
                    "dni": "45123456",
                    "email": "lucia.nueva@gymtrack.test",
                    "confirmar_incorporacion": True,
                },
            )
            self.assertEqual(res_inc.status_code, 201)
            self.assertFalse(res_inc.json()["creado"])
            self.assertTrue(res_inc.json()["cuenta_auth_reutilizada"])
            self.assertEqual(len(self.fake_auth.create_calls), 1)  # NO creó segunda cuenta Auth

            # 17. Intentar asociarla de nuevo al mismo Gimnasio 20 -> 409
            res_dup = self.client.post(
                "/api/socios",
                headers={"Authorization": f"Bearer {tok_d2}", "X-Gimnasio-Id": "20"},
                json={"dni": "45123456"},
            )
            self.assertEqual(res_dup.status_code, 409)

            # 10 y 13. Crear empleado nuevo con contraseña inicial y reutilizarlo en otro gimnasio sin duplicar Auth
            res_emp_new = self.client.post(
                "/api/empleados",
                headers={"Authorization": f"Bearer {tok_d1}", "X-Gimnasio-Id": "10"},
                json={
                    "id_sucursal": 101,
                    "rol": "entrenador",
                    "nombre": "Carlos",
                    "apellido": "Profe",
                    "dni": "33999111",
                    "email": "carlos.profe@gymtrack.test",
                    "contrasena_inicial": "Profe123!",
                },
            )
            self.assertEqual(res_emp_new.status_code, 201, res_emp_new.text)
            self.assertTrue(res_emp_new.json()["creado"])
            self.assertTrue(res_emp_new.json()["cuenta_auth_creada"])
            self.assertNotIn("id_gimnasio", res_emp_new.json()["empleado"])

            # Dueño 2 intenta incorporar a Carlos con nueva contraseña -> 409
            self.assertEqual(
                self.client.post(
                    "/api/empleados",
                    headers={"Authorization": f"Bearer {tok_d2}", "X-Gimnasio-Id": "20"},
                    json={
                        "id_sucursal": 201,
                        "rol": "recepcionista",
                        "dni": "33999111",
                        "email": "carlos.profe@gymtrack.test",
                        "contrasena_inicial": "OtraClave99!",
                    },
                ).status_code,
                409,
            )

            # Dueño 2 incorpora a Carlos sin contraseña -> reutiliza misma identidad
            res_emp_inc = self.client.post(
                "/api/empleados",
                headers={"Authorization": f"Bearer {tok_d2}", "X-Gimnasio-Id": "20"},
                json={
                    "id_sucursal": 201,
                    "rol": "recepcionista",
                    "dni": "33999111",
                    "email": "carlos.profe@gymtrack.test",
                    "confirmar_incorporacion": True,
                },
            )
            self.assertEqual(res_emp_inc.status_code, 201)
            self.assertFalse(res_emp_inc.json()["creado"])
            self.assertTrue(res_emp_inc.json()["cuenta_auth_reutilizada"])

            # 11 y 14. Crear administrador nuevo con contraseña inicial y reutilizarlo en Gym 20
            res_adm_new = self.client.post(
                "/api/administradores",
                headers={"Authorization": f"Bearer {tok_d1}", "X-Gimnasio-Id": "10"},
                json={
                    "id_gimnasio": 10,
                    "nombre": "Valeria",
                    "apellido": "Gestora",
                    "dni": "27555444",
                    "email": "valeria.admin@gymtrack.test",
                    "contrasena_inicial": "AdminNew123!",
                },
            )
            self.assertEqual(res_adm_new.status_code, 201, res_adm_new.text)
            self.assertTrue(res_adm_new.json()["cuenta_auth_creada"])

            res_adm_inc = self.client.post(
                "/api/administradores",
                headers={"Authorization": f"Bearer {tok_d2}", "X-Gimnasio-Id": "20"},
                json={
                    "id_gimnasio": 20,
                    "nombre": "Valeria",
                    "apellido": "Gestora",
                    "dni": "27555444",
                    "email": "valeria.admin@gymtrack.test",
                    "confirmar_incorporacion": True,
                },
            )
            self.assertEqual(res_adm_inc.status_code, 201)
            self.assertFalse(res_adm_inc.json()["creado"])
            self.assertTrue(res_adm_inc.json()["cuenta_auth_reutilizada"])
            self.assertEqual(len(self.fake_auth.create_calls), 3)  # Exactamente 3 cuentas Auth globales creadas

        self._with_env(run)

    def test_03_compensacion_sin_cuenta_auth_huerfana_si_falla_vinculacion(self):
        def run():
            tok_d1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
            self.admin_db.fail_update_on_table = "socio"
            res = self.client.post(
                "/api/socios",
                headers={"Authorization": f"Bearer {tok_d1}", "X-Gimnasio-Id": "10"},
                json={
                    "nombre": "Fallo",
                    "apellido": "Rollback",
                    "dni": "48000111",
                    "email": "rollback@gymtrack.test",
                    "contrasena_inicial": "Rollback123!",
                },
            )
            self.assertEqual(res.status_code, 500)
            self.assertEqual(len(self.fake_auth.delete_calls), 1)
            self.assertNotIn("rollback@gymtrack.test", self.fake_auth.users_by_email)

        self._with_env(run)

    # ------------------------------------------------------------------
    # CAMBIO DE CONTRASEÑA Y RECUPERACIÓN (Tests 26-31)
    # ------------------------------------------------------------------

    def test_04_cambio_voluntario_contrasena_invalida_anterior_y_recuperacion_generica(self):
        def run():
            tok_socio = self._make_token(self.uid_socio_unico, "socio.unico@gymtrack.test")

            async def fake_update_auth_user(_token: str, payload: Dict[str, Any]):
                if "password" in payload:
                    self.fake_auth.users_by_email["socio.unico@gymtrack.test"]["password"] = payload["password"]
                return {"email": "socio.unico@gymtrack.test"}

            with patch("app.routers.auth._update_supabase_auth_user_with_token", new=AsyncMock(side_effect=fake_update_auth_user)):
                # Contraseña actual incorrecta -> 400
                r_bad = self.client.post(
                    "/api/auth/cambiar-contrasena",
                    headers={"Authorization": f"Bearer {tok_socio}"},
                    json={
                        "contrasena_actual": "Incorrecta!",
                        "nueva_contrasena": "NuevaClave2026!",
                        "confirmar_contrasena": "NuevaClave2026!",
                    },
                )
                self.assertEqual(r_bad.status_code, 400)

                # Cambio exitoso -> actualiza contraseña y nunca devuelve contraseñas
                r_ok = self.client.post(
                    "/api/auth/cambiar-contrasena",
                    headers={"Authorization": f"Bearer {tok_socio}"},
                    json={
                        "contrasena_actual": "SocioUnico1!",
                        "nueva_contrasena": "NuevaClave2026!",
                        "confirmar_contrasena": "NuevaClave2026!",
                    },
                )
                self.assertEqual(r_ok.status_code, 200)
                self.assertNotIn("NuevaClave2026!", r_ok.text)
                self.assertNotIn("SocioUnico1!", r_ok.text)

            # La contraseña anterior deja de funcionar y la nueva funciona
            self.assertEqual(
                self.client.post("/api/auth/login-socio", json={"email": "socio.unico@gymtrack.test", "contrasena": "SocioUnico1!"}).status_code,
                401,
            )
            self.assertEqual(
                self.client.post("/api/auth/login-socio", json={"email": "socio.unico@gymtrack.test", "contrasena": "NuevaClave2026!"}).status_code,
                200,
            )

            # Recuperación de contraseña devuelve siempre 200 genérico sin revelar existencia
            r_rec = self.client.post("/api/auth/recuperar-contrasena", json={"email": "inexistente@gymtrack.test"})
            self.assertEqual(r_rec.status_code, 200)
            self.assertEqual(self.fake_auth.reset_calls, ["inexistente@gymtrack.test"])

        self._with_env(run)

    # ------------------------------------------------------------------
    # SEGURIDAD MULTI-GIMNASIO Y MANIPULACIÓN DE HEADERS (Tests 22-25, 32-37)
    # ------------------------------------------------------------------

    def test_05_seguridad_multigimnasio_headers_y_permisos_incorporacion(self):
        def run():
            tok_socio_unico = self._make_token(self.uid_socio_unico, "socio.unico@gymtrack.test")
            tok_dueno_1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")

            # 22 y 32. Socio único (solo Gym 10) intenta manipular X-Gimnasio-Id hacia Gym 20 -> 403
            self.assertEqual(
                self.client.get(
                    "/api/auth/contextos",
                    headers={"Authorization": f"Bearer {tok_socio_unico}", "X-Gimnasio-Id": "20"},
                ).status_code,
                403,
            )

            # 24. Socio intenta manipular X-Active-Role a dueño -> 403
            self.assertEqual(
                self.client.get(
                    "/api/auth/contextos",
                    headers={"Authorization": f"Bearer {tok_socio_unico}", "X-Active-Role": "dueno"},
                ).status_code,
                403,
            )

            # 36. Socio sin permisos intenta usar verificación de identidades -> 403
            self.assertEqual(
                self.client.post(
                    "/api/auth/identidades/verificar",
                    headers={"Authorization": f"Bearer {tok_socio_unico}"},
                    json={"tipo_perfil": "socio", "dni": "40111222", "id_gimnasio": 10},
                ).status_code,
                403,
            )

            # Dueño 1 intenta consultar/crear sobre gimnasio ajeno (20) -> 403
            self.assertEqual(
                self.client.post(
                    "/api/auth/identidades/verificar",
                    headers={"Authorization": f"Bearer {tok_dueno_1}", "X-Gimnasio-Id": "10"},
                    json={"tipo_perfil": "administrador", "email": "admin@gymtrack.test", "id_gimnasio": 20},
                ).status_code,
                403,
            )

            # 23 y 33. Empleado no opera en sucursales no asignadas (sucursal 999 / ajena) -> 403
            tok_emp = self._make_token(self.uid_emp_multi, "empleado@gymtrack.test")
            self.assertEqual(
                self.client.get(
                    "/api/auth/contextos",
                    headers={"Authorization": f"Bearer {tok_emp}", "X-Sucursal-Id": "999"},
                ).status_code,
                403,
            )

            # 25. Logout limpia caché sin error
            self.assertEqual(
                self.client.post("/api/auth/logout", headers={"Authorization": f"Bearer {tok_dueno_1}"}).status_code,
                200,
            )

        self._with_env(run)


if __name__ == "__main__":
    unittest.main()
