import asyncio
import copy
import hashlib
import threading
import time
import urllib.parse
from typing import Optional, Dict, Any, List, Set, Sequence, Tuple, Callable
from fastapi import Request, HTTPException, status, Depends
from supabase import Client

from app.config import create_supabase_client_for_token
from app.core.security import verify_supabase_access_token
from app.services.utils import get_data

# Permisos operativos por sub-rol de empleado en sucursal (tabla public.empleado_sucursal)
PERMISOS_POR_ROL_EMPLEADO: Dict[str, List[str]] = {
    "administrador": [
        "ver_dashboard",
        "gestionar_socios",
        "gestionar_planes",
        "gestionar_membresias",
        "gestionar_pagos",
        "gestionar_asistencias",
        "gestionar_ejercicios",
        "gestionar_rutinas",
        "ver_empleados",
        "ver_sucursales",
    ],
    "recepcionista": [
        "ver_dashboard",
        "gestionar_socios",
        "gestionar_membresias",
        "gestionar_pagos",
        "gestionar_asistencias",
        "ver_sucursales",
    ],
    "entrenador": [
        "ver_dashboard",
        "ver_socios",
        "ver_asistencias",
        "gestionar_ejercicios",
        "gestionar_rutinas",
        "ver_sucursales",
    ],
}

# Permisos organizacionales de un administrador de gimnasio (tabla public.administrador_gimnasio)
PERMISOS_ADMINISTRADOR_GIMNASIO: List[str] = [
    "ver_dashboard",
    "gestionar_sucursales",
    "gestionar_empleados",
    "gestionar_socios",
    "gestionar_planes",
    "gestionar_membresias",
    "gestionar_pagos",
    "gestionar_asistencias",
    "gestionar_ejercicios",
    "gestionar_rutinas",
    "ver_empleados",
    "ver_sucursales",
]

ROLE_ALIASES: Dict[str, str] = {
    "dueno": "dueño",
    "dueño": "dueño",
    "owner": "dueño",
    "admin": "administrador",
    "administrador": "administrador",
    "administrador_gimnasio": "administrador",
    "empleado": "empleado",
    "staff": "empleado",
    "socio": "socio",
}

EMPLOYEE_SUBROLES: Set[str] = {"administrador", "recepcionista", "entrenador"}


def normalize_role_name(role_value: Optional[str]) -> Optional[str]:
    """Normaliza el nombre de un rol de negocio ('dueno' / 'due%C3%B1o' -> 'dueño', etc.)."""
    if not role_value or not isinstance(role_value, str):
        return None
    clean = urllib.parse.unquote(role_value).strip().lower()
    if not clean:
        return None
    if clean.startswith("empleado:"):
        sub = clean.split(":", 1)[1].strip()
        return f"empleado:{sub}" if sub in EMPLOYEE_SUBROLES else None
    return ROLE_ALIASES.get(clean)


def _parse_positive_int(value: Any) -> Optional[int]:
    """Convierte un valor a entero positivo, o devuelve None si no es válido."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
        return parsed if parsed > 0 else None
    except (ValueError, TypeError):
        return None


def _extract_requested_gym_id(request: Request) -> Optional[int]:
    """Extrae el id_gimnasio solicitado en headers, path params o query params."""
    for candidate in (
        request.headers.get("X-Gimnasio-Id") or request.headers.get("x-gimnasio-id"),
        request.path_params.get("id_gimnasio") or request.path_params.get("gym_id"),
        request.query_params.get("id_gimnasio"),
    ):
        parsed = _parse_positive_int(candidate)
        if parsed is not None:
            return parsed
    return None


def _extract_requested_sucursal_id(request: Request) -> Optional[int]:
    """Extrae el id_sucursal solicitado en headers, path params o query params."""
    for candidate in (
        request.headers.get("X-Sucursal-Id") or request.headers.get("x-sucursal-id"),
        request.path_params.get("id_sucursal") or request.path_params.get("sucursal_id"),
        request.query_params.get("id_sucursal"),
    ):
        parsed = _parse_positive_int(candidate)
        if parsed is not None:
            return parsed
    return None


def _extract_requested_role(request: Request) -> Optional[str]:
    """Extrae el rol activo solicitado explícitamente por el cliente en la cabecera X-Active-Role."""
    raw = request.headers.get("X-Active-Role") or request.headers.get("x-active-role")
    if not raw or not raw.strip():
        return None
    return raw.strip().lower()


def get_token_from_header(request: Request) -> Optional[str]:
    """Extrae el token Bearer del encabezado Authorization."""
    auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not auth_header:
        return None
    parts = auth_header.strip().split(" ")
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


async def get_current_auth_user(request: Request) -> Dict[str, Any]:
    """
    Dependency de Nivel 1 (Identidad Auth):
    Extrae y verifica criptográficamente el access token emitido por Supabase Auth.
    Devuelve la identidad autenticada (`auth_user_id`, `email`, `claims`, `access_token`)
    separada de cualquier perfil de negocio.
    """
    auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not auth_header or not auth_header.strip().lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Acceso denegado. No se proporcionó un token de autenticación válido (Bearer token).",
        )

    parts = auth_header.strip().split(" ", 1)
    token = parts[1].strip() if len(parts) == 2 else ""
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token no encontrado en la cabecera de autorización.",
        )

    identity = verify_supabase_access_token(token)
    request.state.auth_user = identity
    return identity


async def get_user_supabase_client(
    request: Request,
    auth_user: Dict[str, Any] = Depends(get_current_auth_user),
) -> Client:
    """
    Dependency que construye el cliente Supabase por request autenticado con el Bearer token
    del usuario, garantizando que todas las operaciones respeten RLS y `auth.uid()`.
    """
    existing = getattr(request.state, "supabase_client", None)
    if existing is not None:
        return existing

    try:
        client = create_supabase_client_for_token(auth_user["access_token"])
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error al inicializar el cliente de sesión de Supabase: {exc}",
        ) from exc

    request.state.supabase_client = client
    return client


def _safe_query_single(db: Client, table: str, select_clause: str, eq_col: str, eq_val: Any) -> Optional[Dict[str, Any]]:
    """Ejecuta una consulta `.maybe_single()` de forma segura, devolviendo un dict o None."""
    res = (
        db.from_(table)
        .select(select_clause)
        .eq(eq_col, eq_val)
        .maybe_single()
        .execute()
    )
    data = get_data(res)
    return data if isinstance(data, dict) else None


def _safe_query_list(
    db: Client,
    table: str,
    select_clause: str,
    eq_col: Optional[str] = None,
    eq_val: Any = None,
    in_col: Optional[str] = None,
    in_vals: Optional[Sequence[Any]] = None,
    order_col: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Ejecuta una consulta de lista de forma segura."""
    if in_col is not None and not in_vals:
        return []
    query = db.from_(table).select(select_clause)
    if eq_col is not None:
        query = query.eq(eq_col, eq_val)
    if in_col is not None and in_vals is not None:
        query = query.in_(in_col, list(in_vals))
    if order_col is not None:
        query = query.order(order_col, desc=False)
    res = query.execute()
    data = get_data(res)
    return data if isinstance(data, list) else []


