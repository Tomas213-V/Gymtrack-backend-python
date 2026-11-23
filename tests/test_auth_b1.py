import unittest
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

import main as main_module
from app.config import is_service_role_key, create_supabase_client_for_token
from app.core.security import verify_supabase_access_token
from app.core.dependencies import (
    get_current_auth_user,
    get_current_user,
    require_roles,
    require_dueno,
    require_administrador,
    require_empleado,
    require_socio,
    require_gimnasio_context,
    require_sucursal_context,
)


class FakeSupabaseResponse:
    def __init__(self, data: Any = None, count: Optional[int] = None):
        self.data = data
        self.count = count


class FakeQueryBuilder:
    def __init__(self, table_name: str, fixtures: Dict[str, Any]):
        self.table_name = table_name
        self.fixtures = fixtures
        self.filters_eq: Dict[str, Any] = {}
        self.filters_in: Dict[str, List[Any]] = {}
        self.is_single = False
        self._update_payload: Optional[Dict[str, Any]] = None
        self._insert_payload: Optional[List[Dict[str, Any]]] = None

    def select(self, *_args, **_kwargs) -> "FakeQueryBuilder":
        return self

    def eq(self, col: str, val: Any) -> "FakeQueryBuilder":
        self.filters_eq[col] = val
        return self

    def in_(self, col: str, vals: List[Any]) -> "FakeQueryBuilder":
        self.filters_in[col] = list(vals)
        return self

    def order(self, *_args, **_kwargs) -> "FakeQueryBuilder":
        return self

    def maybe_single(self) -> "FakeQueryBuilder":
        self.is_single = True
        return self

    def single(self) -> "FakeQueryBuilder":
        self.is_single = True
        return self

    def update(self, payload: Dict[str, Any]) -> "FakeQueryBuilder":
        self._update_payload = payload
        return self

    def insert(self, rows: List[Dict[str, Any]]) -> "FakeQueryBuilder":
        self._insert_payload = rows
        return self

    def execute(self) -> FakeSupabaseResponse:
        rows = self.fixtures.get(self.table_name, [])
        if isinstance(rows, dict):
            rows = [rows]

        if self._insert_payload is not None:
            return FakeSupabaseResponse(self._insert_payload)

        matched = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            ok = True
            for col, val in self.filters_eq.items():
                if "." in col:
                    parent, child = col.split(".", 1)
                    nested = r.get(parent)
                    if isinstance(nested, dict) and nested.get(child) != val:
                        ok = False
                        break
                elif r.get(col) != val:
                    ok = False
                    break
            if not ok:
                continue
            for col, vals in self.filters_in.items():
                if r.get(col) not in vals:
                    ok = False
                    break
            if ok:
                if self._update_payload is not None:
                    updated_row = {**r, **self._update_payload}
                    matched.append(updated_row)
                else:
                    matched.append(r)

        if self._update_payload is not None:
            return FakeSupabaseResponse(matched)

        if self.is_single:
            return FakeSupabaseResponse(matched[0] if matched else None)
        return FakeSupabaseResponse(matched, count=len(matched))


class FakeRpcBuilder:
    def __init__(self, fn_name: str, params: Dict[str, Any], rpc_handlers: Dict[str, Any]):
        self.fn_name = fn_name
        self.params = params
        self.rpc_handlers = rpc_handlers

    def execute(self) -> FakeSupabaseResponse:
        handler = self.rpc_handlers.get(self.fn_name)
        if callable(handler):
            return FakeSupabaseResponse(handler(self.params))
        if handler is not None:
            return FakeSupabaseResponse(handler)
        return FakeSupabaseResponse({"vinculado": True, "perfil_tipo": self.params.get("p_perfil_tipo")})


class FakeSupabaseClient:
    def __init__(self, fixtures: Optional[Dict[str, Any]] = None, rpc_handlers: Optional[Dict[str, Any]] = None):
        self.fixtures = fixtures or {}
        self.rpc_handlers = rpc_handlers or {}

    def from_(self, table_name: str) -> FakeQueryBuilder:
        return FakeQueryBuilder(table_name, self.fixtures)

    def rpc(self, fn_name: str, params: Optional[Dict[str, Any]] = None) -> FakeRpcBuilder:
        return FakeRpcBuilder(fn_name, params or {}, self.rpc_handlers)


