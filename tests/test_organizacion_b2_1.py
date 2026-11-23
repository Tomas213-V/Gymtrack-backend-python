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


class FakeOrgQueryBuilder:
    def __init__(self, table_name: str, state: Dict[str, List[Dict[str, Any]]]):
        self.table_name = table_name
        self.state = state
        self.filters_eq: Dict[str, Any] = {}
        self.filters_in: Dict[str, List[Any]] = {}
        self.is_single = False
        self._update_payload: Optional[Dict[str, Any]] = None
        self._insert_payload: Optional[List[Dict[str, Any]]] = None

    def select(self, *_args, **_kwargs) -> "FakeOrgQueryBuilder":
        return self

    def eq(self, col: str, val: Any) -> "FakeOrgQueryBuilder":
        self.filters_eq[col] = val
        return self

    def in_(self, col: str, vals: List[Any]) -> "FakeOrgQueryBuilder":
        self.filters_in[col] = list(vals)
        return self

    def order(self, *_args, **_kwargs) -> "FakeOrgQueryBuilder":
        return self

    def maybe_single(self) -> "FakeOrgQueryBuilder":
        self.is_single = True
        return self

    def single(self) -> "FakeOrgQueryBuilder":
        self.is_single = True
        return self

    def update(self, payload: Dict[str, Any]) -> "FakeOrgQueryBuilder":
        self._update_payload = payload
        return self

    def insert(self, rows: List[Dict[str, Any]]) -> "FakeOrgQueryBuilder":
        self._insert_payload = rows
        return self

    def _enrich_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        enriched = copy.deepcopy(row)
        gyms_by_id = {int(g["id_gimnasio"]): g for g in self.state.get("gimnasio", [])}
        sucs_by_id = {int(s["id_sucursal"]): s for s in self.state.get("sucursal", [])}
        admins_by_id = {int(a["id_administrador"]): a for a in self.state.get("administrador", [])}
        emps_by_id = {int(e["id_empleado"]): e for e in self.state.get("empleado", [])}

        if self.table_name == "sucursal":
            gid = int(enriched.get("id_gimnasio") or 0)
            if gid in gyms_by_id:
                enriched["gimnasio"] = copy.deepcopy(gyms_by_id[gid])

        elif self.table_name == "administrador":
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

        elif self.table_name == "administrador_gimnasio":
            aid = int(enriched.get("id_administrador") or 0)
            gid = int(enriched.get("id_gimnasio") or 0)
            if aid in admins_by_id:
                enriched["administrador"] = copy.deepcopy(admins_by_id[aid])
            if gid in gyms_by_id:
                enriched["gimnasio"] = copy.deepcopy(gyms_by_id[gid])

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

        elif self.table_name == "empleado_sucursal":
            eid = int(enriched.get("id_empleado") or 0)
            sid = int(enriched.get("id_sucursal") or 0)
            if eid in emps_by_id:
                enriched["empleado"] = copy.deepcopy(emps_by_id[eid])
            if sid in sucs_by_id:
                suc_copy = copy.deepcopy(sucs_by_id[sid])
                gid = int(suc_copy.get("id_gimnasio") or 0)
                if gid in gyms_by_id:
                    suc_copy["gimnasio"] = copy.deepcopy(gyms_by_id[gid])
                enriched["sucursal"] = suc_copy

        return enriched

    def execute(self) -> FakeSupabaseResponse:
        if self.table_name == "vw_catalogo_gimnasios":
            active_gyms = [
                {
                    "id_gimnasio": g["id_gimnasio"],
                    "nombre": g["nombre"],
                    "telefono": g.get("telefono"),
                    "estado": g["estado"],
                }
                for g in self.state.get("gimnasio", [])
                if g.get("estado") is True
            ]
            return FakeSupabaseResponse(active_gyms, count=len(active_gyms))

        rows = self.state.setdefault(self.table_name, [])

        if self._insert_payload is not None:
            pk_map = {
                "gimnasio": "id_gimnasio",
                "sucursal": "id_sucursal",
                "administrador": "id_administrador",
                "administrador_gimnasio": "id_administrador_gimnasio",
                "empleado": "id_empleado",
                "empleado_sucursal": "id_empleado_sucursal",
            }
            pk_col = pk_map.get(self.table_name)
            inserted = []
            for item in self._insert_payload:
                new_row = copy.deepcopy(item)
                if pk_col and new_row.get(pk_col) is None:
                    max_id = max([int(r.get(pk_col) or 0) for r in rows], default=100)
                    new_row[pk_col] = max_id + 1
                rows.append(new_row)
                inserted.append(self._enrich_row(new_row))
            return FakeSupabaseResponse(inserted, count=len(inserted))

        matched_refs: List[Dict[str, Any]] = []
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


class FakeOrgRpcBuilder:
    def __init__(self, fn_name: str, params: Dict[str, Any], client: "FakeOrgSupabaseClient"):
        self.fn_name = fn_name
        self.params = params
        self.client = client

    def execute(self) -> FakeSupabaseResponse:
        self.client.rpc_calls.append((self.fn_name, copy.deepcopy(self.params)))
        if self.fn_name == "rpc_registrar_administrador_gimnasio":
            return FakeSupabaseResponse(self.client._rpc_registrar_administrador_gimnasio(self.params))
        if self.fn_name == "rpc_registrar_empleado_sucursal":
            return FakeSupabaseResponse(self.client._rpc_registrar_empleado_sucursal(self.params))
        raise RuntimeError(f"RPC no soportada en mock: {self.fn_name}")


