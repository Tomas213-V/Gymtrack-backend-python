"""
Suite de tests unitarios (con mocks controlados; NO conecta contra la API real de Mercado Pago)
para la integración OAuth de Mercado Pago en el perfil del gimnasio.

Cubre las validaciones obligatorias:
- Autorización estricta (dueño/admin del gimnasio vs socio/empleado/otro dueño -> 403).
- Bloqueo si OAuth no está configurado (jamás simula conexión exitosa).
- Validación de `state` anti-CSRF y anti-replay (400).
- Cifrado real Fernet de access_token y refresh_token en `integracion_pago_gimnasio`.
- Ningún token ni código OAuth expuesto en respuestas HTTP ni redirecciones.
- Máximo una cuenta de Mercado Pago por gimnasio (409 si ya conectado sin `reconectar=True`).
- Desconexión preserva intacto el historial en `public.pago`.
- Manejo limpio de errores OAuth.
"""

import copy
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import jwt
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

import app.config as app_config
import main as main_module
from app.services import mercadopago_oauth


class FakeIntegracionQuery:
    def __init__(self, db: "FakeIntegracionDB", table: str, *, role: str):
        self.db = db
        self.table = table
        self.role = role
        self._select_cols = "*"
        self._eq: Dict[str, Any] = {}
        self._in: Dict[str, List[Any]] = {}
        self._insert: Optional[List[Dict[str, Any]]] = None
        self._update: Optional[Dict[str, Any]] = None
        self._single = False
        self._returning = "representation"

    def select(self, cols: str = "*"):
        self._select_cols = cols
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

    def insert(self, rows: List[Dict[str, Any]], *, returning: str = "representation"):
        self._insert = [copy.deepcopy(r) for r in rows]
        self._returning = returning
        return self

    def update(self, payload: Dict[str, Any]):
        self._update = copy.deepcopy(payload)
        return self

    def _matches(self, row: Dict[str, Any]) -> bool:
        for k, v in self._eq.items():
            if row.get(k) != v:
                return False
        for k, vals in self._in.items():
            if row.get(k) not in vals:
                return False
        return True

    def execute(self):
        self.db.access_log.append((self.role, self.table))

        # Simulación fiel de PostgreSQL/Supabase:
        # `public.integracion_pago_gimnasio` tiene RLS habilitada y CERO permisos a `anon`/`authenticated`.
        if self.table == "integracion_pago_gimnasio" and self.role != "service_role":
            raise APIError(
                {
                    "code": "42501",
                    "message": "permission denied for table integracion_pago_gimnasio",
                }
            )

        rows = self.db.tables.setdefault(self.table, [])
        if self._insert is not None:
            inserted = []
            for r in self._insert:
                if self.table == "integracion_pago_gimnasio" and "id_integracion_pago" not in r:
                    self.db.seq += 1
                    r["id_integracion_pago"] = self.db.seq
                rows.append(r)
                inserted.append(copy.deepcopy(r))
            return SimpleNamespace(data=[] if self._returning == "minimal" else inserted)

        if self._update is not None:
            updated = []
            for r in rows:
                if self._matches(r):
                    r.update(self._update)
                    updated.append(copy.deepcopy(r))
            return SimpleNamespace(data=updated)

        matched = [self.db._hydrate(self.table, copy.deepcopy(r)) for r in rows if self._matches(r)]
        if self._select_cols != "*" and self.table == "integracion_pago_gimnasio":
            allowed = {c.strip() for c in self._select_cols.split(",") if c.strip()}
            matched = [{k: v for k, v in m.items() if k in allowed} for m in matched]

        if self._single:
            return SimpleNamespace(data=matched[0] if matched else None)
        return SimpleNamespace(data=matched)


class FakeScopedClient:
    def __init__(self, db: "FakeIntegracionDB", role: str):
        self._db = db
        self.role = role

    def from_(self, table: str) -> FakeIntegracionQuery:
        return FakeIntegracionQuery(self._db, table, role=self.role)