class TestSupabaseAuthB1(unittest.TestCase):
    """
    Suite de pruebas de la Fase B1: Migración de Autenticación y Autorización a Supabase Auth.
    Verifica criptográficamente JWTs (ES256 / HS256), claims (iss, aud, exp, sub, role),
    separación entre identidad Auth y perfiles de negocio, multi-perfil, multi-gimnasio,
    empleados reutilizables entre gimnasios y protección contra suplantación de rol y cruce de gimnasio.
    """

    @classmethod
    def setUpClass(cls):
        # Generar par de claves EC P-256 (ES256) idéntico a la configuración real de Supabase Auth
        cls.ec_private_key = ec.generate_private_key(ec.SECP256R1())
        cls.ec_public_key = cls.ec_private_key.public_key()
        cls.other_ec_private_key = ec.generate_private_key(ec.SECP256R1())

        cls.issuer = "https://qdlkljSb.supabase.co/auth/v1"
        cls.audience = "authenticated"
        cls.uid_dueno = "11111111-1111-4111-8111-000000000001"
        cls.uid_admin_org = "11111111-1111-4111-8111-000000000002"
        cls.uid_empleado_multi = "11111111-1111-4111-8111-000000000003"
        cls.uid_socio_multi = "11111111-1111-4111-8111-000000000004"
        cls.uid_multi_perfil = "11111111-1111-4111-8111-000000000005"
        cls.uid_sin_perfil = "11111111-1111-4111-8111-000000000006"
        cls.uid_socio_inactivo = "11111111-1111-4111-8111-000000000007"

        # Crear app de prueba que monta el router real de auth y rutas de prueba para cada dependency
        cls.test_app = FastAPI()
        cls.test_app.include_router(main_module.auth.router)

        @cls.test_app.get("/test/auth-identity")
        async def route_auth_identity(auth_user: Dict[str, Any] = Depends(get_current_auth_user)):
            return {"auth_user_id": auth_user["auth_user_id"], "email": auth_user["email"]}

        @cls.test_app.get("/test/context")
        async def route_context(user: Dict[str, Any] = Depends(get_current_user)):
            return {
                "auth_user_id": user["auth_user_id"],
                "rol": user["rol"],
                "perfiles_disponibles": user["perfiles_disponibles"],
                "id_gimnasio": user["id_gimnasio"],
                "id_sucursal": user["id_sucursal"],
                "roles_empleado": user["roles_empleado"],
                "permisos": user["permisos"],
            }

        @cls.test_app.get("/test/only-dueno")
        async def route_only_dueno(user: Dict[str, Any] = Depends(require_dueno)):
            return {"rol": user["rol"], "id_dueno": user["id_dueno"]}

        @cls.test_app.get("/test/only-admin")
        async def route_only_admin(user: Dict[str, Any] = Depends(require_administrador)):
            return {
                "rol": user["rol"],
                "id_administrador": user["id_administrador"],
                "id_gimnasio": user["id_gimnasio"],
            }

        @cls.test_app.get("/test/only-recepcionista")
        async def route_only_recep(user: Dict[str, Any] = Depends(require_empleado("recepcionista"))):
            return {
                "rol": user["rol"],
                "id_empleado": user["id_empleado"],
                "id_gimnasio": user["id_gimnasio"],
                "id_sucursal": user["id_sucursal"],
                "roles_empleado": user["roles_empleado"],
            }

        @cls.test_app.get("/test/only-socio")
        async def route_only_socio(user: Dict[str, Any] = Depends(require_socio)):
            return {
                "rol": user["rol"],
                "id_socio": user["id_socio"],
                "id_gimnasio": user["id_gimnasio"],
                "id_socio_gimnasio": user["id_socio_gimnasio"],
            }

        @cls.test_app.get("/test/gimnasio/{id_gimnasio}")
        async def route_gym_ctx(user: Dict[str, Any] = Depends(require_gimnasio_context)):
            return {"id_gimnasio": user["id_gimnasio"], "rol": user["rol"]}

        @cls.test_app.get("/test/sucursal/{id_sucursal}")
        async def route_sucursal_ctx(
            user: Dict[str, Any] = Depends(require_sucursal_context("entrenador", "administrador"))
        ):
            return {
                "id_sucursal": user["id_sucursal"],
                "id_gimnasio": user["id_gimnasio"],
                "rol": user["rol"],
                "roles_empleado": user["roles_empleado"],
            }

        @cls.test_app.get("/test/require-roles-staff")
        async def route_require_roles(
            user: Dict[str, Any] = Depends(
                require_roles("dueño", "administrador", "empleado:administrador", "empleado:recepcionista")
            )
        ):
            return {"rol": user["rol"], "id_gimnasio": user["id_gimnasio"]}

        cls.client = TestClient(cls.test_app)

    def _make_token(
        self,
        sub: Optional[str] = None,
        *,
        email: str = "user@gymtrack.test",
        role: str = "authenticated",
        iss: Optional[str] = None,
        aud: Optional[str] = None,
        exp_delta: timedelta = timedelta(hours=1),
        private_key: Any = None,
        alg: str = "ES256",
        extra_claims: Optional[Dict[str, Any]] = None,
    ) -> str:
        now = datetime.now(timezone.utc)
        payload: Dict[str, Any] = {
            "sub": sub if sub is not None else self.uid_dueno,
            "email": email,
            "role": role,
            "aud": aud if aud is not None else self.audience,
            "iss": iss if iss is not None else self.issuer,
            "iat": int(now.timestamp()),
            "exp": int((now + exp_delta).timestamp()),
            "email_confirmed_at": now.isoformat(),
            "app_metadata": {"provider": "email"},
            "user_metadata": {"email_verified": True},
        }
        if extra_claims:
            payload.update(extra_claims)

        key = private_key if private_key is not None else self.ec_private_key
        return jwt.encode(payload, key, algorithm=alg, headers={"kid": "test-kid-1"})

    def _build_default_fixtures(self) -> Dict[str, Any]:
        """
        Construye el estado de base de datos simulado conforme al esquema definitivo
        de 20 tablas en `supabase/gymtrack_instalacion_definitiva.sql`.
        """
        gym_norte = {
            "id_gimnasio": 10,
            "nombre": "Gimnasio Norte Fit",
            "telefono": "+54 11 4000-1100",
            "email": "contacto@nortefit.test",
            "fecha_registro": "2026-01-01",
            "estado": True,
            "id_dueno": 1,
        }
        gym_sur = {
            "id_gimnasio": 20,
            "nombre": "Gimnasio Sur Power",
            "telefono": "+54 11 4000-2200",
            "email": "contacto@surpower.test",
            "fecha_registro": "2026-01-01",
            "estado": True,
            "id_dueno": 2,
        }
        gym_ajeno = {
            "id_gimnasio": 99,
            "nombre": "Gimnasio Ajeno Elite",
            "telefono": "+54 11 4000-9999",
            "email": "contacto@ajeno.test",
            "fecha_registro": "2026-01-01",
            "estado": True,
            "id_dueno": 99,
        }

        suc_norte_1 = {
            "id_sucursal": 101,
            "direccion": "Av. Libertador 1250",
            "telefono": "+54 11 4000-1101",
            "descripcion": "Sede Principal Norte",
            "fecha_registro": "2026-01-01",
            "estado": True,
            "id_gimnasio": 10,
            "gimnasio": gym_norte,
        }
        suc_norte_2 = {
            "id_sucursal": 102,
            "direccion": "Calle Sarmiento 480",
            "telefono": "+54 11 4000-1102",
            "descripcion": "Sede Parque Norte",
            "fecha_registro": "2026-01-01",
            "estado": True,
            "id_gimnasio": 10,
            "gimnasio": gym_norte,
        }
        suc_sur_1 = {
            "id_sucursal": 201,
            "direccion": "Av. Costanera 900",
            "telefono": "+54 11 4000-2201",
            "descripcion": "Sede Única Sur",
            "fecha_registro": "2026-01-01",
            "estado": True,
            "id_gimnasio": 20,
            "gimnasio": gym_sur,
        }

        return {
            "dueno": [
                {
                    "id_dueno": 1,
                    "auth_user_id": self.uid_dueno,
                    "nombre": "Martín",
                    "apellido": "Rivas",
                    "email": "seed_dueno_norte@gymtrack.test",
                    "fecha_creacion": "2026-01-01T00:00:00Z",
                }
            ],
            "gimnasio": [gym_norte, gym_sur, gym_ajeno],
            "sucursal": [suc_norte_1, suc_norte_2, suc_sur_1],
            "administrador": [
                {
                    "id_administrador": 5,
                    "auth_user_id": self.uid_admin_org,
                    "nombre": "Diego",
                    "apellido": "Fernández",
                    "dni": "30111222",
                    "email": "seed_admin_org@gymtrack.test",
                    "telefono": "+54 9 11 5555-0001",
                    "estado": True,
                    "fecha_creacion": "2026-01-01T00:00:00Z",
                    "administrador_gimnasio": [
                        {
                            "id_administrador_gimnasio": 501,
                            "id_administrador": 5,
                            "id_gimnasio": 10,
                            "estado": True,
                            "gimnasio": gym_norte,
                        },
                        {
                            # Asignación inactiva en Gym Sur: NO debe otorgar acceso a Gym 20
                            "id_administrador_gimnasio": 502,
                            "id_administrador": 5,
                            "id_gimnasio": 20,
                            "estado": False,
                            "gimnasio": gym_sur,
                        },
                    ],
                }
            ],
            "empleado": [
                # Empleado con identidad reutilizable entre dos gimnasios y distintos roles por sucursal
                {
                    "id_empleado": 30,
                    "auth_user_id": self.uid_empleado_multi,
                    "nombre": "Julián",
                    "apellido": "Castro",
                    "dni": "34100103",
                    "email": "seed_trainer_norte@gymtrack.test",
                    "telefono": "+54 9 11 5555-0103",
                    "estado": True,
                    "fecha_creacion": "2026-01-01T00:00:00Z",
                    "empleado_sucursal": [
                        {
                            "id_empleado_sucursal": 301,
                            "rol": "entrenador",
                            "id_empleado": 30,
                            "id_sucursal": 101,
                            "estado": True,
                            "sucursal": suc_norte_1,
                        },
                        {
                            "id_empleado_sucursal": 302,
                            "rol": "recepcionista",
                            "id_empleado": 30,
                            "id_sucursal": 201,
                            "estado": True,
                            "sucursal": suc_sur_1,
                        },
                        {
                            # Asignación desactivada en Sucursal 102
                            "id_empleado_sucursal": 303,
                            "rol": "administrador",
                            "id_empleado": 30,
                            "id_sucursal": 102,
                            "estado": False,
                            "sucursal": suc_norte_2,
                        },
                    ],
                },
                # Usuario multi-perfil: es Empleado (entrenador en Gym 10 / Suc 101) y Socio (en Gym 20)
                {
                    "id_empleado": 31,
                    "auth_user_id": self.uid_multi_perfil,
                    "nombre": "Lucas",
                    "apellido": "MultiPerfil",
                    "dni": "38999000",
                    "email": "multiperfil@gymtrack.test",
                    "telefono": "+54 9 11 5555-0999",
                    "estado": True,
                    "fecha_creacion": "2026-01-01T00:00:00Z",
                    "empleado_sucursal": [
                        {
                            "id_empleado_sucursal": 310,
                            "rol": "entrenador",
                            "id_empleado": 31,
                            "id_sucursal": 101,
                            "estado": True,
                            "sucursal": suc_norte_1,
                        }
                    ],
                },
            ],
            "socio": [
                # Socio asociado activamente a varios gimnasios (Gym 10 y Gym 20)
                {
                    "id_socio": 70,
                    "auth_user_id": self.uid_socio_multi,
                    "nombre": "Carlos",
                    "apellido": "MultiGym",
                    "dni": "40111001",
                    "telefono": "+54 9 11 6000-0001",
                    "email": "seed_socio_multi@gymtrack.test",
                    "fecha_alta": "2026-01-01",
                    "estado": True,
                    "socio_gimnasio": [
                        {
                            "id_socio_gimnasio": 701,
                            "id_socio": 70,
                            "id_gimnasio": 10,
                            "fecha_alta": "2026-01-01",
                            "estado": True,
                            "gimnasio": gym_norte,
                            "membresia_socio": [
                                {
                                    "id_membresia_socio": 7001,
                                    "id_socio_gimnasio": 701,
                                    "id_membresia_gimnasio": 1,
                                    "id_gimnasio": 10,
                                    "precio": 25000.0,
                                    "duracion_dias_contratados": 30,
                                    "fecha_inicio": "2026-11-01",
                                    "fecha_vencimiento": "2026-12-01",
                                    "estado": "activa",
                                }
                            ],
                        },
                        {
                            "id_socio_gimnasio": 702,
                            "id_socio": 70,
                            "id_gimnasio": 20,
                            "fecha_alta": "2026-01-10",
                            "estado": True,
                            "gimnasio": gym_sur,
                            "membresia_socio": [
                                {
                                    "id_membresia_socio": 7002,
                                    "id_socio_gimnasio": 702,
                                    "id_membresia_gimnasio": 2,
                                    "id_gimnasio": 20,
                                    "precio": 30000.0,
                                    "duracion_dias_contratados": 30,
                                    "fecha_inicio": "2026-11-05",
                                    "fecha_vencimiento": "2026-12-05",
                                    "estado": "activa",
                                }
                            ],
                        },
                    ],
                },
                # Socio del usuario multi-perfil (asociado únicamente a Gym 20)
                {
                    "id_socio": 71,
                    "auth_user_id": self.uid_multi_perfil,
                    "nombre": "Lucas",
                    "apellido": "MultiPerfil",
                    "dni": "38999000",
                    "telefono": "+54 9 11 5555-0999",
                    "email": "multiperfil@gymtrack.test",
                    "fecha_alta": "2026-01-15",
                    "estado": True,
                    "socio_gimnasio": [
                        {
                            "id_socio_gimnasio": 710,
                            "id_socio": 71,
                            "id_gimnasio": 20,
                            "fecha_alta": "2026-01-15",
                            "estado": True,
                            "gimnasio": gym_sur,
                            "membresia_socio": [],
                        }
                    ],
                },
                # Socio deshabilitado globalmente (estado = False)
                {
                    "id_socio": 72,
                    "auth_user_id": self.uid_socio_inactivo,
                    "nombre": "Paula",
                    "apellido": "Inactiva",
                    "dni": "40111004",
                    "telefono": "+54 9 11 6000-0004",
                    "email": "inactiva@gymtrack.test",
                    "fecha_alta": "2026-01-01",
                    "estado": False,
                    "socio_gimnasio": [],
                },
            ],
            "socio_gimnasio": [
                {"id_socio_gimnasio": 701, "id_socio": 70, "id_gimnasio": 10, "estado": True},
                {"id_socio_gimnasio": 702, "id_socio": 70, "id_gimnasio": 10, "estado": False},
            ],
        }

    # ==========================================================================
    # 1. PRUEBAS CRIPTOGRÁFICAS DE TOKEN (VÁLIDO, EXPIRADO, FIRMA, ISSUER, AUDIENCE)
    # ==========================================================================

    def test_01_token_valido_es256_y_hs256(self):
        """Verifica que tokens válidos firmados con ES256 (asimétrico) o HS256 (simétrico) se validan correctamente."""
        token_es256 = self._make_token(sub=self.uid_dueno, email="seed_dueno_norte@gymtrack.test")
        identity = verify_supabase_access_token(
            token_es256,
            verification_key=self.ec_public_key,
            expected_issuer=self.issuer,
            expected_audience=self.audience,
        )
        self.assertEqual(identity["auth_user_id"], self.uid_dueno)
        self.assertEqual(identity["email"], "seed_dueno_norte@gymtrack.test")
        self.assertEqual(identity["role"], "authenticated")
        self.assertTrue(identity["email_verified"])

        # Token HS256 con secreto explícito de Supabase Auth
        hs_secret = "supabase-project-jwt-secret-key-at-least-32-bytes"
        token_hs256 = self._make_token(
            sub=self.uid_dueno,
            email="seed_dueno_norte@gymtrack.test",
            private_key=hs_secret,
            alg="HS256",
        )
        identity_hs = verify_supabase_access_token(
            token_hs256,
            jwt_secret=hs_secret,
            expected_issuer=self.issuer,
            expected_audience=self.audience,
        )
        self.assertEqual(identity_hs["auth_user_id"], self.uid_dueno)

    def test_02_token_expirado_rechazado_401(self):
        """Verifica que un token expirado sea rechazado con HTTP 401."""
        expired_token = self._make_token(exp_delta=timedelta(minutes=-15))
        with self.assertRaises(Exception) as ctx:
            verify_supabase_access_token(
                expired_token,
                verification_key=self.ec_public_key,
                expected_issuer=self.issuer,
                expected_audience=self.audience,
            )
        exc = ctx.exception
        self.assertEqual(getattr(exc, "status_code", None), 401)
        self.assertIn("expirado", str(getattr(exc, "detail", "")).lower())

    def test_03_firma_incorrecta_o_token_manipulado_rechazado_401(self):
        """Verifica que un token firmado con otra clave o con payload alterado sea rechazado con HTTP 401."""
        forged_token = self._make_token(private_key=self.other_ec_private_key)
        with self.assertRaises(Exception) as ctx:
            verify_supabase_access_token(
                forged_token,
                verification_key=self.ec_public_key,
                expected_issuer=self.issuer,
                expected_audience=self.audience,
            )
        self.assertEqual(getattr(ctx.exception, "status_code", None), 401)
        self.assertIn("firma", str(getattr(ctx.exception, "detail", "")).lower())

        # Token sin firma (alg='none') tampoco debe aceptarse jamás
        unsigned = jwt.encode(
            {
                "sub": self.uid_dueno,
                "role": "authenticated",
                "aud": self.audience,
                "iss": self.issuer,
                "iat": int(datetime.now(timezone.utc).timestamp()),
                "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
            },
            "",
            algorithm="none",
        )
        with self.assertRaises(Exception) as ctx_none:
            verify_supabase_access_token(
                unsigned,
                verification_key=self.ec_public_key,
                expected_issuer=self.issuer,
                expected_audience=self.audience,
            )
        self.assertEqual(getattr(ctx_none.exception, "status_code", None), 401)

    def test_04_issuer_y_audience_incorrectos_rechazados_401(self):
        """Verifica que un token con issuer o audience distinto al proyecto Supabase sea rechazado con HTTP 401."""
        wrong_issuer_token = self._make_token(iss="https://otro-proyecto-malicioso.supabase.co/auth/v1")
        with self.assertRaises(Exception) as ctx_iss:
            verify_supabase_access_token(
                wrong_issuer_token,
                verification_key=self.ec_public_key,
                expected_issuer=self.issuer,
                expected_audience=self.audience,
            )
        self.assertEqual(getattr(ctx_iss.exception, "status_code", None), 401)
        self.assertIn("issuer", str(getattr(ctx_iss.exception, "detail", "")).lower())

        wrong_aud_token = self._make_token(aud="wrong-audience")
        with self.assertRaises(Exception) as ctx_aud:
            verify_supabase_access_token(
                wrong_aud_token,
                verification_key=self.ec_public_key,
                expected_issuer=self.issuer,
                expected_audience=self.audience,
            )
        self.assertEqual(getattr(ctx_aud.exception, "status_code", None), 401)
        self.assertIn("audience", str(getattr(ctx_aud.exception, "detail", "")).lower())

    def test_05_rechazo_service_role_y_usuarios_deshabilitados_auth(self):
        """Verifica que tokens con role='service_role' o 'anon' y usuarios Auth deshabilitados sean rechazados."""
        service_token = self._make_token(role="service_role")
        self.assertTrue(is_service_role_key(service_token))
        self.assertTrue(is_service_role_key("sb_secret_test_key_123"))

        with self.assertRaises(Exception) as ctx_sr:
            verify_supabase_access_token(
                service_token,
                verification_key=self.ec_public_key,
                expected_issuer=self.issuer,
                expected_audience=self.audience,
            )
        self.assertEqual(getattr(ctx_sr.exception, "status_code", None), 401)

        with self.assertRaises(PermissionError):
            create_supabase_client_for_token(service_token)

        # Usuario baneado/deshabilitado en Supabase Auth -> 403 Forbidden
        future_ban = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        banned_token = self._make_token(extra_claims={"banned_until": future_ban})
        with self.assertRaises(Exception) as ctx_ban:
            verify_supabase_access_token(
                banned_token,
                verification_key=self.ec_public_key,
                expected_issuer=self.issuer,
                expected_audience=self.audience,
            )
        self.assertEqual(getattr(ctx_ban.exception, "status_code", None), 403)

    # ==========================================================================
    # 2. PRUEBAS DE ENDPOINTS Y DEPENDENCIAS FASTAPI (AUTH, PERFILES Y TENANTS)
    # ==========================================================================

    def _patch_auth_environment(self, fixtures: Optional[Dict[str, Any]] = None):
        fake_db = FakeSupabaseClient(fixtures if fixtures is not None else self._build_default_fixtures())

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

    def test_06_usuario_no_autenticado_401(self):
        """Solicitudes sin cabecera Authorization o con formato Bearer inválido reciben 401."""
        res_no_header = self.client.get("/api/auth/me")
        self.assertEqual(res_no_header.status_code, 401)

        res_bad_scheme = self.client.get("/api/auth/me", headers={"Authorization": "Basic abc123"})
        self.assertEqual(res_bad_scheme.status_code, 401)

        res_empty_bearer = self.client.get("/api/auth/me", headers={"Authorization": "Bearer   "})
        self.assertEqual(res_empty_bearer.status_code, 401)

    def test_07_usuario_autenticado_sin_perfil_de_negocio_403_pero_con_identidad_auth(self):
        """
        Un usuario válido en Supabase Auth pero sin registro en dueno/administrador/empleado/socio:
        - Puede acceder a dependencias de nivel 1 (get_current_auth_user, ej. /vincular-perfil).
        - Es rechazado con 403 Forbidden en endpoints de negocio (get_current_user, ej. /api/auth/me).
        """
        token = self._make_token(sub=self.uid_sin_perfil, email="nuevo_sin_perfil@gymtrack.test")
        p1, p2, _ = self._patch_auth_environment()
        with p1, p2:
            # Nivel 1 (Identidad Auth pura) funciona
            res_ident = self.client.get(
                "/test/auth-identity",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(res_ident.status_code, 200)
            self.assertEqual(res_ident.json()["auth_user_id"], self.uid_sin_perfil)

            # Vinculación de perfil vía RPC funciona con identidad Auth
            res_vinc = self.client.post(
                "/api/auth/vincular-perfil",
                json={"perfil_tipo": "socio"},
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(res_vinc.status_code, 200)
            self.assertTrue(res_vinc.json()["resultado"]["vinculado"])

            # Nivel 2 (Perfil de negocio) rechaza con 403
            res_me = self.client.get(
                "/api/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(res_me.status_code, 403)
            self.assertIn("perfil de negocio", res_me.json()["detail"].lower())

    def test_08_usuario_con_perfil_deshabilitado_403(self):
        """Un usuario cuyo perfil de negocio tiene estado=False es rechazado con 403 Forbidden."""
        token = self._make_token(sub=self.uid_socio_inactivo, email="inactiva@gymtrack.test")
        p1, p2, _ = self._patch_auth_environment()
        with p1, p2:
            res = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(res.status_code, 403)
            self.assertIn("inactiv", res.json()["detail"].lower())

    def test_09_usuario_con_varios_perfiles_y_relaciones_multigimnasio(self):
        """
        Verifica los casos multi-relación del esquema definitivo:
        1. Socio asociado a múltiples gimnasios (socio_gimnasio en Gym 10 y Gym 20).
        2. Empleado con identidad reutilizable entre distintos gimnasios y roles distintos por sucursal
           (entrenador en Sucursal 101 de Gym 10; recepcionista en Sucursal 201 de Gym 20).
        3. Administrador organizacional con múltiples filas en administrador_gimnasio (activo en Gym 10, inactivo en Gym 20).
        4. Usuario con múltiples perfiles simultáneos (Empleado en Gym 10 + Socio en Gym 20).
        """
        p1, p2, _ = self._patch_auth_environment()
        with p1, p2:
            # A) Socio multi-gimnasio puede alternar entre Gym 10 y Gym 20
            token_socio = self._make_token(sub=self.uid_socio_multi, email="seed_socio_multi@gymtrack.test")
            res_s_g10 = self.client.get(
                "/test/only-socio",
                headers={"Authorization": f"Bearer {token_socio}", "X-Gimnasio-Id": "10"},
            )
            self.assertEqual(res_s_g10.status_code, 200)
            self.assertEqual(res_s_g10.json()["id_gimnasio"], 10)
            self.assertEqual(res_s_g10.json()["id_socio_gimnasio"], 701)

            res_s_g20 = self.client.get(
                "/test/only-socio",
                headers={"Authorization": f"Bearer {token_socio}", "X-Gimnasio-Id": "20"},
            )
            self.assertEqual(res_s_g20.status_code, 200)
            self.assertEqual(res_s_g20.json()["id_gimnasio"], 20)
            self.assertEqual(res_s_g20.json()["id_socio_gimnasio"], 702)

            # B) Empleado reutilizable entre Gym 10 (entrenador en Suc 101) y Gym 20 (recepcionista en Suc 201)
            token_emp = self._make_token(sub=self.uid_empleado_multi, email="seed_trainer_norte@gymtrack.test")
            # En Sucursal 101 es entrenador -> accede a /test/sucursal/101 (requiere entrenador o administrador)
            res_emp_s101 = self.client.get(
                "/test/sucursal/101",
                headers={"Authorization": f"Bearer {token_emp}"},
            )
            self.assertEqual(res_emp_s101.status_code, 200)
            self.assertEqual(res_emp_s101.json()["roles_empleado"], ["entrenador"])

            # En Sucursal 201 es recepcionista -> NO tiene permiso de entrenador en /test/sucursal/201 (403)
            res_emp_s201_denied = self.client.get(
                "/test/sucursal/201",
                headers={"Authorization": f"Bearer {token_emp}"},
            )
            self.assertEqual(res_emp_s201_denied.status_code, 403)

            # Pero en Gym 20 / Sucursal 201 SÍ puede acceder a /test/only-recepcionista
            res_emp_recep = self.client.get(
                "/test/only-recepcionista",
                headers={"Authorization": f"Bearer {token_emp}", "X-Gimnasio-Id": "20"},
            )
            self.assertEqual(res_emp_recep.status_code, 200)
            self.assertEqual(res_emp_recep.json()["id_gimnasio"], 20)
            self.assertEqual(res_emp_recep.json()["roles_empleado"], ["recepcionista"])

            # C) Usuario con múltiples perfiles (Empleado en Gym 10 + Socio en Gym 20)
            token_mp = self._make_token(sub=self.uid_multi_perfil, email="multiperfil@gymtrack.test")
            res_mp_default = self.client.get(
                "/test/context",
                headers={"Authorization": f"Bearer {token_mp}"},
            )
            self.assertEqual(res_mp_default.status_code, 200)
            self.assertEqual(set(res_mp_default.json()["perfiles_disponibles"]), {"empleado", "socio"})

            # Seleccionando explícitamente rol 'socio' opera en su gimnasio de socio (Gym 20)
            res_mp_socio = self.client.get(
                "/test/context",
                headers={"Authorization": f"Bearer {token_mp}", "X-Active-Role": "socio"},
            )
            self.assertEqual(res_mp_socio.status_code, 200)
            self.assertEqual(res_mp_socio.json()["rol"], "socio")
            self.assertEqual(res_mp_socio.json()["id_gimnasio"], 20)

            # Y sin X-Active-Role, el endpoint require_socio resuelve automáticamente su perfil de socio
            res_mp_auto_socio = self.client.get(
                "/test/only-socio",
                headers={"Authorization": f"Bearer {token_mp}"},
            )
            self.assertEqual(res_mp_auto_socio.status_code, 200)
            self.assertEqual(res_mp_auto_socio.json()["id_socio"], 71)
            self.assertEqual(res_mp_auto_socio.json()["id_gimnasio"], 20)

    def test_10_intento_de_suplantar_un_rol_403(self):
        """
        Verifica que ningún usuario pueda suplantar un rol que no posee en la base de datos:
        - Ni enviando X-Active-Role (ej. socio enviando X-Active-Role: dueño / administrador / empleado).
        - Ni inyectando claims 'rol': 'dueño' en user_metadata del JWT.
        - Ni invocando endpoints protegidos por require_dueno / require_administrador / require_roles.
        """
        # Socio con user_metadata falsificado intentando proclamarse 'dueño'
        token_socio_spoof = self._make_token(
            sub=self.uid_socio_multi,
            email="seed_socio_multi@gymtrack.test",
            extra_claims={"user_metadata": {"rol": "dueño", "id_dueno": 1}},
        )
        p1, p2, _ = self._patch_auth_environment()
        with p1, p2:
            # 1. El backend ignora el rol del frontend/metadata y resuelve 'socio' desde BD
            res_ctx = self.client.get(
                "/test/context",
                headers={"Authorization": f"Bearer {token_socio_spoof}"},
            )
            self.assertEqual(res_ctx.status_code, 200)
            self.assertEqual(res_ctx.json()["rol"], "socio")

            # 2. Si envía X-Active-Role: dueno (o due%C3%B1o) -> 403 Forbidden inmediato
            res_spoof_header = self.client.get(
                "/test/context",
                headers={
                    "Authorization": f"Bearer {token_socio_spoof}",
                    "X-Active-Role": "dueno",
                },
            )
            self.assertEqual(res_spoof_header.status_code, 403)

            res_spoof_encoded = self.client.get(
                "/test/context",
                headers={
                    "Authorization": f"Bearer {token_socio_spoof}",
                    "X-Active-Role": "due%C3%B1o",
                },
            )
            self.assertEqual(res_spoof_encoded.status_code, 403)

            # 3. Si envía X-Active-Role: administrador -> 403 Forbidden
            res_spoof_admin = self.client.get(
                "/test/context",
                headers={
                    "Authorization": f"Bearer {token_socio_spoof}",
                    "X-Active-Role": "administrador",
                },
            )
            self.assertEqual(res_spoof_admin.status_code, 403)

            # 4. Si intenta acceder a endpoints de dueño, admin o staff -> 403 Forbidden
            self.assertEqual(
                self.client.get(
                    "/test/only-dueno",
                    headers={"Authorization": f"Bearer {token_socio_spoof}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.get(
                    "/test/only-admin",
                    headers={"Authorization": f"Bearer {token_socio_spoof}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.get(
                    "/test/require-roles-staff",
                    headers={"Authorization": f"Bearer {token_socio_spoof}"},
                ).status_code,
                403,
            )

            # 5. Empleado entrenador en Gym 10 intentando suplantar sub-rol 'empleado:administrador' -> 403
            token_emp = self._make_token(sub=self.uid_empleado_multi, email="seed_trainer_norte@gymtrack.test")
            res_emp_spoof_sub = self.client.get(
                "/test/context",
                headers={
                    "Authorization": f"Bearer {token_emp}",
                    "X-Active-Role": "empleado:administrador",
                },
            )
            self.assertEqual(res_emp_spoof_sub.status_code, 403)

    def test_11_intento_de_acceder_a_gimnasio_o_sucursal_no_autorizado_403(self):
        """
        Verifica el bloqueo anti-IDOR / anti-cruce de tenant cuando un usuario intenta
        operar sobre un id_gimnasio o id_sucursal al que no tiene acceso activo en BD.
        """
        p1, p2, _ = self._patch_auth_environment()
        with p1, p2:
            # 1. Dueño 1 (dueño de Gym 10) intenta acceder a Gym 20 (de Dueño 2) o Gym 99
            token_dueno = self._make_token(sub=self.uid_dueno, email="seed_dueno_norte@gymtrack.test")
            res_dueno_ok = self.client.get(
                "/test/gimnasio/10",
                headers={"Authorization": f"Bearer {token_dueno}"},
            )
            self.assertEqual(res_dueno_ok.status_code, 200)

            res_dueno_gym20 = self.client.get(
                "/test/gimnasio/20",
                headers={"Authorization": f"Bearer {token_dueno}"},
            )
            self.assertEqual(res_dueno_gym20.status_code, 403)

            res_dueno_header_gym99 = self.client.get(
                "/api/auth/me",
                headers={"Authorization": f"Bearer {token_dueno}", "X-Gimnasio-Id": "99"},
            )
            self.assertEqual(res_dueno_header_gym99.status_code, 403)

            # 2. Administrador Organizacional: activo en Gym 10, pero INACTIVO (estado=False) en Gym 20
            token_admin = self._make_token(sub=self.uid_admin_org, email="seed_admin_org@gymtrack.test")
            res_admin_g10 = self.client.get(
                "/test/only-admin",
                headers={"Authorization": f"Bearer {token_admin}", "X-Gimnasio-Id": "10"},
            )
            self.assertEqual(res_admin_g10.status_code, 200)
            self.assertEqual(res_admin_g10.json()["id_gimnasio"], 10)

            res_admin_g20_inactivo = self.client.get(
                "/test/only-admin",
                headers={"Authorization": f"Bearer {token_admin}", "X-Gimnasio-Id": "20"},
            )
            self.assertEqual(res_admin_g20_inactivo.status_code, 403)

            # 3. Empleado asignado a Sucursal 101 pero con asignación inactiva en Sucursal 102
            token_emp = self._make_token(sub=self.uid_empleado_multi, email="seed_trainer_norte@gymtrack.test")
            res_emp_suc102 = self.client.get(
                "/test/sucursal/102",
                headers={"Authorization": f"Bearer {token_emp}"},
            )
            self.assertEqual(res_emp_suc102.status_code, 403)

            # 4. Usuario multi-perfil (Empleado en Gym 10, Socio en Gym 20):
            # Si actúa con rol 'socio' e intenta acceder a Gym 10 (donde solo es empleado, no socio) -> 403
            token_mp = self._make_token(sub=self.uid_multi_perfil, email="multiperfil@gymtrack.test")
            res_mp_cross = self.client.get(
                "/test/context",
                headers={
                    "Authorization": f"Bearer {token_mp}",
                    "X-Active-Role": "socio",
                    "X-Gimnasio-Id": "10",
                },
            )
            self.assertEqual(res_mp_cross.status_code, 403)

    def test_12_endpoint_auth_me_para_cada_perfil(self):
        """Verifica que GET /api/auth/me devuelva la estructura correcta para Dueño, Administrador, Empleado y Socio."""
        p1, p2, _ = self._patch_auth_environment()
        with p1, p2:
            for uid, expected_role in (
                (self.uid_dueno, "dueño"),
                (self.uid_admin_org, "administrador"),
                (self.uid_empleado_multi, "empleado"),
                (self.uid_socio_multi, "socio"),
            ):
                token = self._make_token(sub=uid)
                res = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
                self.assertEqual(res.status_code, 200)
                body = res.json()
                self.assertEqual(body["usuario"]["rol"], expected_role)
                self.assertEqual(body["usuario"]["auth_user_id"], uid)


if __name__ == "__main__":
    unittest.main()