_PROFILE_CACHE_TTL_SECONDS = 8.0
_PROFILE_CACHE_MAX_ENTRIES = 256
_PROFILE_CACHE_LOCK = threading.Lock()
_PROFILE_RAW_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_PROFILE_INFLIGHT_LOCKS: Dict[Tuple[str, int], asyncio.Lock] = {}


def _is_real_supabase_client(db: Any) -> bool:
    """Detecta si `db` es un cliente real de Supabase y no un doble/mock de pruebas."""
    return db is not None and type(db).__module__.startswith("supabase")


async def run_db_parallel(
    db: Client,
    access_token: Optional[str],
    tasks: Sequence[Callable[[Client], Any]],
) -> List[Any]:
    """
    Ejecuta operaciones de lectura PostgREST en paralelo usando hilos dedicados y clientes
    aislados cuando `db` es un cliente real de Supabase, evitando bloquear el event loop.
    En tests unitarios con mocks/fakes (`_is_real_supabase_client(db) == False`), ejecuta
    secuencialmente de forma síncrona para mantener compatibilidad determinística.
    """
    if not tasks:
        return []
    if not _is_real_supabase_client(db):
        return [fn(db) for fn in tasks]
    if len(tasks) == 1 or not access_token:
        results = []
        for fn in tasks:
            results.append(await asyncio.to_thread(fn, db))
        return results
    return list(
        await asyncio.gather(
            *[
                asyncio.to_thread(fn, create_supabase_client_for_token(access_token))
                for fn in tasks
            ]
        )
    )


def _build_profile_cache_key(auth_user: Dict[str, Any]) -> Optional[str]:
    uid = str(auth_user.get("auth_user_id") or "").strip()
    token = str(auth_user.get("access_token") or "").strip()
    if not uid or not token:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    return f"{uid}:{token_hash}"


def invalidate_user_context_cache(auth_user_id: Optional[str] = None) -> None:
    """Invalida la caché efímera de perfiles de negocio del usuario o de todos si `auth_user_id` es None."""
    with _PROFILE_CACHE_LOCK:
        if not auth_user_id:
            _PROFILE_RAW_CACHE.clear()
            return
        prefix = f"{auth_user_id}:"
        for key in [k for k in _PROFILE_RAW_CACHE if k.startswith(prefix)]:
            _PROFILE_RAW_CACHE.pop(key, None)


def _get_cached_profile_bundle(cache_key: str) -> Optional[Dict[str, Any]]:
    now = time.monotonic()
    with _PROFILE_CACHE_LOCK:
        entry = _PROFILE_RAW_CACHE.get(cache_key)
        if not entry:
            return None
        expires_at, payload = entry
        if now >= expires_at:
            _PROFILE_RAW_CACHE.pop(cache_key, None)
            return None
        return copy.deepcopy(payload)


def _set_cached_profile_bundle(cache_key: str, payload: Dict[str, Any]) -> None:
    now = time.monotonic()
    with _PROFILE_CACHE_LOCK:
        if len(_PROFILE_RAW_CACHE) >= _PROFILE_CACHE_MAX_ENTRIES:
            expired = [k for k, (exp, _) in _PROFILE_RAW_CACHE.items() if now >= exp]
            for k in expired:
                _PROFILE_RAW_CACHE.pop(k, None)
            if len(_PROFILE_RAW_CACHE) >= _PROFILE_CACHE_MAX_ENTRIES:
                _PROFILE_RAW_CACHE.pop(next(iter(_PROFILE_RAW_CACHE)), None)
        _PROFILE_RAW_CACHE[cache_key] = (
            now + _PROFILE_CACHE_TTL_SECONDS,
            copy.deepcopy(payload),
        )


def _get_inflight_async_lock(cache_key: str) -> asyncio.Lock:
    loop_id = id(asyncio.get_running_loop())
    lock_key = (cache_key, loop_id)
    with _PROFILE_CACHE_LOCK:
        lock = _PROFILE_INFLIGHT_LOCKS.get(lock_key)
        if lock is None:
            if len(_PROFILE_INFLIGHT_LOCKS) > 512:
                _PROFILE_INFLIGHT_LOCKS.clear()
            lock = asyncio.Lock()
            _PROFILE_INFLIGHT_LOCKS[lock_key] = lock
        return lock


def _query_single_with_token(
    access_token: str,
    table: str,
    select_clause: str,
    eq_col: str,
    eq_val: Any,
) -> Optional[Dict[str, Any]]:
    """Ejecuta `_safe_query_single` con cliente aislado para llamadas paralelas seguras por hilo."""
    worker_db = create_supabase_client_for_token(access_token)
    return _safe_query_single(worker_db, table, select_clause, eq_col, eq_val)


