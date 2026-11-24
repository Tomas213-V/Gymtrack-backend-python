"""
Servicio centralizado de detección segura de identidades existentes y aprovisionamiento
de cuentas globales de Supabase Auth para GymTrack.

Reglas de seguridad obligatorias:
- Una única cuenta global en Supabase Auth por persona (`auth_user_id`).
- Nunca almacena ni devuelve contraseñas en tablas propias, logs ni respuestas API.
- Nunca utiliza `email_confirm=True` para evitar el flujo de verificación configurado.
- Nunca permite que un dueño modifique o reemplace la contraseña de una cuenta Auth existente.
- Evita enumeración masiva de DNI/correos con rate limiting por actor autenticado y
  respuestas mínimas sin fuga de datos personales de otros gimnasios.
- Gestiona compensación (limpieza de cuenta Auth) si falla la vinculación inicial.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple

from fastapi import HTTPException, status
from supabase import Client

from app.config import (
    create_supabase_anon_client,
    create_supabase_client_for_token,
    supabase_admin,
    supabase_anon,
)
from app.services.utils import get_data

EMAIL_REGEX = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# Rate limit ligero en memoria por usuario autenticado (evita enumeración masiva de DNI/emails)
_LOOKUP_RATE_LIMIT_MAX = 30
_LOOKUP_RATE_LIMIT_WINDOW_SECONDS = 60.0
_LOOKUP_RATE_LOCK = threading.Lock()
_LOOKUP_HISTORY: Dict[str, Deque[float]] = {}

_PERFIL_TABLE_META: Dict[str, Tuple[str, str]] = {
    "socio": ("socio", "id_socio"),
    "empleado": ("empleado", "id_empleado"),
    "administrador": ("administrador", "id_administrador"),
    "dueno": ("dueno", "id_dueno"),
}


def reset_lookup_rate_limit() -> None:
    """Limpia el estado en memoria de rate-limit (útil en tests)."""
    with _LOOKUP_RATE_LOCK:
        _LOOKUP_HISTORY.clear()


def enforce_identity_lookup_rate_limit(actor_auth_user_id: Optional[str]) -> None:
    """Limita la frecuencia de consultas de detección de identidades por actor autenticado."""
    if not actor_auth_user_id:
        return
    now = time.monotonic()
    with _LOOKUP_RATE_LOCK:
        bucket = _LOOKUP_HISTORY.setdefault(str(actor_auth_user_id), deque())
        while bucket and (now - bucket[0]) > _LOOKUP_RATE_LIMIT_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= _LOOKUP_RATE_LIMIT_MAX:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Demasiadas consultas de verificación en poco tiempo. Esperá unos segundos e intentá nuevamente.",
            )
        bucket.append(now)


def normalize_dni(value: Any) -> Optional[str]:
    if value is None:
        return None
    clean = str(value).strip()
    return clean if clean else None


def normalize_dni_digits(value: Any) -> Optional[str]:
    clean = normalize_dni(value)
    if not clean:
        return None
    stripped = re.sub(r"[\.\s-]", "", clean)
    return stripped or clean


def normalize_email(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    clean = str(value).strip().lower()
    return clean if clean else None


def _get_admin_client(override: Optional[Client] = None) -> Optional[Client]:
    return override if override is not None else supabase_admin


def _get_anon_client(override: Optional[Client] = None) -> Optional[Client]:
    if override is not None:
        return override
    if supabase_anon is not None and type(supabase_anon).__module__.startswith("supabase"):
        return create_supabase_anon_client()
    return supabase_anon


def _find_profile_row_by_email_or_dni(
    client: Client,
    table: str,
    pk_col: str,
    email: Optional[str],
    dni: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """
    Busca una fila en `public.{table}` por email y/o DNI devolviendo `(row, conflicto_identidad)`.
    `conflicto_identidad` es True si el email y el DNI pertenecen a registros distintos.
    """
    cols = f"{pk_col}, auth_user_id, email, estado" + (", dni" if table != "dueno" else "")
    row_email: Optional[Dict[str, Any]] = None
    row_dni: Optional[Dict[str, Any]] = None

    if email:
        try:
            res = client.from_(table).select(cols).eq("email", email).maybe_single().execute()
            data = get_data(res)
            if isinstance(data, dict):
                row_email = data
        except Exception:
            pass

    if dni and table != "dueno":
        for candidate_dni in dict.fromkeys(filter(None, [dni, normalize_dni_digits(dni)])):
            try:
                res = client.from_(table).select(cols).eq("dni", candidate_dni).maybe_single().execute()
                data = get_data(res)
                if isinstance(data, dict):
                    row_dni = data
                    break
            except Exception:
                pass

    if row_email and row_dni and row_email.get(pk_col) != row_dni.get(pk_col):
        return None, True

    return (row_dni or row_email), False


def _find_global_auth_uid_by_email(
    email: Optional[str],
    admin_client: Optional[Client] = None,
    fallback_db: Optional[Client] = None,
) -> Optional[str]:
    """
    Busca si el correo ya tiene una identidad global (`auth_user_id`) vinculada en cualquier
    perfil de GymTrack o en Supabase Auth (sin exponer datos privados ni contraseñas).
    """
    clean_email = normalize_email(email)
    if not clean_email:
        return None

    adm = _get_admin_client(admin_client)
    if adm is not None:
        # 1. Buscar primero en las tablas de perfiles (rápido e indexado)
        for table, pk_col in _PERFIL_TABLE_META.values():
            try:
                res = (
                    adm.from_(table)
                    .select(f"{pk_col}, auth_user_id")
                    .eq("email", clean_email)
                    .maybe_single()
                    .execute()
                )
                row = get_data(res)
                if isinstance(row, dict) and row.get("auth_user_id"):
                    return str(row["auth_user_id"])
            except Exception:
                continue

        # 2. Fallback acotado en Supabase Auth Admin API (cuando el usuario Auth existe sin perfil aún)
        try:
            auth_admin = getattr(getattr(adm, "auth", None), "admin", None)
            if auth_admin is not None and hasattr(auth_admin, "list_users"):
                users_resp = auth_admin.list_users(page=1, per_page=200)
                users_list = (
                    users_resp
                    if isinstance(users_resp, list)
                    else getattr(users_resp, "users", None) or []
                )
                for u in users_list:
                    u_email = normalize_email(
                        u.get("email") if isinstance(u, dict) else getattr(u, "email", None)
                    )
                    if u_email == clean_email:
                        u_id = u.get("id") if isinstance(u, dict) else getattr(u, "id", None)
                        if u_id:
                            return str(u_id)
        except Exception:
            pass
    elif fallback_db is not None:
        for table, pk_col in _PERFIL_TABLE_META.values():
            try:
                res = (
                    fallback_db.from_(table)
                    .select(f"{pk_col}, auth_user_id")
                    .eq("email", clean_email)
                    .maybe_single()
                    .execute()
                )
                row = get_data(res)
                if isinstance(row, dict) and row.get("auth_user_id"):
                    return str(row["auth_user_id"])
            except Exception:
                continue

    return None


def detectar_identidad_para_incorporacion(
    *,
    db: Client,
    actor_auth_user_id: Optional[str],
    tipo_perfil: str,
    gym_id: int,
    sucursal_id: Optional[int] = None,
    dni: Optional[str] = None,
    email: Optional[str] = None,
    admin_client: Optional[Client] = None,
    skip_rate_limit: bool = False,
) -> Dict[str, Any]:
    """
    Detecta de forma segura si un DNI y/o correo corresponde a una persona ya registrada
    en GymTrack, sin exponer datos personales sensibles de otros gimnasios.
    """
    if not skip_rate_limit:
        enforce_identity_lookup_rate_limit(actor_auth_user_id)

    tipo_norm = str(tipo_perfil or "").strip().lower()
    if tipo_norm not in ("socio", "empleado", "administrador"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tipo de perfil inválido. Valores permitidos: socio, empleado, administrador.",
        )

    clean_dni = normalize_dni(dni)
    clean_dni_digits = normalize_dni_digits(dni)
    clean_email = normalize_email(email)

    if not clean_dni and not clean_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes proporcionar al menos DNI o correo electrónico para verificar la identidad.",
        )
    if clean_email and not EMAIL_REGEX.match(clean_email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El formato del correo electrónico no es válido.",
        )

    ya_asociado_en_este_gimnasio = False
    asociacion_activa_en_este_gimnasio = False
    ya_asignado_en_esta_sucursal = False

    # 1. Verificar pertenencia en el gimnasio / sucursal actual vía cliente RLS del actor
    if tipo_norm == "socio":
        res_sg = (
            db.from_("socio_gimnasio")
            .select("id_socio_gimnasio, id_socio, id_gimnasio, estado, socio(id_socio, dni, email, estado)")
            .eq("id_gimnasio", int(gym_id))
            .execute()
        )
        for sg in (get_data(res_sg) or []):
            if not isinstance(sg, dict) or int(sg.get("id_gimnasio") or 0) != int(gym_id):
                continue
            soc = sg.get("socio") if isinstance(sg.get("socio"), dict) else {}
            s_dni = normalize_dni(soc.get("dni"))
            s_dni_digits = normalize_dni_digits(soc.get("dni"))
            s_email = normalize_email(soc.get("email"))
            match_dni = bool(clean_dni and s_dni and (s_dni == clean_dni or s_dni_digits == clean_dni_digits))
            match_email = bool(clean_email and s_email and s_email == clean_email)
            if match_dni or match_email:
                ya_asociado_en_este_gimnasio = True
                if sg.get("estado") is not False:
                    asociacion_activa_en_este_gimnasio = True
                    break

    elif tipo_norm == "administrador":
        res_ag = (
            db.from_("administrador_gimnasio")
            .select(
                "id_administrador_gimnasio, id_administrador, id_gimnasio, estado, "
                "administrador(id_administrador, dni, email, estado)"
            )
            .eq("id_gimnasio", int(gym_id))
            .execute()
        )
        for ag in (get_data(res_ag) or []):
            if not isinstance(ag, dict) or int(ag.get("id_gimnasio") or 0) != int(gym_id):
                continue
            adm_obj = ag.get("administrador") if isinstance(ag.get("administrador"), dict) else {}
            a_dni = normalize_dni(adm_obj.get("dni"))
            a_dni_digits = normalize_dni_digits(adm_obj.get("dni"))
            a_email = normalize_email(adm_obj.get("email"))
            match_dni = bool(clean_dni and a_dni and (a_dni == clean_dni or a_dni_digits == clean_dni_digits))
            match_email = bool(clean_email and a_email and a_email == clean_email)
            if match_dni or match_email:
                ya_asociado_en_este_gimnasio = True
                if ag.get("estado") is not False:
                    asociacion_activa_en_este_gimnasio = True
                    break

    elif tipo_norm == "empleado":
        res_sucs = (
            db.from_("sucursal")
            .select("id_sucursal, id_gimnasio")
            .eq("id_gimnasio", int(gym_id))
            .execute()
        )
        suc_ids_gym = {
            int(s["id_sucursal"])
            for s in (get_data(res_sucs) or [])
            if isinstance(s, dict) and s.get("id_sucursal") is not None
        }
        if suc_ids_gym:
            res_es = (
                db.from_("empleado_sucursal")
                .select(
                    "id_empleado_sucursal, id_empleado, id_sucursal, rol, estado, "
                    "empleado(id_empleado, dni, email, estado)"
                )
                .in_("id_sucursal", sorted(suc_ids_gym))
                .execute()
            )
            for es in (get_data(res_es) or []):
                if not isinstance(es, dict):
                    continue
                emp_obj = es.get("empleado") if isinstance(es.get("empleado"), dict) else {}
                e_dni = normalize_dni(emp_obj.get("dni"))
                e_dni_digits = normalize_dni_digits(emp_obj.get("dni"))
                e_email = normalize_email(emp_obj.get("email"))
                match_dni = bool(clean_dni and e_dni and (e_dni == clean_dni or e_dni_digits == clean_dni_digits))
                match_email = bool(clean_email and e_email and e_email == clean_email)
                if match_dni or match_email:
                    ya_asociado_en_este_gimnasio = True
                    if es.get("estado") is not False:
                        asociacion_activa_en_este_gimnasio = True
                        if sucursal_id is not None and int(es.get("id_sucursal") or 0) == int(sucursal_id):
                            ya_asignado_en_esta_sucursal = True

    # 2. Verificar existencia global (mismo tipo de perfil + cuenta Auth global)
    table, pk_col = _PERFIL_TABLE_META[tipo_norm]
    adm = _get_admin_client(admin_client)
    lookup_client = adm if adm is not None else db

    perfil_row, conflicto = _find_profile_row_by_email_or_dni(
        lookup_client, table, pk_col, clean_email, clean_dni
    )

    if conflicto:
        return {
            "tipo_perfil": tipo_norm,
            "existe_en_gymtrack": True,
            "existe_perfil_mismo_tipo": True,
            "tiene_cuenta_auth": True,
            "ya_asociado_en_este_gimnasio": ya_asociado_en_este_gimnasio,
            "asociacion_activa_en_este_gimnasio": asociacion_activa_en_este_gimnasio,
            "ya_asignado_en_esta_sucursal": ya_asignado_en_esta_sucursal,
            "conflicto_identidad": True,
            "requiere_contrasena_inicial": False,
            "requiere_confirmacion": False,
            "mensaje": "Conflicto de identidad: el DNI y el correo electrónico corresponden a personas distintas.",
        }

    existe_perfil_mismo_tipo = perfil_row is not None
    perfil_auth_uid = str(perfil_row["auth_user_id"]) if (perfil_row and perfil_row.get("auth_user_id")) else None
    global_auth_uid = perfil_auth_uid or _find_global_auth_uid_by_email(
        clean_email or (perfil_row.get("email") if perfil_row else None),
        admin_client=adm,
        fallback_db=db,
    )
    tiene_cuenta_auth = bool(global_auth_uid)
    existe_en_gymtrack = bool(
        existe_perfil_mismo_tipo
        or tiene_cuenta_auth
        or ya_asociado_en_este_gimnasio
    )

    # 3. Mensaje claro y seguro (sin filtrar datos personales de otros gimnasios)
    if tipo_norm == "empleado" and ya_asignado_en_esta_sucursal:
        mensaje = "Esta persona ya tiene una asignación activa en esta sucursal."
        requiere_confirmacion = False
    elif tipo_norm in ("socio", "administrador") and asociacion_activa_en_este_gimnasio:
        mensaje = "Esta persona ya está asociada a tu gimnasio."
        requiere_confirmacion = False
    elif existe_en_gymtrack:
        mensaje = "Esta persona ya tiene un registro en GymTrack. ¿Querés incorporarla a tu gimnasio?"
        requiere_confirmacion = True
    else:
        mensaje = "Persona nueva en GymTrack. Podés definir una contraseña inicial para su cuenta."
        requiere_confirmacion = False

    return {
        "tipo_perfil": tipo_norm,
        "existe_en_gymtrack": existe_en_gymtrack,
        "existe_perfil_mismo_tipo": existe_perfil_mismo_tipo,
        "tiene_cuenta_auth": tiene_cuenta_auth,
        "ya_asociado_en_este_gimnasio": ya_asociado_en_este_gimnasio,
        "asociacion_activa_en_este_gimnasio": asociacion_activa_en_este_gimnasio,
        "ya_asignado_en_esta_sucursal": ya_asignado_en_esta_sucursal,
        "conflicto_identidad": False,
        "requiere_contrasena_inicial": not tiene_cuenta_auth,
        "requiere_confirmacion": requiere_confirmacion,
        "mensaje": mensaje,
        # Uso interno exclusivamente en el backend (se remueve antes de responder a HTTP)
        "_internal_global_auth_uid": global_auth_uid,
        "_internal_perfil_auth_uid": perfil_auth_uid,
        "_internal_perfil_estado": perfil_row.get("estado") if isinstance(perfil_row, dict) else None,
    }


def sanitize_detection_response(result: Dict[str, Any]) -> Dict[str, Any]:
    """Elimina cualquier clave interna antes de devolver la respuesta HTTP al frontend."""
    return {k: v for k, v in result.items() if not k.startswith("_internal_")}


def validate_initial_password_policy(
    *,
    password_inicial: Optional[str],
    email: Optional[str],
    tiene_cuenta_auth_existente: bool,
    perfil_ya_existia: bool,
) -> Optional[str]:
    """
    Valida la política de contraseña inicial antes de ejecutar la RPC de negocio:
    - Si la persona ya tiene cuenta Auth global o ya existía su perfil con cuenta Auth,
      PROHÍBE enviar una nueva contraseña (evita que un dueño sobrescriba credenciales ajenas).
    - Si se envía contraseña inicial para una persona nueva, exige email válido y >= 6 caracteres.
    """
    clean_pwd = str(password_inicial).strip() if password_inicial is not None else ""
    if not clean_pwd:
        return None

    if tiene_cuenta_auth_existente:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Esta persona ya posee una cuenta global en GymTrack. "
                "No está permitido modificar ni reemplazar su contraseña al incorporarla."
            ),
        )

    if perfil_ya_existia:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Esta persona ya tiene un registro en GymTrack. "
                "Confirmá su incorporación sin definir una contraseña nueva."
            ),
        )

    clean_email = normalize_email(email)
    if not clean_email or not EMAIL_REGEX.match(clean_email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Para establecer una contraseña inicial debes ingresar un correo electrónico válido.",
        )

    if len(clean_pwd) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La contraseña inicial debe tener al menos 6 caracteres.",
        )

    return clean_pwd


def _link_auth_uid_with_admin(
    admin_client: Client,
    table: str,
    pk_col: str,
    pk_val: int,
    auth_uid: str,
) -> None:
    res = (
        admin_client.from_(table)
        .update({"auth_user_id": auth_uid})
        .eq(pk_col, int(pk_val))
        .execute()
    )
    rows = get_data(res)
    if rows is None or (isinstance(rows, list) and len(rows) == 0):
        raise RuntimeError(f"No se pudo vincular auth_user_id en public.{table} (#{pk_val}).")


def _compensate_delete_auth_user(admin_client: Client, auth_uid: str) -> None:
    try:
        auth_admin = getattr(getattr(admin_client, "auth", None), "admin", None)
        if auth_admin is not None and hasattr(auth_admin, "delete_user"):
            auth_admin.delete_user(auth_uid)
    except Exception:
        # Nunca exponer errores internos de limpieza ni credenciales
        pass


def aprovisionar_o_vincular_cuenta_auth(
    *,
    tipo_perfil: str,
    pk_val: int,
    email: Optional[str],
    nombre: Optional[str],
    apellido: Optional[str],
    password_inicial: Optional[str],
    existing_profile_auth_uid: Optional[str] = None,
    existing_global_auth_uid: Optional[str] = None,
    admin_client: Optional[Client] = None,
    anon_client: Optional[Client] = None,
) -> Dict[str, Any]:
    """
    Aprovisiona o reutiliza la cuenta global de Supabase Auth para el perfil recién registrado:
    1. Si el perfil ya tiene `auth_user_id`: no crea ni toca credenciales.
    2. Si ya existe una cuenta Auth global con ese email (ej. la persona ya tiene otro rol):
       reutiliza su `auth_user_id` vinculándolo al nuevo perfil sin crear otra cuenta ni tocar contraseña.
    3. Si el dueño proveyó `password_inicial` para una persona nueva sin cuenta Auth:
       - Con `supabase_admin`: usa `auth.admin.create_user(..., email_confirm=False)` y vincula
         `auth_user_id`; si falla la vinculación, elimina el usuario Auth recién creado (compensación).
       - Sin `supabase_admin`: usa `supabase_anon.auth.sign_up(...)` (respeta confirmación real).
    Nunca devuelve contraseñas ni `auth_user_id`.
    """
    table, pk_col = _PERFIL_TABLE_META[tipo_perfil]
    adm = _get_admin_client(admin_client)
    anon = _get_anon_client(anon_client)
    clean_email = normalize_email(email)

    # Caso 1: perfil ya vinculado a una cuenta global
    if existing_profile_auth_uid:
        return {
            "cuenta_auth_creada": False,
            "cuenta_auth_reutilizada": True,
            "requiere_confirmacion_email": False,
        }

    # Caso 2: ya existe cuenta global para ese correo (otro rol o ficha previa) -> reutilizar
    global_uid = existing_global_auth_uid or _find_global_auth_uid_by_email(clean_email, admin_client=adm)
    if global_uid:
        if adm is not None:
            try:
                _link_auth_uid_with_admin(adm, table, pk_col, pk_val, global_uid)
            except Exception:
                pass
        return {
            "cuenta_auth_creada": False,
            "cuenta_auth_reutilizada": True,
            "requiere_confirmacion_email": False,
        }

    # Caso 3: persona nueva sin contraseña inicial -> ficha presencial pendiente de activación
    if not password_inicial:
        return {
            "cuenta_auth_creada": False,
            "cuenta_auth_reutilizada": False,
            "requiere_confirmacion_email": False,
        }

    if not clean_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El correo electrónico es obligatorio para crear la cuenta de acceso.",
        )

    user_meta = {
        "nombre": (nombre or "").strip(),
        "apellido": (apellido or "").strip(),
    }

    # Camino A: Supabase Admin API en servidor (nunca email_confirm=True)
    if adm is not None:
        auth_admin = getattr(getattr(adm, "auth", None), "admin", None)
        if auth_admin is not None and hasattr(auth_admin, "create_user"):
            created_uid: Optional[str] = None
            try:
                created_res = auth_admin.create_user(
                    {
                        "email": clean_email,
                        "password": password_inicial,
                        "email_confirm": False,
                        "user_metadata": user_meta,
                    }
                )
                created_user = getattr(created_res, "user", None) or (
                    created_res.get("user") if isinstance(created_res, dict) else created_res
                )
                created_uid = (
                    created_user.get("id")
                    if isinstance(created_user, dict)
                    else getattr(created_user, "id", None)
                )
                if not created_uid:
                    raise RuntimeError("Supabase Auth no devolvió el identificador del usuario creado.")
                created_uid = str(created_uid)
            except Exception as exc:
                msg = str(exc).lower()
                if "already" in msg or "registered" in msg or "exists" in msg:
                    # La cuenta Auth ya existía en Supabase: reutilizarla sin sobrescribir contraseña
                    existing_uid = _find_global_auth_uid_by_email(clean_email, admin_client=adm)
                    if existing_uid:
                        try:
                            _link_auth_uid_with_admin(adm, table, pk_col, pk_val, existing_uid)
                        except Exception:
                            pass
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            "Ya existe una cuenta de GymTrack con este correo electrónico. "
                            "No se modificó su contraseña; incorporá a la persona sin definir una nueva contraseña."
                        ),
                    ) from exc
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No fue posible crear la cuenta de acceso en Supabase Auth.",
                ) from exc

            try:
                _link_auth_uid_with_admin(adm, table, pk_col, pk_val, created_uid)
            except Exception as exc:
                # Compensación obligatoria: eliminar cuenta Auth para no dejar cuentas huérfanas
                _compensate_delete_auth_user(adm, created_uid)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="No se pudo vincular la cuenta de acceso al perfil creado. Se revirtió la creación Auth.",
                ) from exc

            return {
                "cuenta_auth_creada": True,
                "cuenta_auth_reutilizada": False,
                "requiere_confirmacion_email": True,
            }

    # Camino B: sin service_role configurado -> flujo público oficial sign_up de Supabase Auth
    if anon is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Supabase Auth no está configurado correctamente en el servidor.",
        )

    try:
        signup_res = anon.auth.sign_up(
            {
                "email": clean_email,
                "password": password_inicial,
                "options": {"data": user_meta},
            }
        )
        auth_user = getattr(signup_res, "user", None)
        session = getattr(signup_res, "session", None)
    except Exception as exc:
        msg = str(exc).lower()
        if "already" in msg or "registered" in msg or "exists" in msg:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Ya existe una cuenta de GymTrack con este correo electrónico. "
                    "No se modificó su contraseña; incorporá a la persona sin definir una nueva contraseña."
                ),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fue posible crear la cuenta de acceso en Supabase Auth.",
        ) from exc

    # Detección de usuario ya existente en Supabase sign_up (identities vacío cuando email confirmation está activo)
    identities = getattr(auth_user, "identities", None) if auth_user else None
    if auth_user is not None and isinstance(identities, list) and len(identities) == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Ya existe una cuenta de GymTrack con este correo electrónico. "
                "No se modificó su contraseña; incorporá a la persona sin definir una nueva contraseña."
            ),
        )

    access_token = getattr(session, "access_token", None) if session else None
    email_confirmed_at = getattr(auth_user, "email_confirmed_at", None) if auth_user else None

    if access_token and email_confirmed_at:
        try:
            user_db = create_supabase_client_for_token(access_token)
            user_db.rpc("rpc_vincular_identidad_perfil", {"p_perfil_tipo": tipo_perfil}).execute()
        except Exception:
            pass
        return {
            "cuenta_auth_creada": True,
            "cuenta_auth_reutilizada": False,
            "requiere_confirmacion_email": False,
        }

    return {
        "cuenta_auth_creada": True,
        "cuenta_auth_reutilizada": False,
        "requiere_confirmacion_email": True,
    }
