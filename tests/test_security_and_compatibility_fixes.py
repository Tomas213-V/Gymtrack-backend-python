import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Dict, Any, List, Optional
from unittest.mock import patch, MagicMock, AsyncMock

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

import main as main_module
from app.config import (
    resolve_supabase_anon_key,
    is_valid_public_supabase_key,
    create_supabase_client_for_token,
)
from app.core.security import verify_supabase_access_token
from app.services.utils import map_postgrest_error
from tests.test_integral_asistencias_rutinas_dashboards import FakeIntegralSupabaseClient


class StrictAuthMeQueryBuilder:
    """
    Mock estricto de PostgREST para `GET /api/auth/me` y `PUT /api/auth/me`.
    - Valida que `membresia_socio` NUNCA embeba `gimnasio(...)` directamente (lanza PGRST200).
    - Puede inyectar un `APIError` controlado para verificar que no se ocultan errores.
    """

    def __init__(
        self,
        table_name: str,
        fixtures: Dict[str, Any],
        select_log: List[tuple[str, str]],
        inject_error_on_table: Optional[Dict[str, APIError]] = None,
    ):
        self.table_name = table_name
        self.fixtures = fixtures
        self.select_log = select_log
        self.inject_error_on_table = inject_error_on_table or {}
        self._select_clause = ""
        self._eq: Dict[str, Any] = {}
        self._update: Optional[Dict[str, Any]] = None
        self._insert: Optional[List[Dict[str, Any]]] = None
        self._single = False

    def select(self, clause: str = "*", **_kwargs):
        self._select_clause = clause
        self.select_log.append((self.table_name, clause))
        return self

    def eq(self, col: str, val: Any):
        self._eq[col] = val
        return self

    def in_(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def maybe_single(self):
        self._single = True
        return self

    def update(self, payload: Dict[str, Any]):
        self._update = payload
        return self

    def insert(self, rows: List[Dict[str, Any]]):
        self._insert = rows
        return self

    def execute(self):
        if self.table_name in self.inject_error_on_table:
            raise self.inject_error_on_table[self.table_name]

        # Validación estricta de FK PostgREST: membresia_socio NO tiene FK directa a gimnasio
        if self.table_name == "membresia_socio" and self._select_clause:
            # Detectar 'gimnasio(' a nivel raíz (sin estar dentro de 'socio_gimnasio')
            depth = 0
            buf = []
            top_tokens = []
            for ch in self._select_clause:
                if ch == "(":
                    depth += 1
                    buf.append(ch)
                elif ch == ")":
                    depth -= 1
                    buf.append(ch)
                elif ch == "," and depth == 0:
                    top_tokens.append("".join(buf).strip())
                    buf = []
                else:
                    buf.append(ch)
            if buf:
                top_tokens.append("".join(buf).strip())
            for tok in top_tokens:
                if tok.startswith("gimnasio(") or tok.startswith("gimnasio!"):
                    raise APIError(
                        {
                            "code": "PGRST200",
                            "message": "Could not find a relationship between 'membresia_socio' and 'gimnasio' in the schema cache",
                        }
                    )

        rows = copy.deepcopy(self.fixtures.get(self.table_name, []))
        if self._insert is not None:
            return SimpleNamespace(data=self._insert)
        if self._update is not None:
            updated = []
            for r in rows:
                if all(r.get(k) == v for k, v in self._eq.items()):
                    updated.append({**r, **self._update})
            return SimpleNamespace(data=updated)

        matched = []
        for r in rows:
            ok = True
            for k, v in self._eq.items():
                if "." in k:
                    p, c = k.split(".", 1)
                    if isinstance(r.get(p), dict) and r[p].get(c) != v:
                        ok = False
                        break
                elif r.get(k) != v:
                    ok = False
                    break
            if ok:
                matched.append(r)

        if self._single:
            return SimpleNamespace(data=matched[0] if matched else None)
        return SimpleNamespace(data=matched)


class StrictAuthMeClient:
    def __init__(
        self,
        fixtures: Dict[str, Any],
        inject_error_on_table: Optional[Dict[str, APIError]] = None,
    ):
        self.fixtures = fixtures
        self.select_log: List[tuple[str, str]] = []
        self.rpc_calls: List[tuple[str, Dict[str, Any]]] = []
        self.inject_error_on_table = inject_error_on_table or {}

    def from_(self, table_name: str):
        return StrictAuthMeQueryBuilder(
            table_name,
            self.fixtures,
            self.select_log,
            inject_error_on_table=self.inject_error_on_table,
        )

    def rpc(self, fn_name: str, params: Optional[Dict[str, Any]] = None):
        self.rpc_calls.append((fn_name, copy.deepcopy(params or {})))
        return SimpleNamespace(
            execute=lambda: SimpleNamespace(
                data={"vinculado": True, "perfil_tipo": (params or {}).get("p_perfil_tipo"), "id": 70}
            )
        )


class TestSecurityAndCompatibilityFixes(unittest.TestCase):
    """
    Pruebas obligatorias de las correcciones finales de seguridad y compatibilidad:
      1. Registro seguro y vinculación de identidades (sin email_confirm=True ni account takeover).
      2. Rechazo estricto de claves secretas/service_role en SUPABASE_ANON_KEY y variables VITE_*.
      3. GET /api/auth/me con join compuesto válido (membresia_socio -> socio_gimnasio -> gimnasio) sin ocultar errores.
      4. PUT /api/auth/me actualizando contraseña y solicitando cambio de email con Bearer token (sin service_role ni sesión GoTrue).
      5. Permisos de rol y sucursal en POST /api/asistencias/salida.
      6. Mapeo centralizado de errores PostgREST APIError (23505, 42501, 23503, 23514, P0001, PGRST200).
    """

    @classmethod
    def setUpClass(cls):
        cls.ec_private_key = ec.generate_private_key(ec.SECP256R1())
        cls.ec_public_key = cls.ec_private_key.public_key()
        cls.issuer = "https://qdlkljSb.supabase.co/auth/v1"
        cls.audience = "authenticated"
        cls.uid_socio = "44444444-4444-4444-8444-000000000001"
        cls.uid_dueno = "44444444-4444-4444-8444-000000000002"
        cls.client = TestClient(main_module.app)

    def _make_token(
        self,
        sub: str,
        email: Optional[str] = "user@gymtrack.test",
        email_confirmed: bool = True,
        extra_user_metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        now = datetime.now(timezone.utc)
        payload: Dict[str, Any] = {
            "sub": sub,
            "role": "authenticated",
            "aud": self.audience,
            "iss": self.issuer,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
            "app_metadata": {"provider": "email"},
            "user_metadata": extra_user_metadata or {},
        }
        if email is not None:
            payload["email"] = email
        if email_confirmed:
            payload["email_confirmed_at"] = now.isoformat()
        return jwt.encode(payload, self.ec_private_key, algorithm="ES256", headers={"kid": "test-kid-sec"})

    def _fake_verify(self, token: str):
        return verify_supabase_access_token(
            token,
            verification_key=self.ec_public_key,
            expected_issuer=self.issuer,
            expected_audience=self.audience,
        )

    # 1. Registro de socio NO usa service_role, NO auto-confirma email y NO toma control de ficha presencial
    def test_01_register_socio_sin_autovinculacion_ni_email_confirm(self):
        fake_anon = MagicMock()
        # Caso A: Supabase Auth requiere confirmación de correo (session=None)
        fake_anon.auth.sign_up.return_value = SimpleNamespace(
            user=SimpleNamespace(id=self.uid_socio, email="victima@gymtrack.com", email_confirmed_at=None),
            session=None,
        )

        with patch("app.routers.auth.supabase_anon", fake_anon):
            res = self.client.post(
                "/api/auth/register-socio",
                json={
                    "nombre": "Atacante",
                    "apellido": "Test",
                    "email": "victima@gymtrack.com",
                    "password": "Password123!",
                },
            )
            self.assertEqual(res.status_code, 201)
            body = res.json()
            self.assertTrue(body["requiere_confirmacion_email"])
            self.assertNotIn("token", body)
            fake_anon.auth.sign_up.assert_called_once()

        # Caso B: incluso si sign_up devolviera access_token pero SIN email_confirmed_at, NO crea ni vincula ficha
        fake_anon_unconfirmed = MagicMock()
        fake_anon_unconfirmed.auth.sign_up.return_value = SimpleNamespace(
            user=SimpleNamespace(id=self.uid_socio, email="victima@gymtrack.com", email_confirmed_at=None),
            session=SimpleNamespace(access_token="tok-unconfirmed", refresh_token="ref-1"),
        )
        strict_db = StrictAuthMeClient({})
        with patch("app.routers.auth.supabase_anon", fake_anon_unconfirmed), patch(
            "app.routers.auth.create_supabase_client_for_token", return_value=strict_db
        ):
            res_b = self.client.post(
                "/api/auth/register-socio",
                json={
                    "nombre": "Atacante",
                    "apellido": "Test",
                    "email": "victima@gymtrack.com",
                    "password": "Password123!",
                },
            )
            self.assertEqual(res_b.status_code, 201)
            self.assertTrue(res_b.json()["requiere_confirmacion_email"])
            # Jamás debe invocar rpc_vincular_identidad_perfil desde register-socio
            self.assertEqual(strict_db.rpc_calls, [])

        # Caso C: si ya existe ficha presencial (conflicto 23505), devuelve 409 y jamás vincula automáticamente
        fake_anon_confirmed = MagicMock()
        fake_anon_confirmed.auth.sign_up.return_value = SimpleNamespace(
            user=SimpleNamespace(
                id=self.uid_socio,
                email="victima@gymtrack.com",
                email_confirmed_at=datetime.now(timezone.utc).isoformat(),
            ),
            session=SimpleNamespace(access_token="tok-confirmed", refresh_token="ref-2"),
        )
        dup_db = StrictAuthMeClient(
            {},
            inject_error_on_table={
                "socio": APIError({"code": "23505", "message": "duplicate key value violates unique constraint uq_socio_email"})
            },
        )
        with patch("app.routers.auth.supabase_anon", fake_anon_confirmed), patch(
            "app.routers.auth.create_supabase_client_for_token", return_value=dup_db
        ):
            res_c = self.client.post(
                "/api/auth/register-socio",
                json={
                    "nombre": "Socio",
                    "apellido": "Presencial",
                    "email": "victima@gymtrack.com",
                    "password": "Password123!",
                },
            )
            self.assertEqual(res_c.status_code, 409)
            self.assertEqual(dup_db.rpc_calls, [])

    # 2. Registro de dueño usa sign_up público y respeta confirmación de email
    def test_02_register_dueno_usa_signup_publico(self):
        fake_anon = MagicMock()
        fake_anon.auth.sign_up.return_value = SimpleNamespace(
            user=SimpleNamespace(id=self.uid_dueno, email="dueno@gymtrack.com", email_confirmed_at=None),
            session=None,
        )
        with patch("app.routers.auth.supabase_anon", fake_anon):
            res = self.client.post(
                "/api/auth/register",
                json={
                    "nombre": "Martín",
                    "apellido": "Rivas",
                    "email": "dueno@gymtrack.com",
                    "password": "Password123!",
                    "nombreGimnasio": "Gym Central",
                },
            )
            self.assertEqual(res.status_code, 201)
            self.assertTrue(res.json()["requiere_confirmacion_email"])
            fake_anon.auth.sign_up.assert_called_once()

    # 3. Vinculación de perfil exige correo verificado real (ignora user_metadata manipulado)
    def test_03_vincular_perfil_rechaza_correo_no_verificado_y_user_metadata_falso(self):
        # Token SIN email_confirmed_at real pero con user_metadata falsificado por el cliente
        token_spoof = self._make_token(
            self.uid_socio,
            email="victima@gym.test",
            email_confirmed=False,
            extra_user_metadata={
                "email": "otra@gym.test",
                "email_verified": True,
                "email_confirmed_at": "2026-01-01T00:00:00Z",
            },
        )
        strict_db = StrictAuthMeClient({})
        with patch("app.core.dependencies.verify_supabase_access_token", side_effect=self._fake_verify), patch(
            "app.core.dependencies.create_supabase_client_for_token", return_value=strict_db
        ):
            res_forb = self.client.post(
                "/api/auth/vincular-perfil",
                json={"perfil_tipo": "socio"},
                headers={"Authorization": f"Bearer {token_spoof}"},
            )
            self.assertEqual(res_forb.status_code, 403)
            self.assertEqual(strict_db.rpc_calls, [])

            # Con email_confirmed_at top-level legítimo sí permite invocar rpc_vincular_identidad_perfil
            token_ok = self._make_token(self.uid_socio, email="victima@gym.test", email_confirmed=True)
            res_ok = self.client.post(
                "/api/auth/vincular-perfil",
                json={"perfil_tipo": "socio"},
                headers={"Authorization": f"Bearer {token_ok}"},
            )
            self.assertEqual(res_ok.status_code, 200)
            self.assertEqual(strict_db.rpc_calls[-1], ("rpc_vincular_identidad_perfil", {"p_perfil_tipo": "socio"}))

    # 3b. verify_supabase_access_token NO acepta payload['email_verified']=True sin email_confirmed_at
    def test_03b_verify_token_rechaza_claim_email_verified_sin_email_confirmed_at(self):
        now = datetime.now(timezone.utc)
        base_claims = {
            "sub": self.uid_socio,
            "email": "socio@gymtrack.com",
            "role": "authenticated",
            "aud": self.audience,
            "iss": self.issuer,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
            "app_metadata": {"provider": "email"},
            "user_metadata": {
                "email": "otro@gymtrack.com",
                "email_verified": True,
                "email_confirmed_at": now.isoformat(),
            },
        }

        # Caso 1: payload trae email_verified=True pero NO email_confirmed_at -> email_verified DEBE ser False
        tok_only_bool = jwt.encode(
            {**base_claims, "email_verified": True},
            self.ec_private_key,
            algorithm="ES256",
            headers={"kid": "test-kid-sec"},
        )
        ident_bool = verify_supabase_access_token(
            tok_only_bool,
            verification_key=self.ec_public_key,
            expected_issuer=self.issuer,
            expected_audience=self.audience,
        )
        self.assertIsNone(ident_bool["email_confirmed_at"])
        self.assertFalse(ident_bool["email_verified"])

        # Además /api/auth/vincular-perfil debe rechazar ese token con 403
        strict_db = StrictAuthMeClient({})
        with patch("app.core.dependencies.verify_supabase_access_token", side_effect=self._fake_verify), patch(
            "app.core.dependencies.create_supabase_client_for_token", return_value=strict_db
        ):
            r_forb = self.client.post(
                "/api/auth/vincular-perfil",
                json={"perfil_tipo": "socio"},
                headers={"Authorization": f"Bearer {tok_only_bool}"},
            )
            self.assertEqual(r_forb.status_code, 403)
            self.assertEqual(strict_db.rpc_calls, [])

        # Caso 2: email_confirmed_at vacío/espacios -> email_verified DEBE ser False
        tok_blank = jwt.encode(
            {**base_claims, "email_confirmed_at": "   "},
            self.ec_private_key,
            algorithm="ES256",
            headers={"kid": "test-kid-sec"},
        )
        ident_blank = verify_supabase_access_token(
            tok_blank,
            verification_key=self.ec_public_key,
            expected_issuer=self.issuer,
            expected_audience=self.audience,
        )
        self.assertIsNone(ident_blank["email_confirmed_at"])
        self.assertFalse(ident_blank["email_verified"])

        # Caso 3: fuente confiable auth_user de Supabase Auth con email_confirmed_at -> email_verified True
        ident_auth_user = verify_supabase_access_token(
            tok_only_bool,
            verification_key=self.ec_public_key,
            expected_issuer=self.issuer,
            expected_audience=self.audience,
            auth_user=SimpleNamespace(email="socio@gymtrack.com", email_confirmed_at=now.isoformat()),
        )
        self.assertEqual(ident_auth_user["email_confirmed_at"], now.isoformat())
        self.assertTrue(ident_auth_user["email_verified"])

    # 4. Rechazo explícito de claves secretas/service_role en SUPABASE_ANON_KEY y variables VITE_*
    def test_04_rechazo_claves_secretas_en_supabase_anon_key_sin_exponer_secreto(self):
        fake_sb_secret = "sb_secret_NO_DEBE_APARECER_EN_LOGS_999999"
        fake_jwt_service = jwt.encode(
            {"role": "service_role", "iss": "supabase"},
            "dummy-secret-for-test-only-32bytes!",
            algorithm="HS256",
        )
        fake_jwt_anon = jwt.encode(
            {"role": "anon", "iss": "supabase"},
            "dummy-secret-for-test-only-32bytes!",
            algorithm="HS256",
        )

        # Clave sb_secret_* en SUPABASE_ANON_KEY -> RuntimeError sin imprimir valor
        with self.assertRaises(RuntimeError) as ctx1:
            resolve_supabase_anon_key({"SUPABASE_ANON_KEY": fake_sb_secret}, raise_on_secret=True)
        self.assertIn("SUPABASE_ANON_KEY", str(ctx1.exception))
        self.assertNotIn(fake_sb_secret, str(ctx1.exception))

        # JWT service_role en VITE_SUPABASE_PUBLISHABLE_KEY -> RuntimeError sin imprimir valor
        with self.assertRaises(RuntimeError) as ctx2:
            resolve_supabase_anon_key({"VITE_SUPABASE_PUBLISHABLE_KEY": fake_jwt_service}, raise_on_secret=True)
        self.assertIn("VITE_SUPABASE_PUBLISHABLE_KEY", str(ctx2.exception))
        self.assertNotIn(fake_jwt_service, str(ctx2.exception))

        # Claves públicas válidas sí se aceptan
        key_pub, viols1 = resolve_supabase_anon_key({"SUPABASE_ANON_KEY": "sb_publishable_demo_key"}, raise_on_secret=True)
        self.assertEqual(key_pub, "sb_publishable_demo_key")
        self.assertEqual(viols1, [])
        self.assertTrue(is_valid_public_supabase_key("sb_publishable_demo_key"))
        self.assertTrue(is_valid_public_supabase_key(fake_jwt_anon))
        self.assertFalse(is_valid_public_supabase_key(fake_sb_secret))
        self.assertFalse(is_valid_public_supabase_key(fake_jwt_service))

        # create_supabase_client_for_token rechaza token service_role y configuración inválida
        with self.assertRaises(PermissionError):
            create_supabase_client_for_token(fake_jwt_service)

        with patch("app.config.SUPABASE_PUBLIC_KEY_SECRET_VIOLATIONS", ["VITE_SUPABASE_PUBLISHABLE_KEY"]):
            with self.assertRaises(RuntimeError):
                create_supabase_client_for_token("valid.user.token")

    # 5. GET /api/auth/me usa join compuesto válido y NO silencia errores de PostgREST
    def test_05_get_auth_me_join_valido_y_no_oculta_errores(self):
        token_socio = self._make_token(self.uid_socio, email="socio@gym.test", email_confirmed=True)
        base_fixtures = {
            "dueno": [],
            "administrador": [],
            "empleado": [],
            "sucursal": [],
            "socio": [
                {
                    "id_socio": 70,
                    "auth_user_id": self.uid_socio,
                    "dni": "40111002",
                    "telefono": "1155550000",
                    "fecha_alta": "2026-01-10",
                    "estado": True,
                    "nombre": "Lucía",
                    "apellido": "Pereyra",
                    "email": "socio@gym.test",
                    "socio_gimnasio": [
                        {
                            "id_socio_gimnasio": 701,
                            "id_socio": 70,
                            "id_gimnasio": 10,
                            "estado": True,
                            "gimnasio": {"id_gimnasio": 10, "nombre": "Norte Fit", "estado": True},
                            "membresia_socio": [],
                        }
                    ],
                }
            ],
            "membresia_socio": [
                {
                    "id_membresia_socio": 801,
                    "id_socio_gimnasio": 701,
                    "id_gimnasio": 10,
                    "id_membresia_gimnasio": 501,
                    "precio": 30000.0,
                    "estado": "activa",
                    "socio_gimnasio": {
                        "id_socio_gimnasio": 701,
                        "id_socio": 70,
                        "id_gimnasio": 10,
                        "estado": True,
                        "gimnasio": {"id_gimnasio": 10, "nombre": "Norte Fit", "estado": True},
                    },
                    "membresia_gimnasio": {"id_membresia_gimnasio": 501, "nombre": "Plan Full"},
                }
            ],
        }

        # 5a. Con datos reales: el join compuesto pasa la validación estricta (sin gimnasio directo en membresia_socio)
        db_ok = StrictAuthMeClient(base_fixtures)
        with patch("app.core.dependencies.verify_supabase_access_token", side_effect=self._fake_verify), patch(
            "app.core.dependencies.create_supabase_client_for_token", return_value=db_ok
        ):
            r_ok = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token_socio}"})
            self.assertEqual(r_ok.status_code, 200)
            membs = r_ok.json()["membresias"]
            self.assertEqual(len(membs), 1)
            self.assertEqual(membs[0]["socio_gimnasio"]["gimnasio"]["nombre"], "Norte Fit")

        # 5b. Sin membresías: devuelve 200 con lista vacía real
        fixtures_vacias = {**base_fixtures, "membresia_socio": []}
        db_empty = StrictAuthMeClient(fixtures_vacias)
        with patch("app.core.dependencies.verify_supabase_access_token", side_effect=self._fake_verify), patch(
            "app.core.dependencies.create_supabase_client_for_token", return_value=db_empty
        ):
            r_empty = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token_socio}"})
            self.assertEqual(r_empty.status_code, 200)
            self.assertEqual(r_empty.json()["membresias"], [])

        # 5c. Error PGRST200 en PostgREST: NO se silencia devolviendo [], propaga 502 Bad Gateway
        db_err = StrictAuthMeClient(
            base_fixtures,
            inject_error_on_table={
                "membresia_socio": APIError({"code": "PGRST200", "message": "Relationship not found"})
            },
        )
        with patch("app.core.dependencies.verify_supabase_access_token", side_effect=self._fake_verify), patch(
            "app.core.dependencies.create_supabase_client_for_token", return_value=db_err
        ):
            r_err = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token_socio}"})
            self.assertEqual(r_err.status_code, 502)

    # 6. PUT /api/auth/me actualiza contraseña y solicita cambio de email con Bearer token (sin service_role ni GoTrue session)
    def test_06_put_auth_me_password_email_con_bearer_y_sesion_expirada(self):
        token_dueno = self._make_token(self.uid_dueno, email="dueno@gym.test", email_confirmed=True)
        fixtures = {
            "dueno": [
                {
                    "id_dueno": 1,
                    "auth_user_id": self.uid_dueno,
                    "nombre": "Martín",
                    "apellido": "Rivas",
                    "email": "dueno@gym.test",
                }
            ],
            "gimnasio": [{"id_gimnasio": 10, "nombre": "Norte Fit", "estado": True, "id_dueno": 1}],
            "sucursal": [],
            "administrador": [],
            "empleado": [],
            "socio": [],
        }

        # 6a. Cambio de contraseña + solicitud de cambio de correo (respetando confirmación de email)
        db_client = StrictAuthMeClient(fixtures)
        fake_anon = MagicMock()
        fake_anon.auth.sign_in_with_password.side_effect = [
            SimpleNamespace(session=SimpleNamespace(access_token=token_dueno, refresh_token="r0")),
            SimpleNamespace(session=SimpleNamespace(access_token="new-access-token", refresh_token="new-refresh")),
        ]
        captured_calls: List[Dict[str, Any]] = []

        async def _fake_put(self_http, url, headers=None, json=None, **_kwargs):
            captured_calls.append({"url": url, "headers": dict(headers or {}), "json": dict(json or {})})
            if "password" in (json or {}):
                return SimpleNamespace(status_code=200, content=b'{"id":"ok"}', json=lambda: {"id": self.uid_dueno})
            # GoTrue devuelve new_email pendiente de confirmación real (NO cambia email todavía)
            return SimpleNamespace(
                status_code=200,
                content=b'{"email":"dueno@gym.test","new_email":"nuevo@gym.test"}',
                json=lambda: {"email": "dueno@gym.test", "new_email": "nuevo@gym.test"},
            )

        with patch("app.core.dependencies.verify_supabase_access_token", side_effect=self._fake_verify), patch(
            "app.core.dependencies.create_supabase_client_for_token", return_value=db_client
        ), patch("app.routers.auth.supabase_anon", fake_anon), patch(
            "app.routers.auth.validate_supabase_configuration", return_value=None
        ), patch("app.routers.auth.SUPABASE_URL", "https://demo.supabase.co"), patch(
            "app.routers.auth.SUPABASE_ANON_KEY", "sb_publishable_demo"
        ), patch("httpx.AsyncClient.put", new=_fake_put):
            res = self.client.put(
                "/api/auth/me",
                json={
                    "nombre": "Martín Editado",
                    "email": "nuevo@gym.test",
                    "contrasena_actual": "OldPass123!",
                    "nueva_contrasena": "NewPass456!",
                },
                headers={"Authorization": f"Bearer {token_dueno}"},
            )
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertTrue(body["cambio_email_pendiente_confirmacion"])
            self.assertEqual(body["email_pendiente"], "nuevo@gym.test")
            self.assertEqual(body["token"], "new-access-token")
            # No se sobrescribió public.dueno.email hasta que se confirme el nuevo correo
            self.assertEqual(body["usuario"]["email"], "dueno@gym.test")
            self.assertEqual(body["usuario"]["nombre"], "Martín Editado")
            # Verificamos que ambas llamadas fueron a /auth/v1/user con Bearer token y apikey pública
            self.assertEqual(len(captured_calls), 2)
            self.assertEqual(captured_calls[0]["url"], "https://demo.supabase.co/auth/v1/user")
            self.assertEqual(captured_calls[0]["headers"]["Authorization"], f"Bearer {token_dueno}")
            self.assertEqual(captured_calls[0]["headers"]["apikey"], "sb_publishable_demo")
            self.assertEqual(captured_calls[1]["json"], {"email": "nuevo@gym.test"})

        # 6b. Cuando GoTrue responde 401 (sesión expirada/revocada) -> 401 Unauthorized
        async def _fake_put_401(self_http, url, headers=None, json=None, **_kwargs):
            return SimpleNamespace(
                status_code=401,
                content=b'{"msg":"invalid JWT: token is expired"}',
                json=lambda: {"msg": "invalid JWT: token is expired"},
            )

        with patch("app.core.dependencies.verify_supabase_access_token", side_effect=self._fake_verify), patch(
            "app.core.dependencies.create_supabase_client_for_token", return_value=StrictAuthMeClient(fixtures)
        ), patch("app.routers.auth.validate_supabase_configuration", return_value=None), patch(
            "app.routers.auth.SUPABASE_URL", "https://demo.supabase.co"
        ), patch("app.routers.auth.SUPABASE_ANON_KEY", "sb_publishable_demo"), patch(
            "httpx.AsyncClient.put", new=_fake_put_401
        ):
            res_exp = self.client.put(
                "/api/auth/me",
                json={"email": "otro@gym.test"},
                headers={"Authorization": f"Bearer {token_dueno}"},
            )
            self.assertEqual(res_exp.status_code, 401)

    # 7. Permisos de rol y sucursal en POST /api/asistencias/salida
    def test_07_post_asistencias_salida_valida_rol_y_sucursal(self):
        uid_dueno_1 = "44444444-4444-4444-8444-000000000011"
        uid_recep_101 = "44444444-4444-4444-8444-000000000012"
        uid_trainer_101 = "44444444-4444-4444-8444-000000000013"
        state = {
            "dueno": [
                {"id_dueno": 1, "auth_user_id": uid_dueno_1, "nombre": "Martín", "apellido": "Rivas", "email": "d1@gymtrack.com"},
            ],
            "gimnasio": [
                {"id_gimnasio": 10, "nombre": "Norte Fit", "estado": True, "id_dueno": 1},
            ],
            "sucursal": [
                {"id_sucursal": 101, "direccion": "Av. Norte 1", "descripcion": "Sede A", "estado": True, "id_gimnasio": 10},
                {"id_sucursal": 102, "direccion": "Av. Norte 2", "descripcion": "Sede B", "estado": True, "id_gimnasio": 10},
            ],
            "administrador": [],
            "administrador_gimnasio": [],
            "empleado": [
                {"id_empleado": 30, "auth_user_id": uid_recep_101, "nombre": "Sofía", "apellido": "Recep", "estado": True},
                {"id_empleado": 31, "auth_user_id": uid_trainer_101, "nombre": "Julián", "apellido": "Trainer", "estado": True},
            ],
            "empleado_sucursal": [
                {"id_empleado_sucursal": 301, "rol": "recepcionista", "id_empleado": 30, "id_sucursal": 101, "estado": True},
                {"id_empleado_sucursal": 302, "rol": "entrenador", "id_empleado": 31, "id_sucursal": 101, "estado": True},
            ],
            "socio": [
                {"id_socio": 70, "auth_user_id": self.uid_socio, "nombre": "Lucía", "apellido": "Pereyra", "estado": True},
            ],
            "socio_gimnasio": [
                {"id_socio_gimnasio": 701, "id_socio": 70, "id_gimnasio": 10, "estado": True},
            ],
            "asistencia_socio": [
                {
                    "id_asistencia_socio": 9050,
                    "id_socio": 70,
                    "id_sucursal": 102,
                    "hora_ingreso": datetime.now(timezone.utc).isoformat(),
                    "hora_salida": None,
                    "estado": "presente",
                    "anulada_por": None,
                    "anulada_en": None,
                    "motivo_anulacion": None,
                }
            ],
        }
        fake_db = FakeIntegralSupabaseClient(state)

        def _verify_int(token: str):
            claims = self._fake_verify(token)
            fake_db.actor_uid = claims["sub"]
            return claims

        token_trainer = self._make_token(uid_trainer_101, "trainer@gymtrack.com")
        token_recep101 = self._make_token(uid_recep_101, "recep@gymtrack.com")
        token_dueno1 = self._make_token(uid_dueno_1, "d1@gymtrack.com")

        with patch("app.core.dependencies.verify_supabase_access_token", side_effect=_verify_int), patch(
            "app.core.dependencies.create_supabase_client_for_token", return_value=fake_db
        ):
            # Entrenador NO puede cerrar salida de un socio -> 403
            r_tr = self.client.post(
                "/api/asistencias/salida",
                json={"id_socio": 70},
                headers={"Authorization": f"Bearer {token_trainer}"},
            )
            self.assertEqual(r_tr.status_code, 403)

            # Recepcionista de Sucursal 101 NO puede cerrar asistencia abierta en Sucursal 102 -> 403
            r_other_suc = self.client.post(
                "/api/asistencias/salida",
                json={"id_socio": 70},
                headers={"Authorization": f"Bearer {token_recep101}"},
            )
            self.assertEqual(r_other_suc.status_code, 403)

            # Dueño 1 (alcance organizacional en Gym 10) SÍ puede cerrar la asistencia de Sucursal 102 -> 200
            r_ok = self.client.post(
                "/api/asistencias/salida",
                json={"id_socio": 70},
                headers={"Authorization": f"Bearer {token_dueno1}"},
            )
            self.assertEqual(r_ok.status_code, 200)
            self.assertEqual(r_ok.json()["asistencia"]["estado"], "finalizada")

    # 8. Mapeo centralizado de errores PostgREST APIError (handler global + mapper)
    def test_08_mapeo_centralizado_errores_postgrest_apierror(self):
        # 8a. Verificación directa de códigos SQLSTATE/PostgREST
        self.assertEqual(map_postgrest_error(APIError({"code": "23505", "message": "duplicate key"}))[0], 409)
        self.assertEqual(map_postgrest_error(APIError({"code": "42501", "message": "new row violates rls"}))[0], 403)
        self.assertEqual(map_postgrest_error(APIError({"code": "23503", "message": "insert violates fk"}))[0], 400)
        self.assertEqual(
            map_postgrest_error(APIError({"code": "23503", "message": "update or delete is still referenced"}))[0],
            409,
        )
        self.assertEqual(map_postgrest_error(APIError({"code": "23514", "message": "check constraint violated"}))[0], 400)
        self.assertEqual(
            map_postgrest_error(APIError({"code": "P0001", "message": "Ya existe un registro duplicado"}))[0],
            409,
        )
        self.assertEqual(
            map_postgrest_error(APIError({"code": "P0001", "message": "No está permitido transferir la entidad"}))[0],
            403,
        )
        self.assertEqual(map_postgrest_error(APIError({"code": "PGRST200", "message": "Relationship not found"}))[0], 502)

        # 8b. Verificación integrada del @app.exception_handler(APIError) en escritura directa de gimnasio
        token_dueno = self._make_token(self.uid_dueno, email="dueno@gym.test", email_confirmed=True)
        fixtures = {
            "dueno": [{"id_dueno": 1, "auth_user_id": self.uid_dueno, "nombre": "Martín", "apellido": "Rivas", "email": "dueno@gym.test"}],
            "gimnasio": [],
            "sucursal": [],
            "administrador": [],
            "empleado": [],
            "socio": [],
        }
        # Inyectar error 23505 en INSERT sobre gimnasio (simula constraint UNIQUE en escritura directa)
        err_db = StrictAuthMeClient(
            fixtures,
            inject_error_on_table={
                "membresia_gimnasio": APIError({"code": "23505", "message": "duplicate key uq_plan_nombre_gimnasio"})
            },
        )
        # Agregar gym 10 a fixtures para que dueño 1 tenga permiso
        fixtures["gimnasio"] = [{"id_gimnasio": 10, "nombre": "Norte Fit", "estado": True, "id_dueno": 1}]
        with patch("app.core.dependencies.verify_supabase_access_token", side_effect=self._fake_verify), patch(
            "app.core.dependencies.create_supabase_client_for_token", return_value=err_db
        ):
            res_api_err = self.client.post(
                "/api/planes-membresia",
                json={"nombre": "Plan Duplicado", "precio": 25000, "duracion_dias": 30, "id_gimnasio": 10},
                headers={"Authorization": f"Bearer {token_dueno}"},
            )
            self.assertEqual(res_api_err.status_code, 409)
            self.assertEqual(res_api_err.json()["code"], "23505")

    # 9. POST /api/gimnasios usa returning='minimal' + SELECT posterior (evita fallo RLS de fn_dueno_posee_gimnasio STABLE)
    def test_09_crear_gimnasio_usa_returning_minimal_por_rls_stable(self):
        token_dueno = self._make_token(self.uid_dueno, email="dueno@gym.test", email_confirmed=True)
        fixtures = {
            "dueno": [
                {
                    "id_dueno": 1,
                    "auth_user_id": self.uid_dueno,
                    "nombre": "Martín",
                    "apellido": "Rivas",
                    "email": "dueno@gym.test",
                }
            ],
            "gimnasio": [],
            "sucursal": [],
            "administrador": [],
            "empleado": [],
            "socio": [],
        }
        insert_returning_modes = []

        class RlsStrictGimnasioQuery(StrictAuthMeQueryBuilder):
            def __init__(self, table_name, fixtures_ref, select_log):
                super().__init__(table_name, fixtures_ref, select_log)
                self._limit_val = None
                self._order_col = None
                self._order_desc = False
                self._returning = "representation"

            def order(self, col: str, desc: bool = False):
                self._order_col = col
                self._order_desc = desc
                return self

            def limit(self, count: int):
                self._limit_val = count
                return self

            def insert(self, rows, *, returning="representation"):
                insert_returning_modes.append((self.table_name, returning))
                if self.table_name == "gimnasio" and returning != "minimal":
                    raise APIError(
                        {
                            "code": "42501",
                            "message": 'new row violates row-level security policy for table "gimnasio"',
                        }
                    )
                self._insert = rows
                self._returning = returning
                return self

            def execute(self):
                if self._insert is not None:
                    table = self.fixtures.setdefault(self.table_name, [])
                    inserted = []
                    for r in self._insert:
                        row = dict(r)
                        if self.table_name == "gimnasio" and "id_gimnasio" not in row:
                            row["id_gimnasio"] = len(table) + 1
                        table.append(row)
                        inserted.append(row)
                    if self._returning == "minimal":
                        return SimpleNamespace(data=[])
                    return SimpleNamespace(data=inserted)
                res = super().execute()
                if isinstance(res.data, list):
                    data = list(res.data)
                    if self._order_col:
                        data.sort(key=lambda x: x.get(self._order_col) or 0, reverse=self._order_desc)
                    if self._limit_val is not None:
                        data = data[: self._limit_val]
                    return SimpleNamespace(data=data)
                return res

        class RlsStrictGimnasioClient(StrictAuthMeClient):
            def from_(self, table_name: str):
                return RlsStrictGimnasioQuery(table_name, self.fixtures, self.select_log)

        rls_db = RlsStrictGimnasioClient(fixtures)
        with patch("app.core.dependencies.verify_supabase_access_token", side_effect=self._fake_verify), patch(
            "app.core.dependencies.create_supabase_client_for_token", return_value=rls_db
        ):
            res = self.client.post(
                "/api/gimnasios",
                json={"nombre": "Power Gym", "telefono": "1155556666", "email": "power@gym.com"},
                headers={"Authorization": f"Bearer {token_dueno}"},
            )
            self.assertEqual(res.status_code, 201, res.text)
            body = res.json()
            self.assertEqual(body["gimnasio"]["id_gimnasio"], 1)
            self.assertEqual(body["gimnasio"]["nombre"], "Power Gym")
            self.assertEqual(body["gimnasio"]["id_dueno"], 1)
            self.assertEqual(insert_returning_modes, [("gimnasio", "minimal")])


if __name__ == "__main__":
    unittest.main()