def _query_list_with_token(
    access_token: str,
    table: str,
    select_clause: str,
    eq_col: Optional[str] = None,
    eq_val: Any = None,
    in_col: Optional[str] = None,
    in_vals: Optional[Sequence[Any]] = None,
    order_col: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Ejecuta `_safe_query_list` con cliente aislado para llamadas paralelas seguras por hilo."""
    worker_db = create_supabase_client_for_token(access_token)
    return _safe_query_list(
        worker_db,
        table,
        select_clause,
        eq_col=eq_col,
        eq_val=eq_val,
        in_col=in_col,
        in_vals=in_vals,
        order_col=order_col,
    )


async def resolver_contexto_negocio(
    db: Client,
    auth_user: Dict[str, Any],
    requested_gym_id: Optional[int] = None,
    requested_sucursal_id: Optional[int] = None,
    requested_role: Optional[str] = None,
    use_cache: bool = False,
    store_cache: bool = False,
) -> Dict[str, Any]:
    """
    Resuelve desde la base de datos todos los perfiles de negocio asociados al `auth_user_id`
    (`dueno`, `administrador` + `administrador_gimnasio`, `empleado` + `empleado_sucursal`,
    `socio` + `socio_gimnasio`) y valida estrictamente el contexto de rol, gimnasio y sucursal.

    Reglas de seguridad:
    - No confía en roles ni IDs enviados por el frontend sin comprobar su relación activa en BD.
    - Soporta usuarios con múltiples perfiles y asociaciones multi-gimnasio / multi-sucursal.
    - Rechaza explícitamente intentos de suplantación de rol (403), cuentas sin perfil (403),
      perfiles deshabilitados (403) y accesos a gimnasios o sucursales no autorizados (403).
    """
    uid = auth_user["auth_user_id"]
    is_real_client = _is_real_supabase_client(db)
    cache_key = _build_profile_cache_key(auth_user) if is_real_client else None

    _SELECT_DUENO = "id_dueno, auth_user_id, nombre, apellido, email, fecha_creacion"
    _SELECT_ADMIN = (
        "id_administrador, auth_user_id, nombre, apellido, dni, email, telefono, estado, fecha_creacion, "
        "administrador_gimnasio("
        "id_administrador_gimnasio, id_administrador, id_gimnasio, fecha_asignacion, estado, "
        "gimnasio(id_gimnasio, nombre, telefono, email, fecha_registro, estado, id_dueno)"
        ")"
    )
    _SELECT_EMPLEADO = (
        "id_empleado, auth_user_id, nombre, apellido, dni, email, telefono, estado, fecha_creacion, "
        "empleado_sucursal("
        "id_empleado_sucursal, rol, id_empleado, id_sucursal, estado, fecha_asignacion, "
        "sucursal("
        "id_sucursal, direccion, telefono, descripcion, fecha_registro, estado, id_gimnasio, "
        "gimnasio(id_gimnasio, nombre, telefono, email, fecha_registro, estado, id_dueno)"
        ")"
        ")"
    )
    _SELECT_SOCIO = (
        "id_socio, auth_user_id, dni, telefono, fecha_alta, estado, nombre, apellido, email, "
        "socio_gimnasio("
        "id_socio_gimnasio, id_socio, id_gimnasio, fecha_alta, estado, creado_en, "
        "gimnasio(id_gimnasio, nombre, telefono, email, fecha_registro, estado, id_dueno), "
        "membresia_socio("
        "id_membresia_socio, id_socio_gimnasio, id_membresia_gimnasio, id_gimnasio, "
        "precio, duracion_dias_contratados, limite_asistencias_semanales, limite_asistencias_totales, "
        "fecha_inicio, fecha_vencimiento, estado"
        ")"
        ")"
    )

    cached_bundle: Optional[Dict[str, Any]] = None
    if use_cache and cache_key:
        cached_bundle = _get_cached_profile_bundle(cache_key)

    async def _fetch_profiles_live() -> Tuple[
        Optional[Dict[str, Any]],
        Optional[Dict[str, Any]],
        Optional[Dict[str, Any]],
        Optional[Dict[str, Any]],
    ]:
        if is_real_client:
            token = auth_user["access_token"]
            return await asyncio.gather(
                asyncio.to_thread(_safe_query_single, db, "dueno", _SELECT_DUENO, "auth_user_id", uid),
                asyncio.to_thread(_query_single_with_token, token, "administrador", _SELECT_ADMIN, "auth_user_id", uid),
                asyncio.to_thread(_query_single_with_token, token, "empleado", _SELECT_EMPLEADO, "auth_user_id", uid),
                asyncio.to_thread(_query_single_with_token, token, "socio", _SELECT_SOCIO, "auth_user_id", uid),
            )
        return (
            _safe_query_single(db, "dueno", _SELECT_DUENO, "auth_user_id", uid),
            _safe_query_single(db, "administrador", _SELECT_ADMIN, "auth_user_id", uid),
            _safe_query_single(db, "empleado", _SELECT_EMPLEADO, "auth_user_id", uid),
            _safe_query_single(db, "socio", _SELECT_SOCIO, "auth_user_id", uid),
        )

    async def _resolve_bundle() -> Dict[str, Any]:
        dueno, administrador, empleado, socio = await _fetch_profiles_live()

        tiene_algun_registro = any(p is not None for p in (dueno, administrador, empleado, socio))
        if not tiene_algun_registro:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Tu cuenta autenticada no posee ningún perfil de negocio vinculado en GymTrack.",
            )

        perfiles_disp: List[str] = []
        perfiles_inact: Set[str] = set()

        if dueno is not None:
            perfiles_disp.append("dueño")

        if administrador is not None:
            raw_ag_check = administrador.get("administrador_gimnasio") or []
            if not isinstance(raw_ag_check, list):
                raw_ag_check = [raw_ag_check] if raw_ag_check else []
            tiene_ag_activa = any(
                isinstance(ag, dict) and ag.get("estado") is not False and ag.get("id_gimnasio") is not None
                for ag in raw_ag_check
            )
            if administrador.get("estado") is False or not tiene_ag_activa:
                perfiles_inact.add("administrador")
            else:
                perfiles_disp.append("administrador")

        if empleado is not None:
            raw_es_check = empleado.get("empleado_sucursal") or []
            if not isinstance(raw_es_check, list):
                raw_es_check = [raw_es_check] if raw_es_check else []
            tiene_es_activa = any(
                isinstance(es, dict) and es.get("estado") is not False and es.get("id_sucursal") is not None
                for es in raw_es_check
            )
            if empleado.get("estado") is False or not tiene_es_activa:
                perfiles_inact.add("empleado")
            else:
                perfiles_disp.append("empleado")

        if socio is not None:
            if socio.get("estado") is False:
                perfiles_inact.add("socio")
            else:
                perfiles_disp.append("socio")

        gyms_dueno: List[Dict[str, Any]] = []
        sucursales_dueno: List[Dict[str, Any]] = []
        sucursales_admin: List[Dict[str, Any]] = []
        sucursales_socio: List[Dict[str, Any]] = []

        # Preparar IDs de admin y socio para consultar sus sucursales en paralelo con las de dueño
        admin_asig: List[Dict[str, Any]] = []
        gym_ids_admin: List[int] = []
        if "administrador" in perfiles_disp and administrador is not None:
            raw_ag = administrador.get("administrador_gimnasio") or []
            if not isinstance(raw_ag, list):
                raw_ag = [raw_ag] if raw_ag else []
            admin_asig = [
                ag for ag in raw_ag
                if isinstance(ag, dict) and ag.get("estado") is not False and ag.get("id_gimnasio") is not None
            ]
            for ag in admin_asig:
                gid = int(ag["id_gimnasio"])
                if gid not in gym_ids_admin:
                    gym_ids_admin.append(gid)

        gym_ids_socio_activos: Set[int] = set()
        if "socio" in perfiles_disp and socio is not None:
            raw_sg = socio.get("socio_gimnasio") or []
            if not isinstance(raw_sg, list):
                raw_sg = [raw_sg] if raw_sg else []
            for sg in raw_sg:
                if isinstance(sg, dict) and sg.get("id_gimnasio") is not None and sg.get("estado") is not False:
                    gym_ids_socio_activos.add(int(sg["id_gimnasio"]))

        def _fetch_dueno_chain() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
            if "dueño" not in perfiles_disp or dueno is None:
                return [], []
            g_list = _safe_query_list(
                db,
                "gimnasio",
                "id_gimnasio, nombre, telefono, email, fecha_registro, estado, id_dueno",
                eq_col="id_dueno",
                eq_val=dueno["id_dueno"],
                order_col="id_gimnasio",
            )
            g_ids = [int(g["id_gimnasio"]) for g in g_list if g.get("id_gimnasio") is not None]
            s_list = (
                _safe_query_list(
                    db,
                    "sucursal",
                    "id_sucursal, direccion, telefono, descripcion, fecha_registro, estado, id_gimnasio",
                    in_col="id_gimnasio",
                    in_vals=g_ids,
                    order_col="id_sucursal",
                )
                if g_ids
                else []
            )
            return g_list, s_list

        if is_real_client:
            token = auth_user["access_token"]
            tasks = [asyncio.to_thread(_fetch_dueno_chain)]
            if gym_ids_admin:
                tasks.append(
                    asyncio.to_thread(
                        _query_list_with_token,
                        token,
                        "sucursal",
                        "id_sucursal, direccion, telefono, descripcion, fecha_registro, estado, id_gimnasio",
                        None,
                        None,
                        "id_gimnasio",
                        gym_ids_admin,
                        "id_sucursal",
                    )
                )
            else:
                tasks.append(asyncio.sleep(0, result=[]))
            if gym_ids_socio_activos:
                tasks.append(
                    asyncio.to_thread(
                        _query_list_with_token,
                        token,
                        "sucursal",
                        "id_sucursal, direccion, telefono, descripcion, fecha_registro, estado, id_gimnasio",
                        None,
                        None,
                        "id_gimnasio",
                        sorted(gym_ids_socio_activos),
                        "id_sucursal",
                    )
                )
            else:
                tasks.append(asyncio.sleep(0, result=[]))

            (gyms_dueno, sucursales_dueno), sucursales_admin, sucursales_socio = await asyncio.gather(*tasks)
        else:
            gyms_dueno, sucursales_dueno = _fetch_dueno_chain()
            sucursales_admin = (
                _safe_query_list(
                    db,
                    "sucursal",
                    "id_sucursal, direccion, telefono, descripcion, fecha_registro, estado, id_gimnasio",
                    in_col="id_gimnasio",
                    in_vals=gym_ids_admin,
                    order_col="id_sucursal",
                )
                if gym_ids_admin
                else []
            )
            sucursales_socio = (
                _safe_query_list(
                    db,
                    "sucursal",
                    "id_sucursal, direccion, telefono, descripcion, fecha_registro, estado, id_gimnasio",
                    in_col="id_gimnasio",
                    in_vals=sorted(gym_ids_socio_activos),
                    order_col="id_sucursal",
                )
                if gym_ids_socio_activos
                else []
            )

        return {
            "dueno": dueno,
            "administrador": administrador,
            "empleado": empleado,
            "socio": socio,
            "perfiles_disponibles": perfiles_disp,
            "perfiles_inactivos": list(perfiles_inact),
            "gyms_dueno": gyms_dueno,
            "sucursales_dueno": sucursales_dueno,
            "sucursales_admin": sucursales_admin,
            "sucursales_socio": sucursales_socio,
        }

    if cached_bundle is None:
        if use_cache and cache_key:
            async with _get_inflight_async_lock(cache_key):
                cached_bundle = _get_cached_profile_bundle(cache_key)
                if cached_bundle is None:
                    cached_bundle = await _resolve_bundle()
                    if store_cache:
                        _set_cached_profile_bundle(cache_key, cached_bundle)
        else:
            cached_bundle = await _resolve_bundle()
            if store_cache and cache_key:
                _set_cached_profile_bundle(cache_key, cached_bundle)

    dueno = cached_bundle["dueno"]
    administrador = cached_bundle["administrador"]
    empleado = cached_bundle["empleado"]
    socio = cached_bundle["socio"]
    perfiles_disponibles = list(cached_bundle["perfiles_disponibles"])
    perfiles_inactivos = set(cached_bundle["perfiles_inactivos"])
    gyms_dueno_cached = cached_bundle["gyms_dueno"]
    sucursales_dueno_cached = cached_bundle["sucursales_dueno"]
    sucursales_admin_cached = cached_bundle["sucursales_admin"]
    sucursales_socio_cached = cached_bundle["sucursales_socio"]

    # 3. Construir el mapa de gimnasios, sucursales y permisos por cada perfil activo
    contexto_por_perfil: Dict[str, Dict[str, Any]] = {}

    # 3.1. Perfil Dueño
    if "dueño" in perfiles_disponibles and dueno is not None:
        gym_ids_dueno = [int(g["id_gimnasio"]) for g in gyms_dueno_cached if g.get("id_gimnasio") is not None]
        contexto_por_perfil["dueño"] = {
            "gimnasios": gyms_dueno_cached,
            "gym_ids": set(gym_ids_dueno),
            "sucursales": sucursales_dueno_cached,
            "sucursal_ids": {int(s["id_sucursal"]) for s in sucursales_dueno_cached if s.get("id_sucursal") is not None},
            "permisos": ["*"],
            "roles_empleado": [],
            "roles_por_gimnasio": {},
            "roles_por_sucursal": {},
        }

    # 3.2. Perfil Administrador Organizacional (N:M vía administrador_gimnasio)
    raw_admin_gyms = (administrador.get("administrador_gimnasio") or []) if administrador else []
    if not isinstance(raw_admin_gyms, list):
        raw_admin_gyms = [raw_admin_gyms] if raw_admin_gyms else []
    admin_asignaciones_activas = [
        ag for ag in raw_admin_gyms
        if isinstance(ag, dict) and ag.get("estado") is not False and ag.get("id_gimnasio") is not None
    ]

    if "administrador" in perfiles_disponibles and administrador is not None:
        gyms_admin: List[Dict[str, Any]] = []
        gym_ids_admin: List[int] = []
        for ag in admin_asignaciones_activas:
            gid = int(ag["id_gimnasio"])
            gym_obj = ag.get("gimnasio") if isinstance(ag.get("gimnasio"), dict) else {
                "id_gimnasio": gid,
                "estado": True,
            }
            if gid not in gym_ids_admin:
                gym_ids_admin.append(gid)
                gyms_admin.append({
                    **gym_obj,
                    "id_gimnasio": gid,
                    "id_administrador_gimnasio": ag.get("id_administrador_gimnasio"),
                    "estado_asignacion": ag.get("estado", True),
                })

        contexto_por_perfil["administrador"] = {
            "gimnasios": gyms_admin,
            "gym_ids": set(gym_ids_admin),
            "sucursales": sucursales_admin_cached,
            "sucursal_ids": {int(s["id_sucursal"]) for s in sucursales_admin_cached if s.get("id_sucursal") is not None},
            "permisos": list(PERMISOS_ADMINISTRADOR_GIMNASIO),
            "roles_empleado": [],
            "roles_por_gimnasio": {},
            "roles_por_sucursal": {},
            "administrador_gimnasios": admin_asignaciones_activas,
        }

    # 3.3. Perfil Empleado Operativo por Sucursal (identidad reutilizable N:M vía empleado_sucursal)
    raw_emp_sucs = (empleado.get("empleado_sucursal") or []) if empleado else []
    if not isinstance(raw_emp_sucs, list):
        raw_emp_sucs = [raw_emp_sucs] if raw_emp_sucs else []
    emp_asignaciones_activas = [
        es for es in raw_emp_sucs
        if isinstance(es, dict) and es.get("estado") is not False and es.get("id_sucursal") is not None
    ]

    if "empleado" in perfiles_disponibles and empleado is not None:
        sucursales_emp: List[Dict[str, Any]] = []
        gyms_emp_map: Dict[int, Dict[str, Any]] = {}
        roles_por_gimnasio: Dict[int, List[str]] = {}
        roles_por_sucursal: Dict[int, str] = {}

        for es in emp_asignaciones_activas:
            sid = int(es["id_sucursal"])
            sub_rol = str(es.get("rol") or "").strip().lower()
            suc_obj = es.get("sucursal") if isinstance(es.get("sucursal"), dict) else {}
            gid_raw = suc_obj.get("id_gimnasio") or es.get("id_gimnasio")
            gid = int(gid_raw) if gid_raw is not None else None

            suc_entry = {
                "id_empleado_sucursal": es.get("id_empleado_sucursal"),
                "id_sucursal": sid,
                "rol": sub_rol,
                "estado": es.get("estado", True),
                "direccion": suc_obj.get("direccion"),
                "telefono": suc_obj.get("telefono"),
                "descripcion": suc_obj.get("descripcion"),
                "estado_sucursal": suc_obj.get("estado", True),
                "id_gimnasio": gid,
            }
            sucursales_emp.append(suc_entry)
            if sub_rol:
                roles_por_sucursal[sid] = sub_rol

            if gid is not None:
                if sub_rol and sub_rol not in roles_por_gimnasio.setdefault(gid, []):
                    roles_por_gimnasio[gid].append(sub_rol)
                gym_obj = suc_obj.get("gimnasio") if isinstance(suc_obj.get("gimnasio"), dict) else None
                if gid not in gyms_emp_map:
                    gyms_emp_map[gid] = gym_obj or {
                        "id_gimnasio": gid,
                        "estado": True,
                    }

        all_emp_roles = sorted({r for r in roles_por_sucursal.values() if r})
        permisos_emp: Set[str] = set()
        for r in all_emp_roles:
            permisos_emp.update(PERMISOS_POR_ROL_EMPLEADO.get(r, []))

        contexto_por_perfil["empleado"] = {
            "gimnasios": list(gyms_emp_map.values()),
            "gym_ids": set(gyms_emp_map.keys()),
            "sucursales": sucursales_emp,
            "sucursal_ids": set(roles_por_sucursal.keys()),
            "permisos": sorted(permisos_emp),
            "roles_empleado": all_emp_roles,
            "roles_por_gimnasio": roles_por_gimnasio,
            "roles_por_sucursal": roles_por_sucursal,
        }

    # 3.4. Perfil Socio (multi-gimnasio N:M vía socio_gimnasio)
    raw_socio_gyms = (socio.get("socio_gimnasio") or []) if socio else []
    if not isinstance(raw_socio_gyms, list):
        raw_socio_gyms = [raw_socio_gyms] if raw_socio_gyms else []

    if "socio" in perfiles_disponibles and socio is not None:
        gyms_socio: List[Dict[str, Any]] = []
        gym_ids_socio_activos: Set[int] = set()
        socio_gimnasio_por_gym: Dict[int, Dict[str, Any]] = {}

        for sg in raw_socio_gyms:
            if not isinstance(sg, dict) or sg.get("id_gimnasio") is None:
                continue
            gid = int(sg["id_gimnasio"])
            sg_activo = sg.get("estado") is not False
            gym_obj = sg.get("gimnasio") if isinstance(sg.get("gimnasio"), dict) else {"id_gimnasio": gid}

            raw_membs = sg.get("membresia_socio") or []
            if not isinstance(raw_membs, list):
                raw_membs = [raw_membs] if raw_membs else []
            membs_ordenadas = sorted(
                [m for m in raw_membs if isinstance(m, dict)],
                key=lambda m: str(m.get("fecha_vencimiento") or ""),
                reverse=True,
            )
            memb_vigente = next(
                (m for m in membs_ordenadas if m.get("estado") == "activa"),
                membs_ordenadas[0] if membs_ordenadas else None,
            )

            # Un socio puede acceder al contexto del gimnasio si su asociación está activa
            if sg_activo:
                gym_ids_socio_activos.add(gid)
                gym_entry = {
                    **gym_obj,
                    "id_gimnasio": gid,
                    "id_socio_gimnasio": sg.get("id_socio_gimnasio"),
                    "estado_asociacion": sg.get("estado", True),
                    "fecha_alta_gimnasio": sg.get("fecha_alta"),
                    "estado_membresia": memb_vigente.get("estado") if memb_vigente else None,
                    "fecha_vencimiento": memb_vigente.get("fecha_vencimiento") if memb_vigente else None,
                    "id_membresia_socio": memb_vigente.get("id_membresia_socio") if memb_vigente else None,
                }
                gyms_socio.append(gym_entry)
                socio_gimnasio_por_gym[gid] = sg

        sucursales_socio = [s for s in sucursales_socio_cached if s.get("estado") is not False]

        contexto_por_perfil["socio"] = {
            "gimnasios": gyms_socio,
            "gym_ids": gym_ids_socio_activos,
            "sucursales": sucursales_socio,
            "sucursal_ids": {int(s["id_sucursal"]) for s in sucursales_socio if s.get("id_sucursal") is not None},
            "permisos": ["socio_self"],
            "roles_empleado": [],
            "roles_por_gimnasio": {},
            "roles_por_sucursal": {},
            "socio_gimnasio_por_gym": socio_gimnasio_por_gym,
            "socio_gimnasios": raw_socio_gyms,
        }

    # 4. Validar rol solicitado (anti-spoofing estricto)
    rol_activo: Optional[str] = None
    subrol_empleado_solicitado: Optional[str] = None

    if requested_role is not None:
        normalized_req = normalize_role_name(requested_role)
        if normalized_req is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"El rol solicitado ('{requested_role}') no es válido o no está autorizado.",
            )
        if normalized_req.startswith("empleado:"):
            subrol_empleado_solicitado = normalized_req.split(":", 1)[1]
            normalized_req = "empleado"

        if normalized_req in perfiles_inactivos:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Tu perfil de {normalized_req} se encuentra inactivo o deshabilitado.",
            )

        if normalized_req not in perfiles_disponibles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes autorización para asumir o suplantar el rol solicitado.",
            )

        if subrol_empleado_solicitado:
            emp_roles = contexto_por_perfil.get("empleado", {}).get("roles_empleado", [])
            if subrol_empleado_solicitado not in emp_roles:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"No posees asignación activa con el rol de empleado '{subrol_empleado_solicitado}'.",
                )

        rol_activo = normalized_req
    else:
        if not perfiles_disponibles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Tu cuenta se encuentra inactiva o deshabilitada. Contacta a la administración.",
            )

        # Si el cliente indicó un gimnasio o sucursal y el usuario tiene varios perfiles,
        # priorizar el perfil que tenga autorización real sobre ese gimnasio/sucursal.
        priority_order = ["dueño", "administrador", "empleado", "socio"]
        if requested_gym_id is not None:
            matching_profiles = [
                p for p in priority_order
                if p in perfiles_disponibles
                and requested_gym_id in contexto_por_perfil.get(p, {}).get("gym_ids", set())
            ]
            if matching_profiles:
                rol_activo = matching_profiles[0]
        elif requested_sucursal_id is not None:
            matching_profiles = [
                p for p in priority_order
                if p in perfiles_disponibles
                and requested_sucursal_id in contexto_por_perfil.get(p, {}).get("sucursal_ids", set())
            ]
            if matching_profiles:
                rol_activo = matching_profiles[0]

        if rol_activo is None:
            for candidate in priority_order:
                if candidate in perfiles_disponibles:
                    rol_activo = candidate
                    break

    assert rol_activo is not None
    perfil_ctx = contexto_por_perfil[rol_activo]
    allowed_gym_ids: Set[int] = perfil_ctx["gym_ids"]
    allowed_sucursal_ids: Set[int] = perfil_ctx["sucursal_ids"]
    gimnasios_activos: List[Dict[str, Any]] = perfil_ctx["gimnasios"]
    sucursales_activas: List[Dict[str, Any]] = perfil_ctx["sucursales"]

    # 5. Validar y resolver contexto de Gimnasio (anti-IDOR / anti-cruce de tenant)
    id_gimnasio_activo: Optional[int] = None
    gimnasio_activo: Optional[Dict[str, Any]] = None

    if requested_gym_id is not None:
        if requested_gym_id not in allowed_gym_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes autorización para acceder u operar en el gimnasio solicitado.",
            )
        id_gimnasio_activo = requested_gym_id
        gimnasio_activo = next(
            (g for g in gimnasios_activos if int(g["id_gimnasio"]) == requested_gym_id),
            None,
        )
    elif gimnasios_activos:
        primero_activo = next(
            (g for g in gimnasios_activos if g.get("estado") is not False),
            gimnasios_activos[0],
        )
        id_gimnasio_activo = int(primero_activo["id_gimnasio"])
        gimnasio_activo = primero_activo

    # 6. Validar y resolver contexto de Sucursal
    id_sucursal_activa: Optional[int] = None
    sucursal_activa: Optional[Dict[str, Any]] = None

    if requested_sucursal_id is not None:
        if requested_sucursal_id not in allowed_sucursal_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes autorización para acceder u operar en la sucursal solicitada.",
            )
        sucursal_activa = next(
            (s for s in sucursales_activas if int(s["id_sucursal"]) == requested_sucursal_id),
            None,
        )
        if (
            sucursal_activa
            and requested_gym_id is not None
            and sucursal_activa.get("id_gimnasio") is not None
            and int(sucursal_activa["id_gimnasio"]) != requested_gym_id
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="La sucursal solicitada no pertenece al gimnasio activo seleccionado.",
            )
        id_sucursal_activa = requested_sucursal_id
        if id_gimnasio_activo is None and sucursal_activa and sucursal_activa.get("id_gimnasio") is not None:
            id_gimnasio_activo = int(sucursal_activa["id_gimnasio"])
            gimnasio_activo = next(
                (g for g in gimnasios_activos if int(g["id_gimnasio"]) == id_gimnasio_activo),
                None,
            )
    else:
        sucursales_del_gym = [
            s for s in sucursales_activas
            if id_gimnasio_activo is None or s.get("id_gimnasio") == id_gimnasio_activo
        ]
        if sucursales_del_gym:
            primera_suc = next(
                (s for s in sucursales_del_gym if s.get("estado") is not False),
                sucursales_del_gym[0],
            )
            id_sucursal_activa = int(primera_suc["id_sucursal"])
            sucursal_activa = primera_suc

    # 7. Refinar roles y permisos de empleado según el gimnasio/sucursal activo
    roles_empleado_activos: List[str] = list(perfil_ctx.get("roles_empleado", []))
    permisos_activos: List[str] = list(perfil_ctx.get("permisos", []))

    if rol_activo == "empleado":
        roles_por_gym = perfil_ctx.get("roles_por_gimnasio", {})
        roles_por_suc = perfil_ctx.get("roles_por_sucursal", {})
        if requested_sucursal_id is not None and requested_sucursal_id in roles_por_suc:
            roles_empleado_activos = [roles_por_suc[requested_sucursal_id]]
        elif id_gimnasio_activo is not None and id_gimnasio_activo in roles_por_gym:
            roles_empleado_activos = list(roles_por_gym[id_gimnasio_activo])

        if subrol_empleado_solicitado and subrol_empleado_solicitado not in roles_empleado_activos:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"No posees el rol '{subrol_empleado_solicitado}' en el gimnasio o sucursal seleccionado.",
            )

        perm_set: Set[str] = set()
        for r in roles_empleado_activos:
            perm_set.update(PERMISOS_POR_ROL_EMPLEADO.get(r, []))
        permisos_activos = sorted(perm_set)

    # 8. Resolver id_socio_gimnasio activo si el usuario opera como socio
    id_socio_gimnasio_activo: Optional[int] = None
    if rol_activo == "socio" and id_gimnasio_activo is not None:
        sg_map = perfil_ctx.get("socio_gimnasio_por_gym", {})
        sg_row = sg_map.get(id_gimnasio_activo)
        if isinstance(sg_row, dict) and sg_row.get("id_socio_gimnasio") is not None:
            id_socio_gimnasio_activo = int(sg_row["id_socio_gimnasio"])

    id_dueno_val = int(dueno["id_dueno"]) if dueno and dueno.get("id_dueno") is not None else None
    id_admin_val = (
        int(administrador["id_administrador"])
        if administrador and administrador.get("id_administrador") is not None
        else None
    )
    id_empleado_val = (
        int(empleado["id_empleado"])
        if empleado and empleado.get("id_empleado") is not None
        else None
    )
    id_socio_val = int(socio["id_socio"]) if socio and socio.get("id_socio") is not None else None

    # Compatibilidad controlada con routers B2 aún no migrados que leen `id_usuario`
    legacy_id_usuario = id_dueno_val or id_admin_val or id_empleado_val

    return {
        "auth_user_id": uid,
        "email": auth_user.get("email"),
        "email_verified": auth_user.get("email_verified", False),
        "rol": rol_activo,
        "perfiles_disponibles": perfiles_disponibles,
        "dueno": dueno,
        "administrador": administrador,
        "administrador_gimnasios": admin_asignaciones_activas,
        "empleado": empleado,
        "socio": socio,
        "socio_gimnasios": raw_socio_gyms,
        "id_dueno": id_dueno_val,
        "id_administrador": id_admin_val,
        "id_empleado": id_empleado_val,
        "id_socio": id_socio_val,
        "id_socio_gimnasio": id_socio_gimnasio_activo,
        "id_usuario": legacy_id_usuario,
        "id_gimnasio": id_gimnasio_activo,
        "gimnasio": gimnasio_activo,
        "gimnasios": gimnasios_activos,
        "id_sucursal": id_sucursal_activa,
        "sucursal": sucursal_activa,
        "sucursales": sucursales_activas,
        "sucursales_asignadas": sucursales_activas,
        "roles_empleado": roles_empleado_activos,
        "roles_empleado_por_gimnasio": contexto_por_perfil.get("empleado", {}).get("roles_por_gimnasio", {}),
        "roles_empleado_por_sucursal": contexto_por_perfil.get("empleado", {}).get("roles_por_sucursal", {}),
        "permisos": permisos_activos,
        "contexto_por_perfil": contexto_por_perfil,
        "requested_gym_id": requested_gym_id,
        "requested_sucursal_id": requested_sucursal_id,
        "requested_role": requested_role,
        "access_token": auth_user["access_token"],
        "auth_user": auth_user,
        "supabase": db,
    }


async def get_current_user(
    request: Request,
    auth_user: Dict[str, Any] = Depends(get_current_auth_user),
    db: Client = Depends(get_user_supabase_client),
) -> Dict[str, Any]:
    """
    Dependency principal de autenticación y contexto de negocio:
    1. Valida criptográficamente el JWT de Supabase Auth (`get_current_auth_user`).
    2. Crea el cliente Supabase con RLS del usuario (`get_user_supabase_client`).
    3. Resuelve los perfiles reales en base de datos y verifica autorización de rol, gimnasio y sucursal.
    """
    requested_gym_id = _extract_requested_gym_id(request)
    requested_sucursal_id = _extract_requested_sucursal_id(request)
    requested_role = _extract_requested_role(request)

    is_get = request.method.upper() == "GET"
    is_auth_me = request.url.path.rstrip("/").endswith("/auth/me")

    contexto = await resolver_contexto_negocio(
        db=db,
        auth_user=auth_user,
        requested_gym_id=requested_gym_id,
        requested_sucursal_id=requested_sucursal_id,
        requested_role=requested_role,
        use_cache=is_get and not is_auth_me,
        store_cache=is_get,
    )
    request.state.current_user = contexto
    return contexto


async def get_optional_user(request: Request) -> Optional[Dict[str, Any]]:
    """
    Dependency para rutas con autenticación opcional en routers legacy pendientes de B2.
    - Si NO se envía cabecera Authorization, retorna None.
    - Si se envía cabecera Authorization, valida estrictamente el token y el contexto
      (nunca acepta ni ignora silenciosamente tokens inválidos, expirados o suplantaciones).
    """
    auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not auth_header or not auth_header.strip():
        return None

    auth_user = await get_current_auth_user(request)
    db = await get_user_supabase_client(request, auth_user)
    return await get_current_user(request, auth_user, db)


def _switch_profile_context_if_available(
    current_user: Dict[str, Any],
    target_profile: str,
) -> Dict[str, Any]:
    """
    Si el usuario posee múltiples perfiles verificados en BD y no fijó otro rol vía X-Active-Role,
    adapta el contexto activo al perfil requerido por el endpoint validando gimnasio y sucursal.
    """
    if current_user.get("rol") == target_profile:
        return current_user

    if current_user.get("requested_role") is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El rol activo seleccionado no tiene permisos para realizar esta acción.",
        )

    perfiles = current_user.get("perfiles_disponibles") or []
    ctx_map = current_user.get("contexto_por_perfil") or {}
    if target_profile not in perfiles or target_profile not in ctx_map:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes los permisos necesarios para realizar esta acción.",
        )

    p_ctx = ctx_map[target_profile]
    req_gym = current_user.get("requested_gym_id")
    req_suc = current_user.get("requested_sucursal_id")

    if req_gym is not None and req_gym not in p_ctx.get("gym_ids", set()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes autorización para acceder al gimnasio solicitado con este perfil.",
        )
    if req_suc is not None and req_suc not in p_ctx.get("sucursal_ids", set()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes autorización para acceder a la sucursal solicitada con este perfil.",
        )

    gyms = p_ctx.get("gimnasios") or []
    sucs = p_ctx.get("sucursales") or []

    active_gym = None
    if req_gym is not None:
        active_gym = next((g for g in gyms if int(g["id_gimnasio"]) == req_gym), None)
    elif gyms:
        active_gym = next((g for g in gyms if g.get("estado") is not False), gyms[0])

    active_gym_id = int(active_gym["id_gimnasio"]) if active_gym and active_gym.get("id_gimnasio") is not None else None

    active_suc = None
    if req_suc is not None:
        active_suc = next((s for s in sucs if int(s["id_sucursal"]) == req_suc), None)
    else:
        sucs_gym = [s for s in sucs if active_gym_id is None or s.get("id_gimnasio") == active_gym_id]
        if sucs_gym:
            active_suc = next((s for s in sucs_gym if s.get("estado") is not False), sucs_gym[0])

    active_suc_id = int(active_suc["id_sucursal"]) if active_suc and active_suc.get("id_sucursal") is not None else None

    updated = dict(current_user)
    updated["rol"] = target_profile
    updated["gimnasios"] = gyms
    updated["gimnasio"] = active_gym
    updated["id_gimnasio"] = active_gym_id
    updated["sucursales"] = sucs
    updated["sucursales_asignadas"] = sucs
    updated["sucursal"] = active_suc
    updated["id_sucursal"] = active_suc_id
    updated["roles_empleado"] = list(p_ctx.get("roles_empleado", []))
    updated["permisos"] = list(p_ctx.get("permisos", []))
    return updated


def require_roles(*roles_permitidos: str):
    """
    Factory de dependency para verificar roles verificados en la base de datos:
    Soporta: 'dueño' (o 'dueno'), 'administrador', 'empleado', 'socio',
    y sub-roles operativos de sucursal: 'empleado:administrador', 'empleado:recepcionista',
    'empleado:entrenador' (o 'recepcionista', 'entrenador').
    """
    normalized_targets: List[str] = []
    for r in roles_permitidos:
        clean = str(r or "").strip().lower()
        if clean in EMPLOYEE_SUBROLES and clean != "administrador":
            normalized_targets.append(f"empleado:{clean}")
        else:
            norm = normalize_role_name(clean)
            if norm:
                normalized_targets.append(norm)

    async def role_checker(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
        active_role = current_user.get("rol")

        def _matches(user_ctx: Dict[str, Any], candidate_role: str) -> bool:
            u_role = user_ctx.get("rol")
            if candidate_role == u_role:
                if u_role == "empleado" and not user_ctx.get("roles_empleado"):
                    return False
                if u_role == "administrador" and not user_ctx.get("gimnasios"):
                    return False
                return True
            if candidate_role.startswith("empleado:") and u_role == "empleado":
                sub = candidate_role.split(":", 1)[1]
                return sub in (user_ctx.get("roles_empleado") or [])
            return False

        # 1. Verificar con el rol activo actual
        for target in normalized_targets:
            if _matches(current_user, target):
                return current_user

        # 2. Si el usuario tiene múltiples perfiles en BD y no forzó otro rol vía X-Active-Role,
        # comprobar si alguno de sus otros perfiles activos cumple el requisito
        if current_user.get("requested_role") is None:
            for target in normalized_targets:
                base_profile = "empleado" if target.startswith("empleado:") else target
                if base_profile in (current_user.get("perfiles_disponibles") or []):
                    switched = _switch_profile_context_if_available(current_user, base_profile)
                    if _matches(switched, target):
                        return switched

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes los permisos necesarios para realizar esta acción.",
        )

    return role_checker


async def require_dueno(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Exige que el usuario autenticado tenga un perfil verificado de Dueño (`public.dueno`)."""
    ctx = _switch_profile_context_if_available(current_user, "dueño")
    if not ctx.get("id_dueno"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acción exclusiva para el dueño del gimnasio.",
        )
    return ctx


async def require_administrador(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """
    Exige que el usuario autenticado sea un Administrador Organizacional activo
    (`public.administrador` con al menos una asignación activa en `public.administrador_gimnasio`).
    """
    ctx = _switch_profile_context_if_available(current_user, "administrador")
    if not ctx.get("id_administrador") or not ctx.get("gimnasios"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No posees asignaciones activas como administrador de gimnasio.",
        )
    return ctx


def require_empleado(*roles_sucursal_permitidos: str):
    """
    Factory de dependency que exige que el usuario sea un Empleado activo (`public.empleado`)
    con asignación activa en `public.empleado_sucursal` (opcionalmente filtrando por sub-roles:
    'administrador', 'recepcionista', 'entrenador').
    """
    allowed_subroles = [r.strip().lower() for r in roles_sucursal_permitidos if r and r.strip()]

    async def _check_empleado(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
        ctx = _switch_profile_context_if_available(current_user, "empleado")
        if not ctx.get("id_empleado") or not ctx.get("sucursales_asignadas"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No posees asignaciones operativas activas en ninguna sucursal.",
            )
        active_subroles = ctx.get("roles_empleado") or []
        if allowed_subroles and not any(r in active_subroles for r in allowed_subroles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Tu rol de empleado en esta sucursal no tiene permisos para esta operación.",
            )
        return ctx

    return _check_empleado


async def require_socio(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Exige que el usuario autenticado tenga un perfil activo de Socio (`public.socio`)."""
    ctx = _switch_profile_context_if_available(current_user, "socio")
    if not ctx.get("id_socio"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso exclusivo para socios autenticados.",
        )
    return ctx


async def require_gimnasio_context(
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Exige un contexto de gimnasio activo y verificado contra las relaciones reales del usuario en BD.
    Rechaza con 403 cualquier intento de acceder a un gimnasio no autorizado y con 400 si el usuario
    no tiene ningún gimnasio asociado/activo.
    """
    req_gym = _extract_requested_gym_id(request)
    allowed_ids = {
        int(g["id_gimnasio"])
        for g in (current_user.get("gimnasios") or [])
        if isinstance(g, dict) and g.get("id_gimnasio") is not None
    }

    if req_gym is not None and req_gym not in allowed_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes autorización para acceder u operar en el gimnasio solicitado.",
        )

    if not current_user.get("id_gimnasio") or int(current_user["id_gimnasio"]) not in allowed_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No hay un gimnasio activo autorizado disponible para tu cuenta.",
        )

    return current_user


def require_sucursal_context(*roles_empleado_permitidos: str):
    """
    Factory de dependency que exige un contexto de sucursal (`id_sucursal`) válido y autorizado en BD.
    Si el usuario opera como empleado, comprueba que su asignación activa en esa sucursal específica
    corresponda a uno de los `roles_empleado_permitidos` (si se especificaron).
    """
    allowed_subroles = [r.strip().lower() for r in roles_empleado_permitidos if r and r.strip()]

    async def _check_sucursal(
        request: Request,
        current_user: Dict[str, Any] = Depends(get_current_user),
    ) -> Dict[str, Any]:
        req_suc = _extract_requested_sucursal_id(request)
        sucursales = current_user.get("sucursales") or []
        allowed_suc_ids = {
            int(s["id_sucursal"])
            for s in sucursales
            if isinstance(s, dict) and s.get("id_sucursal") is not None
        }

        if req_suc is not None and req_suc not in allowed_suc_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes autorización para acceder u operar en la sucursal solicitada.",
            )

        active_suc_id = req_suc or current_user.get("id_sucursal")
        if not active_suc_id or int(active_suc_id) not in allowed_suc_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No hay una sucursal activa autorizada seleccionada o disponible.",
            )

        if current_user.get("rol") == "empleado" and allowed_subroles:
            roles_por_suc = current_user.get("roles_empleado_por_sucursal") or {}
            rol_en_sucursal = roles_por_suc.get(int(active_suc_id))
            if not rol_en_sucursal or rol_en_sucursal not in allowed_subroles:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No posees el rol requerido en la sucursal seleccionada para realizar esta acción.",
                )

        return current_user

    return _check_sucursal