class FakeOrgSupabaseClient:
    def __init__(self, state: Dict[str, List[Dict[str, Any]]]):
        self.state = state
        self.rpc_calls: List[tuple[str, Dict[str, Any]]] = []

    def from_(self, table_name: str) -> FakeOrgQueryBuilder:
        return FakeOrgQueryBuilder(table_name, self.state)

    def rpc(self, fn_name: str, params: Optional[Dict[str, Any]] = None) -> FakeOrgRpcBuilder:
        return FakeOrgRpcBuilder(fn_name, params or {}, self)

    def _rpc_registrar_administrador_gimnasio(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Simula fielmente `public.rpc_registrar_administrador_gimnasio` del instalador SQL."""
        gid = int(params["p_id_gimnasio"])
        email = str(params["p_email"]).strip().lower()
        dni = str(params["p_dni"]).strip() if params.get("p_dni") else None

        admins = self.state.setdefault("administrador", [])
        existing = next(
            (
                a for a in admins
                if str(a.get("email") or "").strip().lower() == email
                or (dni and str(a.get("dni") or "").strip() == dni)
            ),
            None,
        )
        creado = False
        if existing is None:
            new_id = max([int(a["id_administrador"]) for a in admins], default=10) + 1
            existing = {
                "id_administrador": new_id,
                "auth_user_id": None,
                "nombre": params["p_nombre"],
                "apellido": params["p_apellido"],
                "dni": dni,
                "email": email,
                "telefono": params.get("p_telefono"),
                "estado": True,
                "fecha_creacion": "2026-11-23T12:00:00Z",
            }
            admins.append(existing)
            creado = True
        else:
            existing["estado"] = True

        ags = self.state.setdefault("administrador_gimnasio", [])
        ag = next(
            (
                row for row in ags
                if int(row["id_administrador"]) == int(existing["id_administrador"])
                and int(row["id_gimnasio"]) == gid
            ),
            None,
        )
        if ag is None:
            new_ag_id = max([int(r["id_administrador_gimnasio"]) for r in ags], default=500) + 1
            ag = {
                "id_administrador_gimnasio": new_ag_id,
                "id_administrador": int(existing["id_administrador"]),
                "id_gimnasio": gid,
                "fecha_asignacion": "2026-11-23T12:00:00Z",
                "estado": True,
            }
            ags.append(ag)
        else:
            ag["estado"] = True

        return {
            "creado": creado,
            "administrador": copy.deepcopy(existing),
            "administrador_gimnasio": copy.deepcopy(ag),
        }

    def _rpc_registrar_empleado_sucursal(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Simula fielmente `public.rpc_registrar_empleado_sucursal`:
        Reutiliza la identidad `public.empleado` por `p_id_empleado`, `p_dni` o `p_email`
        sin duplicar el registro ni crear usuarios Auth duplicados.
        """
        sid = int(params["p_id_sucursal"])
        rol = str(params["p_rol"]).strip().lower()
        pid = int(params["p_id_empleado"]) if params.get("p_id_empleado") is not None else None
        dni = str(params["p_dni"]).strip() if params.get("p_dni") else None
        email = str(params["p_email"]).strip().lower() if params.get("p_email") else None

        emps = self.state.setdefault("empleado", [])
        by_id = next((e for e in emps if pid is not None and int(e["id_empleado"]) == pid), None)
        by_dni = next((e for e in emps if dni and str(e.get("dni") or "").strip() == dni), None)
        by_email = next(
            (e for e in emps if email and str(e.get("email") or "").strip().lower() == email),
            None,
        )

        if pid is not None and by_id is None:
            raise ValueError(f"El empleado #{pid} no existe.")

        found_ids = {
            int(x["id_empleado"])
            for x in (by_id, by_dni, by_email)
            if x is not None
        }
        if len(found_ids) > 1:
            raise ValueError("Conflicto de identidad: el id_empleado, DNI y/o email pertenecen a empleados distintos.")

        empleado = by_id or by_dni or by_email
        creado = False
        if empleado is not None:
            empleado["estado"] = True
            if dni and not empleado.get("dni"):
                empleado["dni"] = dni
            if email and not empleado.get("email"):
                empleado["email"] = email
            if params.get("p_telefono"):
                empleado["telefono"] = params["p_telefono"]
        else:
            if not params.get("p_nombre") or not params.get("p_apellido"):
                raise ValueError("Nombre y apellido son obligatorios para crear un nuevo empleado.")
            new_id = max([int(e["id_empleado"]) for e in emps], default=30) + 1
            empleado = {
                "id_empleado": new_id,
                "auth_user_id": None,
                "nombre": params["p_nombre"],
                "apellido": params["p_apellido"],
                "dni": dni,
                "email": email,
                "telefono": params.get("p_telefono"),
                "estado": True,
                "fecha_creacion": "2026-11-23T12:00:00Z",
            }
            emps.append(empleado)
            creado = True

        ess = self.state.setdefault("empleado_sucursal", [])
        es = next(
            (
                row for row in ess
                if int(row["id_empleado"]) == int(empleado["id_empleado"])
                and int(row["id_sucursal"]) == sid
            ),
            None,
        )
        if es is None:
            new_es_id = max([int(r["id_empleado_sucursal"]) for r in ess], default=300) + 1
            es = {
                "id_empleado_sucursal": new_es_id,
                "rol": rol,
                "id_empleado": int(empleado["id_empleado"]),
                "id_sucursal": sid,
                "estado": True,
                "fecha_asignacion": "2026-11-23T12:00:00Z",
            }
            ess.append(es)
        else:
            es["rol"] = rol
            es["estado"] = True

        return {
            "creado": creado,
            "empleado": copy.deepcopy(empleado),
            "empleado_sucursal": copy.deepcopy(es),
        }


class TestOrganizacionB21(unittest.TestCase):
    """
    Suite de pruebas unitarias de la Fase B2.1: Adaptación Organizacional del Backend FastAPI.
    Cubre los 10 escenarios obligatorios de autorización, multi-gimnasio, multi-sucursal,
    reutilización de identidad de empleado, RPCs y anti-spoofing.
    """

    @classmethod
    def setUpClass(cls):
        cls.ec_private_key = ec.generate_private_key(ec.SECP256R1())
        cls.ec_public_key = cls.ec_private_key.public_key()
        cls.issuer = "https://qdlkljSb.supabase.co/auth/v1"
        cls.audience = "authenticated"

        cls.uid_dueno_1 = "11111111-1111-4111-8111-000000000001"
        cls.uid_dueno_2 = "11111111-1111-4111-8111-000000000002"
        cls.uid_admin_multigym = "11111111-1111-4111-8111-000000000003"
        cls.uid_admin_sin_asignacion = "11111111-1111-4111-8111-000000000004"
        cls.uid_emp_multisuc = "11111111-1111-4111-8111-000000000005"
        cls.uid_emp_multigym = "11111111-1111-4111-8111-000000000006"
        cls.uid_emp_desactivado = "11111111-1111-4111-8111-000000000007"
        cls.uid_emp_recepcionista = "11111111-1111-4111-8111-000000000008"

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
        return jwt.encode(payload, self.ec_private_key, algorithm="ES256", headers={"kid": "test-kid-b21"})

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
                    "id_gimnasio": 11,
                    "nombre": "Gimnasio Norte Urbano",
                    "telefono": "+54 11 4000-1200",
                    "email": "urbano@gymtrack.test",
                    "fecha_registro": "2026-02-01",
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
                    "id_sucursal": 102,
                    "direccion": "Calle Sarmiento 480",
                    "telefono": "+54 11 4000-1102",
                    "descripcion": "Sede Parque Norte",
                    "fecha_registro": "2026-01-01",
                    "estado": True,
                    "id_gimnasio": 10,
                },
                {
                    "id_sucursal": 111,
                    "direccion": "Av. Cabildo 3200",
                    "telefono": "+54 11 4000-1201",
                    "descripcion": "Sede Urbano",
                    "fecha_registro": "2026-02-01",
                    "estado": True,
                    "id_gimnasio": 11,
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
                    "id_administrador": 5,
                    "auth_user_id": self.uid_admin_multigym,
                    "nombre": "Diego",
                    "apellido": "MultiAdmin",
                    "dni": "30111222",
                    "email": "admin_multi@gymtrack.test",
                    "telefono": "+54 9 11 5555-0001",
                    "estado": True,
                    "fecha_creacion": "2026-01-01T00:00:00Z",
                },
                {
                    "id_administrador": 6,
                    "auth_user_id": self.uid_admin_sin_asignacion,
                    "nombre": "Carla",
                    "apellido": "SinGym",
                    "dni": "30999888",
                    "email": "admin_sin_gym@gymtrack.test",
                    "telefono": "+54 9 11 5555-0002",
                    "estado": True,
                    "fecha_creacion": "2026-01-01T00:00:00Z",
                },
            ],
            "administrador_gimnasio": [
                # Admin 5 gestiona activamente Gym 10 y Gym 20
                {
                    "id_administrador_gimnasio": 501,
                    "id_administrador": 5,
                    "id_gimnasio": 10,
                    "fecha_asignacion": "2026-01-01T00:00:00Z",
                    "estado": True,
                },
                {
                    "id_administrador_gimnasio": 502,
                    "id_administrador": 5,
                    "id_gimnasio": 20,
                    "fecha_asignacion": "2026-01-02T00:00:00Z",
                    "estado": True,
                },
                # Admin 6 solo tiene una asignación desactivada (estado = False)
                {
                    "id_administrador_gimnasio": 503,
                    "id_administrador": 6,
                    "id_gimnasio": 10,
                    "fecha_asignacion": "2026-01-03T00:00:00Z",
                    "estado": False,
                },
            ],
            "empleado": [
                # Empleado 30: asignado a múltiples sucursales del mismo gimnasio (101 y 102)
                {
                    "id_empleado": 30,
                    "auth_user_id": self.uid_emp_multisuc,
                    "nombre": "Julián",
                    "apellido": "MultiSucursal",
                    "dni": "34100103",
                    "email": "emp_multisuc@gymtrack.test",
                    "telefono": "+54 9 11 5555-0103",
                    "estado": True,
                    "fecha_creacion": "2026-01-01T00:00:00Z",
                },
                # Empleado 31: trabaja en gimnasios distintos (entrenador en Suc 101 de Gym 10 y administrador de sucursal en Suc 201 de Gym 20)
                {
                    "id_empleado": 31,
                    "auth_user_id": self.uid_emp_multigym,
                    "nombre": "Bruno",
                    "apellido": "MultiGimnasio",
                    "dni": "36200202",
                    "email": "emp_multigym@gymtrack.test",
                    "telefono": "+54 9 11 5555-0202",
                    "estado": True,
                    "fecha_creacion": "2026-01-01T00:00:00Z",
                },
                # Empleado 32: tiene su única asignación desactivada (estado = False)
                {
                    "id_empleado": 32,
                    "auth_user_id": self.uid_emp_desactivado,
                    "nombre": "Marcos",
                    "apellido": "BajaSucursal",
                    "dni": "37300303",
                    "email": "emp_baja@gymtrack.test",
                    "telefono": "+54 9 11 5555-0303",
                    "estado": True,
                    "fecha_creacion": "2026-01-01T00:00:00Z",
                },
                # Empleado 33: recepcionista en Sucursal 101
                {
                    "id_empleado": 33,
                    "auth_user_id": self.uid_emp_recepcionista,
                    "nombre": "Sofía",
                    "apellido": "Recep",
                    "dni": "33100102",
                    "email": "recep@gymtrack.test",
                    "telefono": "+54 9 11 5555-0102",
                    "estado": True,
                    "fecha_creacion": "2026-01-01T00:00:00Z",
                },
            ],
            "empleado_sucursal": [
                # Empleado 30 en Sucursal 101 y 102 (Gym 10)
                {
                    "id_empleado_sucursal": 301,
                    "rol": "entrenador",
                    "id_empleado": 30,
                    "id_sucursal": 101,
                    "estado": True,
                    "fecha_asignacion": "2026-01-01T00:00:00Z",
                },
                {
                    "id_empleado_sucursal": 302,
                    "rol": "entrenador",
                    "id_empleado": 30,
                    "id_sucursal": 102,
                    "estado": True,
                    "fecha_asignacion": "2026-01-02T00:00:00Z",
                },
                # Empleado 31 en Gym 10 (Suc 101, entrenador) y en Gym 20 (Suc 201, administrador de sucursal)
                {
                    "id_empleado_sucursal": 303,
                    "rol": "entrenador",
                    "id_empleado": 31,
                    "id_sucursal": 101,
                    "estado": True,
                    "fecha_asignacion": "2026-01-01T00:00:00Z",
                },
                {
                    "id_empleado_sucursal": 304,
                    "rol": "administrador",
                    "id_empleado": 31,
                    "id_sucursal": 201,
                    "estado": True,
                    "fecha_asignacion": "2026-01-05T00:00:00Z",
                },
                # Empleado 32 con asignación desactivada en Suc 101
                {
                    "id_empleado_sucursal": 305,
                    "rol": "entrenador",
                    "id_empleado": 32,
                    "id_sucursal": 101,
                    "estado": False,
                    "fecha_asignacion": "2026-01-01T00:00:00Z",
                },
                # Empleado 33 recepcionista en Suc 101
                {
                    "id_empleado_sucursal": 306,
                    "rol": "recepcionista",
                    "id_empleado": 33,
                    "id_sucursal": 101,
                    "estado": True,
                    "fecha_asignacion": "2026-01-01T00:00:00Z",
                },
            ],
            "socio": [],
            "socio_gimnasio": [],
        }

    def _patch_env(self, state: Optional[Dict[str, List[Dict[str, Any]]]] = None):
        fake_db = FakeOrgSupabaseClient(state if state is not None else self._build_initial_state())

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

    # ==========================================================================
    # 1. DUEÑO QUE CONSULTA Y ADMINISTRA SUS PROPIOS GIMNASIOS Y SUCURSALES
    # ==========================================================================
    def test_01_dueno_consulta_y_crea_sus_propios_gimnasios_y_sucursales(self):
        token = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        p1, p2, fake_db = self._patch_env()
        with p1, p2:
            # 1. Listar gimnasios propios (Dueño 1 posee Gym 10 y Gym 11, no Gym 20)
            res = self.client.get("/api/gimnasios", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertEqual(body["total"], 2)
            self.assertEqual([g["id_gimnasio"] for g in body["gimnasios"]], [10, 11])
            for g in body["gimnasios"]:
                self.assertNotIn("direccion", g)
                self.assertNotIn("id_usuario", g)
                self.assertEqual(g["id_dueno"], 1)

            # 2. Crear un tercer gimnasio para el mismo dueño (multi-gimnasio por dueño permitido)
            res_create = self.client.post(
                "/api/gimnasios",
                json={"nombre": "Gimnasio Norte Boutique", "telefono": "1150001111", "email": "boutique@gymtrack.test"},
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(res_create.status_code, 201)
            new_gym = res_create.json()["gimnasio"]
            self.assertEqual(new_gym["id_dueno"], 1)
            self.assertTrue(new_gym["estado"])
            self.assertNotIn("direccion", new_gym)

            # 3. Crear una sucursal con su dirección dentro del gimnasio 10
            res_suc = self.client.post(
                "/api/gimnasios/10/sucursales",
                json={"direccion": "Av. Santa Fe 2100", "telefono": "1140009999", "descripcion": "Sede Recoleta"},
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(res_suc.status_code, 201)
            self.assertEqual(res_suc.json()["sucursal"]["id_gimnasio"], 10)
            self.assertEqual(res_suc.json()["sucursal"]["direccion"], "Av. Santa Fe 2100")

            # 4. Registrar un administrador organizacional mediante RPC oficial
            res_adm = self.client.post(
                "/api/gimnasios/10/administradores",
                json={
                    "nombre": "Lucía",
                    "apellido": "OrgAdmin",
                    "email": "lucia_admin@gymtrack.test",
                    "dni": "31222333",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(res_adm.status_code, 201)
            self.assertTrue(res_adm.json()["creado"])
            self.assertEqual(fake_db.rpc_calls[-1][0], "rpc_registrar_administrador_gimnasio")

    # ==========================================================================
    # 2. DUEÑO QUE INTENTA ACCEDER O MODIFICAR UN GIMNASIO AJENO
    # ==========================================================================
    def test_02_dueno_intenta_acceder_a_gimnasio_ajeno_403(self):
        token = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        p1, p2, _ = self._patch_env()
        with p1, p2:
            # Gym 20 pertenece a Dueño 2 -> Dueño 1 recibe 403 en todas las rutas de Gym 20 y Sucursal 201
            self.assertEqual(
                self.client.get("/api/gimnasios/20", headers={"Authorization": f"Bearer {token}"}).status_code,
                403,
            )
            self.assertEqual(
                self.client.put(
                    "/api/gimnasios/20",
                    json={"nombre": "Intento Hack"},
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.get(
                    "/api/gimnasios/20/sucursales",
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.post(
                    "/api/gimnasios/20/sucursales",
                    json={"direccion": "Calle Falsa 123"},
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.get(
                    "/api/administradores?id_gimnasio=20",
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.get(
                    "/api/empleados?id_gimnasio=20",
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code,
                403,
            )

    # ==========================================================================
    # 3. ADMINISTRADOR ORGANIZACIONAL CON VARIOS GIMNASIOS
    # ==========================================================================
    def test_03_administrador_con_varios_gimnasios(self):
        token = self._make_token(self.uid_admin_multigym, "admin_multi@gymtrack.test")
        p1, p2, _ = self._patch_env()
        with p1, p2:
            # 1. Consulta sus gimnasios asignados (Gym 10 y Gym 20, pero no Gym 11)
            res_gyms = self.client.get("/api/gimnasios", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(res_gyms.status_code, 200)
            self.assertEqual([g["id_gimnasio"] for g in res_gyms.json()["gimnasios"]], [10, 20])

            # 2. Puede crear sucursales tanto en Gym 10 como en Gym 20
            res_s10 = self.client.post(
                "/api/sucursales",
                json={"id_gimnasio": 10, "direccion": "Nueva Sede Norte Admin"},
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(res_s10.status_code, 201)

            res_s20 = self.client.post(
                "/api/sucursales",
                json={"id_gimnasio": 20, "direccion": "Nueva Sede Sur Admin"},
                headers={"Authorization": f"Bearer {token}", "X-Gimnasio-Id": "20"},
            )
            self.assertEqual(res_s20.status_code, 201)

            # 3. NO puede acceder a Gym 11 (no asignado) -> 403
            self.assertEqual(
                self.client.get("/api/gimnasios/11", headers={"Authorization": f"Bearer {token}"}).status_code,
                403,
            )

            # 4. NO puede crear un nuevo gimnasio ni registrar otros administradores organizacionales (exclusivo dueño)
            self.assertEqual(
                self.client.post(
                    "/api/gimnasios",
                    json={"nombre": "Gym Creado Por Admin"},
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.post(
                    "/api/administradores",
                    json={"id_gimnasio": 10, "nombre": "Otro", "apellido": "Admin", "email": "otro@gymtrack.test"},
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code,
                403,
            )

    # ==========================================================================
    # 4. ADMINISTRADOR SIN ASIGNACIÓN ACTIVA
    # ==========================================================================
    def test_04_administrador_sin_asignacion_activa_403(self):
        token = self._make_token(self.uid_admin_sin_asignacion, "admin_sin_gym@gymtrack.test")
        p1, p2, _ = self._patch_env()
        with p1, p2:
            for endpoint in (
                "/api/gimnasios",
                "/api/sucursales",
                "/api/administradores",
                "/api/administradores/mis-asignaciones",
                "/api/empleados",
            ):
                res = self.client.get(endpoint, headers={"Authorization": f"Bearer {token}"})
                self.assertEqual(res.status_code, 403, f"Se esperaba 403 en {endpoint}")

            res_post = self.client.post(
                "/api/sucursales",
                json={"id_gimnasio": 10, "direccion": "Intento Sin Asignacion"},
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(res_post.status_code, 403)

    # ==========================================================================
    # 5. EMPLEADO ASIGNADO A VARIAS SUCURSALES
    # ==========================================================================
    def test_05_empleado_asignado_a_varias_sucursales(self):
        token = self._make_token(self.uid_emp_multisuc, "emp_multisuc@gymtrack.test")
        p1, p2, _ = self._patch_env()
        with p1, p2:
            # Empleado 30 tiene asignación activa en Sucursal 101 y Sucursal 102 (Gym 10), pero NO en Sucursal 111 ni 201
            res_sucs = self.client.get("/api/sucursales", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(res_sucs.status_code, 200)
            suc_ids = [s["id_sucursal"] for s in res_sucs.json()["sucursales"]]
            self.assertEqual(suc_ids, [101, 102])

            self.assertEqual(
                self.client.get("/api/sucursales/101", headers={"Authorization": f"Bearer {token}"}).status_code,
                200,
            )
            self.assertEqual(
                self.client.get("/api/sucursales/102", headers={"Authorization": f"Bearer {token}"}).status_code,
                200,
            )
            # No puede acceder a Sucursal 111 ni 201 por el simple hecho de tener identidad de empleado -> 403
            self.assertEqual(
                self.client.get("/api/sucursales/111", headers={"Authorization": f"Bearer {token}"}).status_code,
                403,
            )
            self.assertEqual(
                self.client.get("/api/sucursales/201", headers={"Authorization": f"Bearer {token}"}).status_code,
                403,
            )

    # ==========================================================================
    # 6. EMPLEADO QUE TRABAJA EN GIMNASIOS DISTINTOS
    # ==========================================================================
    def test_06_empleado_que_trabaja_en_gimnasios_distintos(self):
        token = self._make_token(self.uid_emp_multigym, "emp_multigym@gymtrack.test")
        p1, p2, _ = self._patch_env()
        with p1, p2:
            # Empleado 31 es 'entrenador' en Sucursal 101 (Gym 10) y 'administrador' de sucursal en Sucursal 201 (Gym 20)
            res_mis = self.client.get("/api/empleados/mis-asignaciones", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(res_mis.status_code, 200)
            body = res_mis.json()
            self.assertNotIn("id_gimnasio", body["empleado"])
            self.assertEqual(sorted([g["id_gimnasio"] for g in body["gimnasios"]]), [10, 20])
            self.assertEqual(body["roles_empleado_por_sucursal"], {"101": "entrenador", "201": "administrador"})

            # En Gym 20 (donde es 'administrador' de sucursal) PUEDE consultar empleados de Gym 20
            res_emp_g20 = self.client.get(
                "/api/empleados?id_gimnasio=20",
                headers={"Authorization": f"Bearer {token}", "X-Gimnasio-Id": "20"},
            )
            self.assertEqual(res_emp_g20.status_code, 200)

            # En Gym 10 (donde solo es 'entrenador') NO puede consultar el padrón de empleados de Gym 10 -> 403
            res_emp_g10 = self.client.get(
                "/api/empleados?id_gimnasio=10",
                headers={"Authorization": f"Bearer {token}", "X-Gimnasio-Id": "10"},
            )
            self.assertEqual(res_emp_g10.status_code, 403)

            # Y aunque sea 'administrador' de sucursal en Gym 20, NO es administrador organizacional:
            # no puede crear sucursales ni consultar administradores organizacionales -> 403
            self.assertEqual(
                self.client.post(
                    "/api/sucursales",
                    json={"id_gimnasio": 20, "direccion": "Intento Admin Sucursal"},
                    headers={"Authorization": f"Bearer {token}", "X-Gimnasio-Id": "20"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.get(
                    "/api/administradores?id_gimnasio=20",
                    headers={"Authorization": f"Bearer {token}", "X-Gimnasio-Id": "20"},
                ).status_code,
                403,
            )

    # ==========================================================================
    # 7. EMPLEADO CON ASIGNACIÓN DESACTIVADA Y BAJA SIN ELIMINAR IDENTIDAD
    # ==========================================================================
    def test_07_empleado_con_asignacion_desactivada_y_baja_conserva_identidad(self):
        p1, p2, fake_db = self._patch_env()
        with p1, p2:
            # A) Empleado 32 tiene su asignación en Sucursal 101 con estado=False -> 403 en todos los endpoints
            token_inactivo = self._make_token(self.uid_emp_desactivado, "emp_baja@gymtrack.test")
            self.assertEqual(
                self.client.get("/api/gimnasios", headers={"Authorization": f"Bearer {token_inactivo}"}).status_code,
                403,
            )
            self.assertEqual(
                self.client.get("/api/sucursales/101", headers={"Authorization": f"Bearer {token_inactivo}"}).status_code,
                403,
            )
            self.assertEqual(
                self.client.get(
                    "/api/empleados/mis-asignaciones",
                    headers={"Authorization": f"Bearer {token_inactivo}"},
                ).status_code,
                403,
            )

            # B) Dueño 1 desactiva la asignación 302 (Empleado 30 en Sucursal 102) mediante DELETE
            token_dueno_1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
            res_del = self.client.delete(
                "/api/empleados/asignaciones/302",
                headers={"Authorization": f"Bearer {token_dueno_1}"},
            )
            self.assertEqual(res_del.status_code, 200)
            self.assertFalse(res_del.json()["empleado_sucursal"]["estado"])

            # La identidad personal del Empleado 30 sigue existiendo en public.empleado
            emp_30 = next((e for e in fake_db.state["empleado"] if e["id_empleado"] == 30), None)
            self.assertIsNotNone(emp_30)
            self.assertTrue(emp_30["estado"])

            # Ahora Empleado 30 ya NO puede acceder a Sucursal 102 (403), pero sigue accediendo a Sucursal 101 (200)
            token_emp_30 = self._make_token(self.uid_emp_multisuc, "emp_multisuc@gymtrack.test")
            self.assertEqual(
                self.client.get("/api/sucursales/102", headers={"Authorization": f"Bearer {token_emp_30}"}).status_code,
                403,
            )
            self.assertEqual(
                self.client.get("/api/sucursales/101", headers={"Authorization": f"Bearer {token_emp_30}"}).status_code,
                200,
            )

            # C) Dueño 1 intenta desactivar globalmente empleado.estado=False del Empleado 31
            #    que trabaja activamente en Gym 20 (Sucursal 201) -> rechazado con 403
            res_global_disable = self.client.put(
                "/api/empleados/31",
                json={"estado": False},
                headers={"Authorization": f"Bearer {token_dueno_1}"},
            )
            self.assertEqual(res_global_disable.status_code, 403)

            # D) Un empleado con identidad globalmente desactivada (empleado.estado=False)
            #    no puede recibir nuevas asignaciones ni reactivar asignaciones en sucursal
            emp_32 = next((e for e in fake_db.state["empleado"] if e["id_empleado"] == 32), None)
            emp_32["estado"] = False
            res_assign_disabled = self.client.post(
                "/api/sucursales/102/empleados",
                json={"rol": "entrenador", "id_empleado": 32},
                headers={"Authorization": f"Bearer {token_dueno_1}"},
            )
            self.assertEqual(res_assign_disabled.status_code, 400)

            res_activate_disabled = self.client.patch(
                "/api/empleados/asignaciones/305/estado",
                json={"estado": True},
                headers={"Authorization": f"Bearer {token_dueno_1}"},
            )
            self.assertEqual(res_activate_disabled.status_code, 400)

    # ==========================================================================
    # 8. RECEPCIONISTA INTENTANDO OPERACIONES EXCLUSIVAS DE ENTRENADOR O ADMINISTRADOR
    # ==========================================================================
    def test_08_recepcionista_bloqueado_en_operaciones_de_administrador_o_entrenador(self):
        token_recep = self._make_token(self.uid_emp_recepcionista, "recep@gymtrack.test")
        p1, p2, _ = self._patch_env()
        with p1, p2:
            # Recepcionista puede ver su propia sucursal 101
            self.assertEqual(
                self.client.get("/api/sucursales/101", headers={"Authorization": f"Bearer {token_recep}"}).status_code,
                200,
            )

            # Recepcionista NO puede listar empleados, registrar empleados, modificar asignaciones,
            # crear/modificar sucursales ni consultar/registrar administradores -> 403 en todos
            self.assertEqual(
                self.client.get("/api/empleados", headers={"Authorization": f"Bearer {token_recep}"}).status_code,
                403,
            )
            self.assertEqual(
                self.client.post(
                    "/api/empleados",
                    json={"id_sucursal": 101, "rol": "entrenador", "email": "nuevo@gymtrack.test", "nombre": "A", "apellido": "B"},
                    headers={"Authorization": f"Bearer {token_recep}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.put(
                    "/api/empleados/asignaciones/301",
                    json={"rol": "administrador"},
                    headers={"Authorization": f"Bearer {token_recep}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.post(
                    "/api/sucursales",
                    json={"id_gimnasio": 10, "direccion": "Sede Recep"},
                    headers={"Authorization": f"Bearer {token_recep}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.put(
                    "/api/sucursales/101",
                    json={"direccion": "Cambio Recep"},
                    headers={"Authorization": f"Bearer {token_recep}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.get("/api/administradores", headers={"Authorization": f"Bearer {token_recep}"}).status_code,
                403,
            )

    # ==========================================================================
    # 9. REUTILIZACIÓN DE EMPLEADO EXISTENTE SIN DUPLICAR SU IDENTIDAD
    # ==========================================================================
    def test_09_reutilizacion_de_empleado_existente_entre_gimnasios_sin_duplicar_identidad(self):
        token_dueno_2 = self._make_token(self.uid_dueno_2, "dueno2@gymtrack.test")
        p1, p2, fake_db = self._patch_env()
        with p1, p2:
            total_empleados_antes = len(fake_db.state["empleado"])

            # Dueño 2 incorpora en Sucursal 201 (Gym 20) al Empleado 30 (que trabajaba en Gym 10) usando su email
            res_reuse = self.client.post(
                "/api/sucursales/201/empleados",
                json={
                    "rol": "entrenador",
                    "email": "emp_multisuc@gymtrack.test",
                },
                headers={"Authorization": f"Bearer {token_dueno_2}"},
            )
            self.assertEqual(res_reuse.status_code, 201)
            body = res_reuse.json()
            self.assertFalse(body["creado"])
            self.assertEqual(body["empleado"]["id_empleado"], 30)
            self.assertEqual(body["empleado"]["auth_user_id"], self.uid_emp_multisuc)
            self.assertNotIn("id_gimnasio", body["empleado"])
            self.assertEqual(body["empleado_sucursal"]["id_sucursal"], 201)
            self.assertEqual(body["empleado_sucursal"]["rol"], "entrenador")
            self.assertTrue(body["empleado_sucursal"]["estado"])

            # Verificar que se invocó la RPC oficial y que NO se duplicó la fila en public.empleado
            self.assertEqual(fake_db.rpc_calls[-1][0], "rpc_registrar_empleado_sucursal")
            self.assertEqual(len(fake_db.state["empleado"]), total_empleados_antes)

            # Prevención de asociaciones activas duplicadas en la misma sucursal (409 Conflict)
            res_dup_emp = self.client.post(
                "/api/sucursales/201/empleados",
                json={"rol": "entrenador", "email": "emp_multisuc@gymtrack.test"},
                headers={"Authorization": f"Bearer {token_dueno_2}"},
            )
            self.assertEqual(res_dup_emp.status_code, 409)

            # Prevención de asociaciones activas duplicadas de administrador en el mismo gimnasio (409 Conflict)
            res_dup_adm = self.client.post(
                "/api/gimnasios/20/administradores",
                json={
                    "nombre": "Diego",
                    "apellido": "MultiAdmin",
                    "email": "admin_multi@gymtrack.test",
                    "dni": "30111222",
                },
                headers={"Authorization": f"Bearer {token_dueno_2}"},
            )
            self.assertEqual(res_dup_adm.status_code, 409)

    # ==========================================================================
    # 10. INTENTOS DE SUPLANTACIÓN DE id_gimnasio, id_sucursal O ROL
    # ==========================================================================
    def test_10_intentos_de_suplantacion_id_gimnasio_id_sucursal_o_rol_403(self):
        token_dueno_1 = self._make_token(self.uid_dueno_1, "dueno1@gymtrack.test")
        p1, p2, _ = self._patch_env()
        with p1, p2:
            # 1. Suplantar id_dueno al crear o actualizar un gimnasio -> 403
            self.assertEqual(
                self.client.post(
                    "/api/gimnasios",
                    json={"nombre": "Gym Spoof", "id_dueno": 2},
                    headers={"Authorization": f"Bearer {token_dueno_1}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.put(
                    "/api/gimnasios/10",
                    json={"nombre": "Transfer Spoof", "id_dueno": 2},
                    headers={"Authorization": f"Bearer {token_dueno_1}"},
                ).status_code,
                403,
            )

            # 2. Enviar X-Gimnasio-Id: 10 (propio) en cabecera intentando operar en ruta /api/gimnasios/20 (ajeno) -> 403
            self.assertEqual(
                self.client.get(
                    "/api/gimnasios/20",
                    headers={"Authorization": f"Bearer {token_dueno_1}", "X-Gimnasio-Id": "10"},
                ).status_code,
                403,
            )

            # 3. Inconsistencia entre id_gimnasio de ruta (10) e id_gimnasio del body (20) -> 403
            self.assertEqual(
                self.client.post(
                    "/api/gimnasios/10/sucursales",
                    json={"id_gimnasio": 20, "direccion": "Spoof Gym Body"},
                    headers={"Authorization": f"Bearer {token_dueno_1}"},
                ).status_code,
                403,
            )

            # 4. Intentar transferir una sucursal existente a otro gimnasio -> 403
            self.assertEqual(
                self.client.put(
                    "/api/sucursales/101",
                    json={"id_gimnasio": 11, "direccion": "Mover Sucursal"},
                    headers={"Authorization": f"Bearer {token_dueno_1}"},
                ).status_code,
                403,
            )

            # 5. Dueño 1 intentando registrar empleado en Sucursal 201 (de Dueño 2) -> 403
            self.assertEqual(
                self.client.post(
                    "/api/empleados",
                    json={"id_sucursal": 201, "rol": "entrenador", "id_empleado": 30},
                    headers={"Authorization": f"Bearer {token_dueno_1}", "X-Gimnasio-Id": "10"},
                ).status_code,
                403,
            )

            # 6. Intentar transferir una asignación empleado_sucursal existente a otra sucursal o empleado -> 403
            self.assertEqual(
                self.client.put(
                    "/api/empleados/asignaciones/301",
                    json={"id_sucursal": 201, "rol": "entrenador"},
                    headers={"Authorization": f"Bearer {token_dueno_1}"},
                ).status_code,
                403,
            )

            # 7. Empleado recepcionista intentando suplantar rol 'dueno' ('due%C3%B1o') o 'empleado:administrador' vía X-Active-Role -> 403
            token_recep = self._make_token(self.uid_emp_recepcionista, "recep@gymtrack.test")
            self.assertEqual(
                self.client.get(
                    "/api/empleados",
                    headers={"Authorization": f"Bearer {token_recep}", "X-Active-Role": "dueno"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.get(
                    "/api/empleados",
                    headers={"Authorization": f"Bearer {token_recep}", "X-Active-Role": "due%C3%B1o"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.get(
                    "/api/empleados",
                    headers={"Authorization": f"Bearer {token_recep}", "X-Active-Role": "empleado:administrador"},
                ).status_code,
                403,
            )

            # 8. Intento de modificar o desactivar una asignación ajena (Dueño 1 sobre asignaciones de Gym 20) -> 403
            self.assertEqual(
                self.client.put(
                    "/api/empleados/asignaciones/304",
                    json={"rol": "recepcionista"},
                    headers={"Authorization": f"Bearer {token_dueno_1}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.delete(
                    "/api/empleados/asignaciones/304",
                    headers={"Authorization": f"Bearer {token_dueno_1}"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.patch(
                    "/api/administradores/asignaciones/502/estado",
                    json={"estado": False},
                    headers={"Authorization": f"Bearer {token_dueno_1}"},
                ).status_code,
                403,
            )

            # 9. Intento de cruzar gimnasio y sucursal incluso entre dos gimnasios del mismo dueño (Gym 10 vs Sucursal 111 de Gym 11) -> 403
            self.assertEqual(
                self.client.get(
                    "/api/sucursales/111",
                    headers={"Authorization": f"Bearer {token_dueno_1}", "X-Gimnasio-Id": "10"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.post(
                    "/api/empleados",
                    json={"id_gimnasio": 10, "id_sucursal": 111, "rol": "entrenador", "id_empleado": 30},
                    headers={"Authorization": f"Bearer {token_dueno_1}"},
                ).status_code,
                403,
            )


if __name__ == "__main__":
    unittest.main()