class FakeIntegracionDB:
    def __init__(self):
        self.seq = 500
        self.access_log: List[tuple[str, str]] = []
        self.tables: Dict[str, List[Dict[str, Any]]] = {
            "dueno": [],
            "administrador": [],
            "administrador_gimnasio": [],
            "empleado": [],
            "empleado_sucursal": [],
            "socio": [],
            "socio_gimnasio": [],
            "gimnasio": [],
            "sucursal": [],
            "integracion_pago_gimnasio": [],
            "pago": [
                {"id_pago": 9001, "id_membresia_socio": 50, "monto": 25000.0, "metodo_pago": "mercadopago", "estado": "aprobado"}
            ],
        }
        self.user_client = FakeScopedClient(self, role="authenticated")
        self.admin_client = FakeScopedClient(self, role="service_role")

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
        return row

    def from_(self, table: str) -> FakeIntegracionQuery:
        return self.user_client.from_(table)


class TestIntegracionMercadoPagoOAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = ec.generate_private_key(ec.SECP256R1())
        cls.public_key = cls.private_key.public_key()
        cls.issuer = "https://mock-mp.supabase.co/auth/v1"
        cls.audience = "authenticated"
        cls.fernet_key = Fernet.generate_key().decode("utf-8")
        cls.client = TestClient(main_module.app)

    def setUp(self):
        mercadopago_oauth.reset_oauth_pending_states()
        from app.routers import integraciones_pago
        integraciones_pago.reset_demo_integrations_store()
        self.db = FakeIntegracionDB()

        self.uid_dueno_1 = "aaaa1111-1111-4111-8111-000000000001"
        self.uid_dueno_2 = "aaaa1111-1111-4111-8111-000000000002"
        self.uid_emp_1 = "bbbb1111-1111-4111-8111-000000000001"
        self.uid_socio_1 = "cccc1111-1111-4111-8111-000000000001"

        self.db.tables["dueno"] = [
            {"id_dueno": 1, "auth_user_id": self.uid_dueno_1, "nombre": "Ana", "apellido": "Dueña", "email": "d1@test.com"},
            {"id_dueno": 2, "auth_user_id": self.uid_dueno_2, "nombre": "Beto", "apellido": "Otro", "email": "d2@test.com"},
        ]
        self.db.tables["gimnasio"] = [
            {"id_gimnasio": 10, "nombre": "Power Norte", "estado": True, "id_dueno": 1},
            {"id_gimnasio": 20, "nombre": "Power Sur", "estado": True, "id_dueno": 2},
        ]
        self.db.tables["sucursal"] = [
            {"id_sucursal": 101, "direccion": "Av 1", "estado": True, "id_gimnasio": 10},
        ]
        self.db.tables["empleado"] = [
            {"id_empleado": 40, "auth_user_id": self.uid_emp_1, "nombre": "Emp", "apellido": "Uno", "email": "emp@test.com", "estado": True},
        ]
        self.db.tables["empleado_sucursal"] = [
            {"id_empleado_sucursal": 401, "id_empleado": 40, "id_sucursal": 101, "rol": "administrador", "estado": True},
        ]
        self.db.tables["socio"] = [
            {"id_socio": 70, "auth_user_id": self.uid_socio_1, "nombre": "Soc", "apellido": "Uno", "email": "soc@test.com", "estado": True},
        ]
        self.db.tables["socio_gimnasio"] = [
            {"id_socio_gimnasio": 701, "id_socio": 70, "id_gimnasio": 10, "estado": True},
        ]

    def _make_token(self, uid: str, email: str) -> str:
        now = datetime.now(timezone.utc)
        payload = {
            "sub": uid,
            "email": email,
            "role": "authenticated",
            "aud": self.audience,
            "iss": self.issuer,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
            "email_confirmed_at": "2026-01-01T00:00:00Z",
        }
        return jwt.encode(payload, self.private_key, algorithm="ES256", headers={"kid": "mp-kid"})

    def _with_env(
        self,
        fn,
        *,
        oauth_configured: bool = True,
        service_role_configured: bool = True,
        demo_mode: bool = False,
        client_id_override: Optional[str] = None,
        client_secret_override: Optional[str] = None,
        redirect_uri_override: Optional[str] = None,
        enc_key_override: Optional[str] = None,
    ):
        fake_jwk = SimpleNamespace(get_signing_key_from_jwt=lambda _t: SimpleNamespace(key=self.public_key))
        c_id = client_id_override if client_id_override is not None else ("1234567890" if oauth_configured else "")
        c_sec = client_secret_override if client_secret_override is not None else ("super-secret-mp" if oauth_configured else "")
        r_uri = (
            redirect_uri_override
            if redirect_uri_override is not None
            else ("http://127.0.0.1:3000/api/integraciones/mercadopago/callback" if oauth_configured else "")
        )
        e_key = enc_key_override if enc_key_override is not None else (self.fernet_key if oauth_configured else "")

        patches = [
            patch("app.core.security.get_jwk_client", return_value=fake_jwk),
            patch("app.core.security.SUPABASE_JWT_ISSUER", self.issuer),
            patch("app.core.security.SUPABASE_JWT_AUDIENCE", self.audience),
            patch("app.core.dependencies.create_supabase_client_for_token", return_value=self.db.user_client),
            patch("app.config.create_supabase_client_for_token", return_value=self.db.user_client),
            patch("app.config.supabase_admin", self.db.admin_client if service_role_configured else None),
            patch("app.config.SUPABASE_SERVICE_ROLE_KEY_INVALID", False),
            patch("app.config.MERCADOPAGO_CLIENT_ID", c_id),
            patch("app.config.MERCADOPAGO_CLIENT_SECRET", c_sec),
            patch("app.config.MERCADOPAGO_REDIRECT_URI", r_uri),
            patch("app.config.MERCADOPAGO_TOKEN_ENCRYPTION_KEY", e_key),
            patch("app.config.MERCADOPAGO_OAUTH_DEMO_MODE", demo_mode),
            patch("app.config.MP_VENDEDOR_USER_ID", "2099887766"),
            patch("app.config.MP_VENDEDOR_USUARIO", "TESTUSER_GYMTRACK"),
            patch("app.config.FRONTEND_URL", "http://localhost:5173"),
        ]
        for p in patches:
            p.start()
        try:
            return fn()
        finally:
            for p in reversed(patches):
                p.stop()

    def test_01_autorizacion_estricta_rechaza_usuarios_sin_permisos_y_gimnasio_ajeno(self):
        def run():
            tok_d1 = self._make_token(self.uid_dueno_1, "d1@test.com")
            tok_emp = self._make_token(self.uid_emp_1, "emp@test.com")
            tok_soc = self._make_token(self.uid_socio_1, "soc@test.com")

            # Socio no puede consultar ni iniciar ni desconectar -> 403
            self.assertEqual(
                self.client.get("/api/integraciones/mercadopago", headers={"Authorization": f"Bearer {tok_soc}"}).status_code,
                403,
            )
            self.assertEqual(
                self.client.post("/api/integraciones/mercadopago/conectar", headers={"Authorization": f"Bearer {tok_soc}"}).status_code,
                403,
            )

            # Empleado de sucursal (incluso rol administrador de sucursal) NO puede gestionar integración org -> 403
            self.assertEqual(
                self.client.get("/api/integraciones/mercadopago", headers={"Authorization": f"Bearer {tok_emp}"}).status_code,
                403,
            )

            # Dueño 1 NO puede consultar ni conectar sobre Gimnasio 20 (ajeno) -> 403
            self.assertEqual(
                self.client.get(
                    "/api/integraciones/mercadopago?id_gimnasio=20",
                    headers={"Authorization": f"Bearer {tok_d1}", "X-Gimnasio-Id": "10"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.post(
                    "/api/integraciones/mercadopago/conectar",
                    headers={"Authorization": f"Bearer {tok_d1}", "X-Gimnasio-Id": "10"},
                    json={"id_gimnasio": 20},
                ).status_code,
                403,
            )

        self._with_env(run)

    def test_02_no_simula_conexion_si_oauth_no_esta_configurado(self):
        def run():
            tok_d1 = self._make_token(self.uid_dueno_1, "d1@test.com")
            r_status = self.client.get(
                "/api/integraciones/mercadopago",
                headers={"Authorization": f"Bearer {tok_d1}", "X-Gimnasio-Id": "10"},
            )
            self.assertEqual(r_status.status_code, 200)
            self.assertFalse(r_status.json()["oauth_configurado"])
            self.assertEqual(r_status.json()["estado"], "no_conectado")

            r_conn = self.client.post(
                "/api/integraciones/mercadopago/conectar",
                headers={"Authorization": f"Bearer {tok_d1}", "X-Gimnasio-Id": "10"},
            )
            self.assertEqual(r_conn.status_code, 503)
            self.assertNotIn("authorization_url", r_conn.text)

        self._with_env(run, oauth_configured=False)

    def test_03_flujo_oauth_completo_cifrado_anti_csrf_unicidad_y_desconexion(self):
        def run():
            tok_d1 = self._make_token(self.uid_dueno_1, "d1@test.com")
            headers_d1 = {"Authorization": f"Bearer {tok_d1}", "X-Gimnasio-Id": "10"}

            # 1. Iniciar OAuth
            r_init = self.client.post("/api/integraciones/mercadopago/conectar", headers=headers_d1)
            self.assertEqual(r_init.status_code, 200, r_init.text)
            auth_url = r_init.json()["authorization_url"]
            parsed = urllib.parse.urlparse(auth_url)
            qs = urllib.parse.parse_qs(parsed.query)
            self.assertEqual(qs["client_id"][0], "1234567890")
            self.assertEqual(qs["response_type"][0], "code")
            state_val = qs["state"][0]

            # 2. Callback con state inválido -> rechazado 400
            r_bad_state = self.client.get(
                "/api/integraciones/mercadopago/callback",
                params={"code": "TG-fake", "state": "invalid.state.10.deadbeef"},
                follow_redirects=False,
            )
            self.assertEqual(r_bad_state.status_code, 400)

            # 3. Callback válido intercambia code (mockeado) y cifra tokens antes de persistir
            fake_mp_tokens = {
                "access_token": "APP_USR-secret-access-token-12345",
                "refresh_token": "TG-secret-refresh-token-67890",
                "external_user_id": "9988776655",
                "token_expires_at": "2027-01-01T00:00:00+00:00",
            }
            with patch(
                "app.routers.integraciones_pago.exchange_mercadopago_oauth_code",
                new=AsyncMock(return_value=fake_mp_tokens),
            ):
                r_cb = self.client.get(
                    "/api/integraciones/mercadopago/callback",
                    params={"code": "TG-auth-code-xyz", "state": state_val},
                    follow_redirects=False,
                )
            self.assertEqual(r_cb.status_code, 302)
            location = r_cb.headers["location"]
            self.assertIn("mp_status=connected", location)
            self.assertNotIn("APP_USR-secret-access-token-12345", location)
            self.assertNotIn("TG-secret-refresh-token-67890", location)
            self.assertNotIn("TG-auth-code-xyz", location)

            # 4. Replay del mismo state -> rechazado 400
            r_replay = self.client.get(
                "/api/integraciones/mercadopago/callback",
                params={"code": "TG-auth-code-xyz", "state": state_val},
                follow_redirects=False,
            )
            self.assertEqual(r_replay.status_code, 400)

            # 5. Verificar almacenamiento cifrado real en BD (nunca texto plano)
            stored_rows = self.db.tables["integracion_pago_gimnasio"]
            self.assertEqual(len(stored_rows), 1)
            row = stored_rows[0]
            self.assertEqual(row["estado"], "conectado")
            self.assertEqual(row["external_user_id"], "9988776655")
            self.assertNotEqual(row["access_token_encrypted"], fake_mp_tokens["access_token"])
            self.assertNotEqual(row["refresh_token_encrypted"], fake_mp_tokens["refresh_token"])
            self.assertEqual(
                mercadopago_oauth.decrypt_oauth_token(row["access_token_encrypted"]),
                fake_mp_tokens["access_token"],
            )
            self.assertEqual(
                mercadopago_oauth.decrypt_oauth_token(row["refresh_token_encrypted"]),
                fake_mp_tokens["refresh_token"],
            )

            # 6. GET estado nunca devuelve tokens ni campos cifrados
            r_get = self.client.get("/api/integraciones/mercadopago", headers=headers_d1)
            self.assertEqual(r_get.status_code, 200)
            body_get = r_get.json()
            self.assertTrue(body_get["conectado"])
            self.assertEqual(body_get["estado"], "conectado")
            self.assertEqual(body_get["cuenta"]["external_user_id"], "9988776655")
            self.assertNotIn("access_token", r_get.text)
            self.assertNotIn("refresh_token", r_get.text)
            self.assertNotIn("APP_USR-secret-access-token-12345", r_get.text)

            # 7. Un gimnasio no puede conectar una segunda cuenta sin reconectar=True -> 409
            r_dup = self.client.post("/api/integraciones/mercadopago/conectar", headers=headers_d1)
            self.assertEqual(r_dup.status_code, 409)

            # Con reconectar=True sí permite revincular y actualiza la misma fila (no duplica)
            r_reconn = self.client.post(
                "/api/integraciones/mercadopago/conectar",
                headers=headers_d1,
                json={"reconectar": True},
            )
            self.assertEqual(r_reconn.status_code, 200)

            # 8. Desconectar limpia tokens cifrados pero NO elimina historial de `public.pago`
            r_disc = self.client.post("/api/integraciones/mercadopago/desconectar", headers=headers_d1)
            self.assertEqual(r_disc.status_code, 200)
            self.assertFalse(r_disc.json()["conectado"])
            self.assertEqual(r_disc.json()["estado"], "no_conectado")
            self.assertIsNone(self.db.tables["integracion_pago_gimnasio"][0]["access_token_encrypted"])
            self.assertIsNone(self.db.tables["integracion_pago_gimnasio"][0]["refresh_token_encrypted"])
            self.assertEqual(self.db.tables["integracion_pago_gimnasio"][0]["estado"], "desconectado")
            self.assertEqual(len(self.db.tables["pago"]), 1)
            self.assertEqual(self.db.tables["pago"][0]["id_pago"], 9001)

        self._with_env(run)

    def test_04_error_oauth_desde_mercadopago_se_informa_correctamente(self):
        def run():
            tok_d1 = self._make_token(self.uid_dueno_1, "d1@test.com")
            r_init = self.client.post(
                "/api/integraciones/mercadopago/conectar",
                headers={"Authorization": f"Bearer {tok_d1}", "X-Gimnasio-Id": "10"},
            )
            state_val = urllib.parse.parse_qs(urllib.parse.urlparse(r_init.json()["authorization_url"]).query)["state"][0]

            r_denied = self.client.get(
                "/api/integraciones/mercadopago/callback",
                params={"error": "access_denied", "state": state_val},
                follow_redirects=False,
            )
            self.assertEqual(r_denied.status_code, 302)
            self.assertIn("mp_status=error", r_denied.headers["location"])
            self.assertIn("mp_reason=access_denied", r_denied.headers["location"])
            self.assertEqual(len(self.db.tables["integracion_pago_gimnasio"]), 0)

        self._with_env(run)

    def test_05_separacion_estricta_service_role_vs_authenticated_y_sin_fallback(self):
        def run_ok():
            tok_d1 = self._make_token(self.uid_dueno_1, "d1@test.com")
            tok_soc = self._make_token(self.uid_socio_1, "soc@test.com")

            # Usuario no autorizado NO debe disparar ninguna consulta con service_role
            r_forb = self.client.get("/api/integraciones/mercadopago", headers={"Authorization": f"Bearer {tok_soc}"})
            self.assertEqual(r_forb.status_code, 403)
            self.assertEqual(
                [entry for entry in self.db.access_log if entry[0] == "service_role"],
                [],
            )

            # Dueño autorizado consulta con éxito usando service_role exclusivamente sobre integracion_pago_gimnasio
            self.db.access_log.clear()
            r_ok = self.client.get(
                "/api/integraciones/mercadopago",
                headers={"Authorization": f"Bearer {tok_d1}", "X-Gimnasio-Id": "10"},
            )
            self.assertEqual(r_ok.status_code, 200, r_ok.text)
            mp_accesses = [role for role, tbl in self.db.access_log if tbl == "integracion_pago_gimnasio"]
            self.assertTrue(mp_accesses)
            self.assertTrue(all(role == "service_role" for role in mp_accesses))
            # Tablas de contexto de usuario siguen consultándose con rol `authenticated` (RLS)
            self.assertIn(("authenticated", "dueno"), self.db.access_log)
            self.assertNotIn(("service_role", "dueno"), self.db.access_log)

        self._with_env(run_ok, oauth_configured=False, service_role_configured=True)

        # Sin SUPABASE_SERVICE_ROLE_KEY -> 503 explícito sin fallback a authenticated y sin filtrar secretos
        def run_missing_admin():
            tok_d1 = self._make_token(self.uid_dueno_1, "d1@test.com")
            tok_soc = self._make_token(self.uid_socio_1, "soc@test.com")

            # Autorización sigue ejecutándose antes de comprobar service_role
            r_soc = self.client.get("/api/integraciones/mercadopago", headers={"Authorization": f"Bearer {tok_soc}"})
            self.assertEqual(r_soc.status_code, 403)

            r_503 = self.client.get(
                "/api/integraciones/mercadopago",
                headers={"Authorization": f"Bearer {tok_d1}", "X-Gimnasio-Id": "10"},
            )
            self.assertEqual(r_503.status_code, 503)
            self.assertIn("SUPABASE_SERVICE_ROLE_KEY", r_503.text)
            self.assertNotIn("permission denied", r_503.text.lower())

        self._with_env(run_missing_admin, oauth_configured=False, service_role_configured=False)

    def test_06_diagnostico_variables_oauth_faltantes_e_invalidas_sin_exponer_secretos(self):
        # 1) Todas faltantes: GET estado responde 200 e informa las 4 variables faltantes
        def run_missing():
            tok_d1 = self._make_token(self.uid_dueno_1, "d1@test.com")
            r_status = self.client.get(
                "/api/integraciones/mercadopago",
                headers={"Authorization": f"Bearer {tok_d1}", "X-Gimnasio-Id": "10"},
            )
            self.assertEqual(r_status.status_code, 200)
            body = r_status.json()
            self.assertFalse(body["oauth_configurado"])
            self.assertEqual(
                body["variables_faltantes"],
                [
                    "MERCADOPAGO_CLIENT_ID",
                    "MERCADOPAGO_CLIENT_SECRET",
                    "MERCADOPAGO_REDIRECT_URI",
                    "MERCADOPAGO_TOKEN_ENCRYPTION_KEY",
                ],
            )
            self.assertEqual(body["variables_invalidas"], [])
            self.assertIn("MERCADOPAGO_CLIENT_ID", body["configuracion_mensaje"])

            r_conn = self.client.post(
                "/api/integraciones/mercadopago/conectar",
                headers={"Authorization": f"Bearer {tok_d1}", "X-Gimnasio-Id": "10"},
            )
            self.assertEqual(r_conn.status_code, 503)
            for var_name in (
                "MERCADOPAGO_CLIENT_ID",
                "MERCADOPAGO_CLIENT_SECRET",
                "MERCADOPAGO_REDIRECT_URI",
                "MERCADOPAGO_TOKEN_ENCRYPTION_KEY",
            ):
                self.assertIn(var_name, r_conn.text)

        self._with_env(run_missing, oauth_configured=False, service_role_configured=True)

        # 2) Valores con formato inválido: se informa qué variables fallan SIN exponer sus valores
        bad_secret_key = "NO_SOY_UNA_CLAVE_FERNET_SECRETA_12345"
        bad_client_secret = "SECRETO_MP_NO_EXPONER_98765"
        bad_redirect = "ruta-invalida-sin-esquema"

        def run_invalid():
            tok_d1 = self._make_token(self.uid_dueno_1, "d1@test.com")
            r_status = self.client.get(
                "/api/integraciones/mercadopago",
                headers={"Authorization": f"Bearer {tok_d1}", "X-Gimnasio-Id": "10"},
            )
            self.assertEqual(r_status.status_code, 200)
            body = r_status.json()
            self.assertFalse(body["oauth_configurado"])
            self.assertEqual(body["variables_faltantes"], [])
            self.assertEqual(
                body["variables_invalidas"],
                ["MERCADOPAGO_REDIRECT_URI", "MERCADOPAGO_TOKEN_ENCRYPTION_KEY"],
            )
            self.assertNotIn(bad_secret_key, r_status.text)
            self.assertNotIn(bad_client_secret, r_status.text)
            self.assertNotIn(bad_redirect, r_status.text)

            r_conn = self.client.post(
                "/api/integraciones/mercadopago/conectar",
                headers={"Authorization": f"Bearer {tok_d1}", "X-Gimnasio-Id": "10"},
            )
            self.assertEqual(r_conn.status_code, 503)
            self.assertIn("MERCADOPAGO_TOKEN_ENCRYPTION_KEY", r_conn.text)
            self.assertIn("MERCADOPAGO_REDIRECT_URI", r_conn.text)
            self.assertNotIn(bad_secret_key, r_conn.text)
            self.assertNotIn(bad_client_secret, r_conn.text)

        self._with_env(
            run_invalid,
            oauth_configured=True,
            service_role_configured=True,
            client_secret_override=bad_client_secret,
            redirect_uri_override=bad_redirect,
            enc_key_override=bad_secret_key,
        )

    def test_07_get_supabase_admin_client_reporta_falta_o_formato_invalido_sin_secretos(self):
        with patch("app.config.supabase_admin", None), patch("app.config.SUPABASE_SERVICE_ROLE_KEY_INVALID", False):
            with self.assertRaises(RuntimeError) as ctx_missing:
                app_config.get_supabase_admin_client()
            self.assertIn("SUPABASE_SERVICE_ROLE_KEY", str(ctx_missing.exception))

        with patch("app.config.supabase_admin", None), patch("app.config.SUPABASE_SERVICE_ROLE_KEY_INVALID", True):
            with self.assertRaises(RuntimeError) as ctx_invalid:
                app_config.get_supabase_admin_client()
            self.assertIn("SUPABASE_SERVICE_ROLE_KEY", str(ctx_invalid.exception))
            self.assertIn("válida", str(ctx_invalid.exception))

    def test_08_simulacion_vinculacion_presentacion_con_cuenta_prueba(self):
        def run_demo():
            tok_d1 = self._make_token(self.uid_dueno_1, "d1@test.com")
            headers_d1 = {"Authorization": f"Bearer {tok_d1}", "X-Gimnasio-Id": "10"}

            # 1. GET estado en modo presentación sin claves de producción ni service_role
            r_status = self.client.get("/api/integraciones/mercadopago", headers=headers_d1)
            self.assertEqual(r_status.status_code, 200, r_status.text)
            self.assertEqual(r_status.json()["modo"], "simulacion_prueba")
            self.assertTrue(r_status.json()["simulacion_disponible"])
            self.assertFalse(r_status.json()["conectado"])

            # 2. POST /conectar devuelve state firmado y cuenta de prueba (sin redirigir a MP producción)
            r_init = self.client.post("/api/integraciones/mercadopago/conectar", headers=headers_d1)
            self.assertEqual(r_init.status_code, 200, r_init.text)
            body_init = r_init.json()
            self.assertEqual(body_init["modo"], "simulacion_prueba")
            self.assertEqual(body_init["cuenta_prueba"]["external_user_id"], "2099887766")
            state_val = body_init["state"]

            # 3. POST /callback-simulado completa vinculación con cifrado y sin exponer tokens
            r_cb = self.client.post(
                "/api/integraciones/mercadopago/callback-simulado",
                headers=headers_d1,
                json={"state": state_val, "autorizado": True},
            )
            self.assertEqual(r_cb.status_code, 200, r_cb.text)
            self.assertTrue(r_cb.json()["conectado"])
            self.assertEqual(r_cb.json()["cuenta"]["external_user_id"], "2099887766")
            self.assertNotIn("access_token", r_cb.text)
            self.assertNotIn("refresh_token", r_cb.text)

            # 4. Replay del mismo state rechazado con 400
            self.assertEqual(
                self.client.post(
                    "/api/integraciones/mercadopago/callback-simulado",
                    headers=headers_d1,
                    json={"state": state_val, "autorizado": True},
                ).status_code,
                400,
            )

            # 5. Desconectar vuelve a no_conectado sin tocar pagos
            r_disc = self.client.post("/api/integraciones/mercadopago/desconectar", headers=headers_d1)
            self.assertEqual(r_disc.status_code, 200)
            self.assertFalse(r_disc.json()["conectado"])
            self.assertEqual(len(self.db.tables["pago"]), 1)

        self._with_env(run_demo, oauth_configured=False, service_role_configured=False, demo_mode=True)


if __name__ == "__main__":
    unittest.main()