def can_access_gimnasio(current_user: Optional[Dict[str, Any]], id_gimnasio: Any) -> bool:
    """Comprueba si el usuario autenticado tiene acceso autorizado al gimnasio indicado."""
    target = _parse_positive_int(id_gimnasio)
    if not current_user or target is None:
        return False
    allowed = {
        int(g["id_gimnasio"])
        for g in (current_user.get("gimnasios") or [])
        if isinstance(g, dict) and g.get("id_gimnasio") is not None
    }
    return target in allowed


def can_access_sucursal(
    current_user: Optional[Dict[str, Any]],
    id_sucursal: Any,
    roles_empleado_permitidos: Optional[Sequence[str]] = None,
) -> bool:
    """Comprueba si el usuario autenticado tiene acceso autorizado a la sucursal indicada."""
    target = _parse_positive_int(id_sucursal)
    if not current_user or target is None:
        return False

    sucursales = current_user.get("sucursales") or []
    allowed = {
        int(s["id_sucursal"])
        for s in sucursales
        if isinstance(s, dict) and s.get("id_sucursal") is not None
    }
    if target not in allowed:
        return False

    if current_user.get("rol") == "empleado" and roles_empleado_permitidos:
        allowed_roles = {r.strip().lower() for r in roles_empleado_permitidos if r}
        rol_suc = (current_user.get("roles_empleado_por_sucursal") or {}).get(target)
        return bool(rol_suc and rol_suc in allowed_roles)

    return True
