import re
from typing import Dict, Any, Optional
import httpx
from fastapi import APIRouter, HTTPException, status, Depends
from supabase import Client

from app.config import (
    SUPABASE_URL,
    SUPABASE_ANON_KEY,
    supabase_anon,
    create_supabase_client_for_token,
    validate_supabase_configuration,
)
from app.core.dependencies import (
    get_current_auth_user,
    get_user_supabase_client,
    get_current_user,
    resolver_contexto_negocio,
)
from app.services.utils import (
    get_data,
    insert_gimnasio_with_rls,
    map_postgrest_error,
    raise_http_from_postgrest_error,
)
from app.models.auth import (
    RegisterOwnerRequest,
    RegisterSocioRequest,
    LoginRequest,
    LoginSocioRequest,
    RefreshTokenRequest,
    VincularPerfilRequest,
    UpdateOwnerProfileRequest,
)

router = APIRouter(prefix="/api/auth", tags=["Autenticación"])

EMAIL_REGEX = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _require_supabase_anon() -> Client:
    if supabase_anon is None:
        try:
            validate_supabase_configuration()
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(exc),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="El cliente público de Supabase Auth no está configurado en el servidor.",
        )
    return supabase_anon


def _build_auth_identity_from_supabase_user(user_obj: Any, access_token: str) -> Dict[str, Any]:
    """
    Construye el diccionario de identidad Auth a partir de la respuesta oficial de Supabase Auth.
    Nunca confía en `user_metadata` para email ni para el estado de verificación del correo.
    """
    uid = str(getattr(user_obj, "id", None) or (user_obj.get("id") if isinstance(user_obj, dict) else ""))
    email = getattr(user_obj, "email", None) or (user_obj.get("email") if isinstance(user_obj, dict) else None)
    email_confirmed_at = getattr(user_obj, "email_confirmed_at", None) or (
        user_obj.get("email_confirmed_at") if isinstance(user_obj, dict) else None
    )
    app_meta = getattr(user_obj, "app_metadata", None) or (
        user_obj.get("app_metadata") if isinstance(user_obj, dict) else {}
    ) or {}
    user_meta = getattr(user_obj, "user_metadata", None) or (
        user_obj.get("user_metadata") if isinstance(user_obj, dict) else {}
    ) or {}

    return {
        "auth_user_id": uid,
        "sub": uid,
        "email": str(email).strip().lower() if email else None,
        "role": "authenticated",
        "email_confirmed_at": email_confirmed_at,
        "email_verified": bool(email_confirmed_at),
        "app_metadata": app_meta,
        "user_metadata": user_meta,
        "access_token": access_token,
    }


def _try_auto_link_profile(db: Client, auth_identity: Dict[str, Any], perfil_tipo: str) -> bool:
    """
    Intenta vincular automáticamente un perfil preexistente sin auth_user_id durante un inicio
    de sesión legítimo invocando `public.rpc_vincular_identidad_perfil(p_perfil_tipo)`.
    Exige estrictamente en Python Y en PostgreSQL que el correo esté realmente verificado
    (`email_confirmed_at IS NOT NULL` en `auth.users`).
    """
    if not auth_identity.get("email_verified") or not auth_identity.get("email_confirmed_at"):
        return False
    try:
        res = db.rpc("rpc_vincular_identidad_perfil", {"p_perfil_tipo": perfil_tipo}).execute()
        data = get_data(res)
        return bool(isinstance(data, dict) and data.get("vinculado") is True)
    except Exception:
        return False


async def _update_supabase_auth_user_with_token(
    access_token: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Actualiza los atributos de cuenta (`password` o solicitud de cambio de `email`) del usuario
    autenticado llamando directamente al endpoint oficial de Supabase Auth (`PUT /auth/v1/user`)
    con el propio access token Bearer del usuario y la clave pública `SUPABASE_ANON_KEY`.
    - Nunca utiliza `service_role`.
    - No depende de una sesión local en `db.auth`.
    - Respeta el flujo real de confirmación de cambio de correo configurado en Supabase Auth.
    """
    try:
        validate_supabase_configuration()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    url = f"{SUPABASE_URL}/auth/v1/user"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "apikey": SUPABASE_ANON_KEY,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.put(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Error de comunicación con Supabase Auth al actualizar la cuenta: {exc}",
        ) from exc

    try:
        body = resp.json() if resp.content else {}
    except Exception:
        body = {}

    if resp.status_code in (401, 403):
        msg = (
            body.get("msg")
            or body.get("message")
            or body.get("error_description")
            or "La sesión ha expirado o no es válida. Inicia sesión nuevamente."
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=msg)

    if resp.status_code >= 400:
        msg = (
            body.get("msg")
            or body.get("message")
            or body.get("error_description")
            or f"Supabase Auth rechazó la actualización de cuenta (HTTP {resp.status_code})."
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)

    return body if isinstance(body, dict) else {}


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register_owner(req: RegisterOwnerRequest):
    """
    Registro de dueño en Supabase Auth (`sign_up` público con verificación real de correo)
    + `public.dueno` (y opcionalmente `public.gimnasio` y `public.sucursal` principal).
    Nunca utiliza `service_role` ni auto-confirma correos (`email_confirm: True`).
    """
    anon_client = _require_supabase_anon()
    clean_email = str(req.email).strip().lower()
    password_input = str(req.contrasena or req.password or "").strip()

    if len(password_input) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La contraseña debe tener al menos 6 caracteres.",
        )

    try:
        signup_res = anon_client.auth.sign_up(
            {
                "email": clean_email,
                "password": password_input,
                "options": {
                    "data": {
                        "nombre": req.nombre.strip(),
                        "apellido": req.apellido.strip(),
                    }
                },
            }
        )
        auth_user = getattr(signup_res, "user", None)
        session = getattr(signup_res, "session", None)
    except Exception as exc:
        msg = str(exc)
        if "already" in msg.lower() or "registered" in msg.lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="El email ingresado ya está registrado.",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No se pudo registrar la cuenta en Supabase Auth: {msg}",
        ) from exc

    if not auth_user or not getattr(auth_user, "id", None):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No se pudo inicializar el usuario en Supabase Auth.",
        )

    access_token = getattr(session, "access_token", None) if session else None
    refresh_token = getattr(session, "refresh_token", None) if session else None

    # Si Supabase Auth requiere confirmación de email, NO crear ni vincular perfiles todavía
    if not access_token:
        return {
            "requiere_confirmacion_email": True,
            "mensaje": (
                "Cuenta creada en Supabase Auth. Confirma tu correo electrónico antes de iniciar sesión."
            ),
        }

    user_db = create_supabase_client_for_token(access_token)
    auth_uid = str(auth_user.id)

    try:
        res_dueno = (
            user_db.from_("dueno")
            .insert(
                [
                    {
                        "auth_user_id": auth_uid,
                        "nombre": req.nombre.strip(),
                        "apellido": req.apellido.strip(),
                        "email": clean_email,
                    }
                ]
            )
            .execute()
        )
    except Exception as exc:
        raise_http_from_postgrest_error(exc, "Error al crear el perfil de dueño")

    dueno_rows = get_data(res_dueno) or []
    if not dueno_rows:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al registrar el perfil de dueño.",
        )
    nuevo_dueno = dueno_rows[0]

    nuevo_gimnasio = None
    nueva_sucursal = None
    if req.nombreGimnasio and req.nombreGimnasio.strip():
        gym_payload = {
            "nombre": req.nombreGimnasio.strip(),
            "telefono": req.telefono.strip() if req.telefono else None,
            "email": req.emailGimnasio.strip().lower() if req.emailGimnasio else clean_email,
            "estado": True,
            "id_dueno": nuevo_dueno["id_dueno"],
        }
        try:
            nuevo_gimnasio = insert_gimnasio_with_rls(user_db, gym_payload)
        except Exception as exc:
            raise_http_from_postgrest_error(exc, "Error al crear el gimnasio inicial")
        if nuevo_gimnasio:
            if req.direccion and req.direccion.strip():
                suc_payload = {
                    "direccion": req.direccion.strip(),
                    "telefono": req.telefono.strip() if req.telefono else None,
                    "descripcion": "Sucursal principal",
                    "estado": True,
                    "id_gimnasio": nuevo_gimnasio["id_gimnasio"],
                }
                try:
                    res_suc = user_db.from_("sucursal").insert([suc_payload]).execute()
                except Exception as exc:
                    raise_http_from_postgrest_error(exc, "Error al crear la sucursal principal")
                suc_rows = get_data(res_suc) or []
                if suc_rows:
                    nueva_sucursal = suc_rows[0]

    usuario_resp = {
        **nuevo_dueno,
        "id_dueno": nuevo_dueno["id_dueno"],
        "id_gimnasio": nuevo_gimnasio["id_gimnasio"] if nuevo_gimnasio else None,
        "rol": "dueño",
        "perfiles_disponibles": ["dueño"],
        "gimnasio": nuevo_gimnasio,
        "gimnasios": [nuevo_gimnasio] if nuevo_gimnasio else [],
    }

    mensaje = (
        "Dueño y Gimnasio registrados exitosamente."
        if nuevo_gimnasio
        else "Cuenta de dueño registrada exitosamente."
    )
    return {
        "mensaje": mensaje,
        "token": access_token,
        "refresh_token": refresh_token,
        "usuario": usuario_resp,
        "gimnasio": nuevo_gimnasio,
        "sucursal": nueva_sucursal,
    }


@router.post("/register-socio", status_code=status.HTTP_201_CREATED)
async def register_socio(req: RegisterSocioRequest):
    """
    Auto-registro de socio en Supabase Auth (`sign_up` público con verificación real de correo).
    Seguridad anti-secuestro de fichas presenciales:
    - Nunca utiliza `service_role` ni auto-confirma el correo (`email_confirm: True`).
    - Nunca auto-vincula una ficha presencial preexistente durante el auto-registro:
      la vinculación de fichas presenciales solo puede ocurrir después de confirmar el correo
      mediante una sesión autenticada verificada (`POST /api/auth/vincular-perfil` o login verificado).
    """
    anon_client = _require_supabase_anon()
    clean_email = str(req.email).strip().lower()
    password_input = str(req.contrasena or req.password or "").strip()

    if len(password_input) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La contraseña debe tener al menos 6 caracteres.",
        )

    try:
        signup_res = anon_client.auth.sign_up(
            {
                "email": clean_email,
                "password": password_input,
                "options": {
                    "data": {
                        "nombre": req.nombre.strip(),
                        "apellido": req.apellido.strip(),
                    }
                },
            }
        )
        auth_user = getattr(signup_res, "user", None)
        session = getattr(signup_res, "session", None)
    except Exception as exc:
        msg = str(exc)
        if "already" in msg.lower() or "registered" in msg.lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="El email ingresado ya está registrado.",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error al registrar socio en Supabase Auth: {msg}",
        ) from exc

    access_token = getattr(session, "access_token", None) if session else None
    refresh_token = getattr(session, "refresh_token", None) if session else None
    email_confirmed_at = getattr(auth_user, "email_confirmed_at", None) if auth_user else None

    # Exigir confirmación real del correo antes de emitir sesión de socio o permitir vincular ficha
    if not access_token or not auth_user or not email_confirmed_at:
        return {
            "requiere_confirmacion_email": True,
            "mensaje": (
                "Cuenta creada en Supabase Auth. Confirma tu correo electrónico antes de iniciar sesión "
                "o vincular una ficha presencial existente."
            ),
        }

    user_db = create_supabase_client_for_token(access_token)
    auth_uid = str(auth_user.id)

    try:
        res_socio = (
            user_db.from_("socio")
            .insert(
                [
                    {
                        "auth_user_id": auth_uid,
                        "nombre": req.nombre.strip(),
                        "apellido": req.apellido.strip(),
                        "email": clean_email,
                        "dni": str(req.dni).strip() if req.dni else None,
                        "telefono": str(req.telefono).strip() if req.telefono else None,
                        "estado": True,
                    }
                ]
            )
            .execute()
        )
        rows = get_data(res_socio) or []
        nuevo_socio = rows[0] if rows else None
    except Exception as exc:
        http_status, msg = map_postgrest_error(exc)
        if http_status == status.HTTP_409_CONFLICT:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Ya existe una ficha de socio registrada con ese correo o DNI. "
                    "Inicia sesión con tu correo verificado y vincula tu perfil mediante /api/auth/vincular-perfil."
                ),
            ) from exc
        raise HTTPException(
            status_code=http_status,
            detail=f"No se pudo crear el perfil de socio: {msg}",
        ) from exc

    socio_resp = {
        **(nuevo_socio or {}),
        "rol": "socio",
        "perfiles_disponibles": ["socio"],
        "gimnasio": None,
        "gimnasios": [],
        "id_gimnasio": None,
    }
    return {
        "mensaje": "Cuenta de socio registrada exitosamente.",
        "token": access_token,
        "refresh_token": refresh_token,
        "socio": socio_resp,
        "usuario": socio_resp,
        "gimnasios": [],
    }


@router.post("/login", status_code=status.HTTP_200_OK)
async def login(req: LoginRequest):
    """
    Inicio de sesión con Supabase Auth para personal organizacional y operativo
    (Dueño, Administrador de Gimnasio o Empleado de Sucursal).
    """
    anon_client = _require_supabase_anon()
    password_input = req.contrasena or req.password
    if not req.email or not password_input:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El correo electrónico y la contraseña son obligatorios.",
        )

    clean_email = req.email.strip().lower()
    if not EMAIL_REGEX.match(clean_email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El formato del email ingresado no es válido.",
        )

    try:
        auth_res = anon_client.auth.sign_in_with_password(
            {"email": clean_email, "password": password_input}
        )
        session = getattr(auth_res, "session", None)
        user_obj = getattr(auth_res, "user", None)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas. Verifica tu email y contraseña.",
        ) from exc

    if not session or not getattr(session, "access_token", None) or not user_obj:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas. Verifica tu email y contraseña.",
        )

    access_token = session.access_token
    user_db = create_supabase_client_for_token(access_token)
    auth_identity = _build_auth_identity_from_supabase_user(user_obj, access_token)

    # Resolver contexto; solo intenta vincular ficha precreada si el correo está realmente verificado
    try:
        contexto = await resolver_contexto_negocio(user_db, auth_identity)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_403_FORBIDDEN and auth_identity.get("email_verified"):
            for candidate_tipo in ("dueno", "administrador", "empleado"):
                _try_auto_link_profile(user_db, auth_identity, candidate_tipo)
            contexto = await resolver_contexto_negocio(user_db, auth_identity)
        else:
            raise

    has_staff_profile = any(
        p in (contexto.get("perfiles_disponibles") or [])
        for p in ("dueño", "administrador", "empleado")
    )
    if not has_staff_profile:
        if "socio" in (contexto.get("perfiles_disponibles") or []):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Esta cuenta corresponde a un Socio. Ingresa desde el Portal del Socio.",
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No se encontró un perfil de Dueño, Administrador o Empleado activo asociado a esta cuenta.",
        )

    rol_activo = contexto["rol"]
    if rol_activo == "dueño":
        perfil_base = contexto.get("dueno") or {}
    elif rol_activo == "administrador":
        perfil_base = contexto.get("administrador") or {}
    else:
        perfil_base = contexto.get("empleado") or {}

    usuario_seguro = {
        **perfil_base,
        "auth_user_id": contexto["auth_user_id"],
        "id_dueno": contexto.get("id_dueno"),
        "id_administrador": contexto.get("id_administrador"),
        "id_empleado": contexto.get("id_empleado"),
        "rol": rol_activo,
        "perfiles_disponibles": contexto.get("perfiles_disponibles"),
        "roles_empleado": contexto.get("roles_empleado"),
        "permisos": contexto.get("permisos"),
        "sucursales_asignadas": contexto.get("sucursales_asignadas"),
        "gimnasio": contexto.get("gimnasio"),
        "gimnasios": contexto.get("gimnasios"),
        "id_gimnasio": contexto.get("id_gimnasio"),
        "id_sucursal": contexto.get("id_sucursal"),
    }

    return {
        "mensaje": "Inicio de sesión exitoso.",
        "token": access_token,
        "refresh_token": getattr(session, "refresh_token", None),
        "expires_at": getattr(session, "expires_at", None),
        "usuario": usuario_seguro,
        "gimnasio": contexto.get("gimnasio"),
        "gimnasios": contexto.get("gimnasios"),
        "sucursales_asignadas": contexto.get("sucursales_asignadas"),
    }


@router.post("/login-socio", status_code=status.HTTP_200_OK)
async def login_socio(req: LoginSocioRequest):
    """
    Inicio de sesión en Supabase Auth para Socios (identidad única multi-gimnasio).
    Solo vincula una ficha presencial preexistente si el correo electrónico está verificado en Supabase Auth.
    """
    anon_client = _require_supabase_anon()
    password_input = req.contrasena or req.password
    if not req.email or not password_input:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El correo electrónico y la contraseña son obligatorios.",
        )

    clean_email = req.email.strip().lower()
    if not EMAIL_REGEX.match(clean_email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El formato del email ingresado no es válido.",
        )

    try:
        auth_res = anon_client.auth.sign_in_with_password(
            {"email": clean_email, "password": password_input}
        )
        session = getattr(auth_res, "session", None)
        user_obj = getattr(auth_res, "user", None)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas. Verifica tu correo electrónico y contraseña.",
        ) from exc

    if not session or not getattr(session, "access_token", None) or not user_obj:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas. Verifica tu correo electrónico y contraseña.",
        )

    access_token = session.access_token
    user_db = create_supabase_client_for_token(access_token)
    auth_identity = _build_auth_identity_from_supabase_user(user_obj, access_token)

    try:
        contexto = await resolver_contexto_negocio(user_db, auth_identity, requested_role="socio")
    except HTTPException as exc:
        if (
            exc.status_code == status.HTTP_403_FORBIDDEN
            and auth_identity.get("email_verified")
            and _try_auto_link_profile(user_db, auth_identity, "socio")
        ):
            contexto = await resolver_contexto_negocio(user_db, auth_identity, requested_role="socio")
        else:
            raise

    socio = contexto.get("socio")
    if not socio:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No se encontró un perfil de socio vinculado a esta cuenta.",
        )

    if req.dni and str(req.dni).strip():
        dni_input = re.sub(r"[\.\s-]", "", str(req.dni)).strip()
        dni_socio = re.sub(r"[\.\s-]", "", str(socio.get("dni") or "")).strip()
        if dni_socio and dni_input != dni_socio:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="El DNI ingresado no coincide con el registrado en tu cuenta.",
            )

    socio_seguro = {
        "id_socio": socio.get("id_socio"),
        "auth_user_id": contexto["auth_user_id"],
        "id_gimnasio": contexto.get("id_gimnasio"),
        "id_socio_gimnasio": contexto.get("id_socio_gimnasio"),
        "nombre": socio.get("nombre"),
        "apellido": socio.get("apellido"),
        "dni": socio.get("dni"),
        "email": socio.get("email"),
        "telefono": socio.get("telefono"),
        "fecha_alta": socio.get("fecha_alta"),
        "estado": socio.get("estado"),
        "rol": "socio",
        "perfiles_disponibles": contexto.get("perfiles_disponibles"),
        "gimnasio": contexto.get("gimnasio"),
        "gimnasios": contexto.get("gimnasios"),
    }

    return {
        "mensaje": "Inicio de sesión exitoso.",
        "token": access_token,
        "refresh_token": getattr(session, "refresh_token", None),
        "expires_at": getattr(session, "expires_at", None),
        "usuario": socio_seguro,
        "socio": socio_seguro,
        "gimnasios": contexto.get("gimnasios"),
    }


@router.post("/refresh", status_code=status.HTTP_200_OK)
async def refresh_session(req: RefreshTokenRequest):
    """Renueva el access token de Supabase Auth utilizando un refresh_token válido."""
    anon_client = _require_supabase_anon()
    try:
        res = anon_client.auth.refresh_session(req.refresh_token.strip())
        session = getattr(res, "session", None)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No se pudo renovar la sesión. Inicia sesión nuevamente.",
        ) from exc

    if not session or not getattr(session, "access_token", None):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No se pudo renovar la sesión. Inicia sesión nuevamente.",
        )

    return {
        "token": session.access_token,
        "refresh_token": getattr(session, "refresh_token", None),
        "expires_at": getattr(session, "expires_at", None),
    }


@router.post("/vincular-perfil", status_code=status.HTTP_200_OK)
async def vincular_identidad_perfil(
    req: VincularPerfilRequest,
    auth_user: Dict[str, Any] = Depends(get_current_auth_user),
    db: Client = Depends(get_user_supabase_client),
):
    """
    Vincula la identidad de Supabase Auth autenticada (con correo realmente verificado) a un perfil
    preexistente (`socio`, `administrador`, `empleado` o `dueno`) mediante la RPC oficial
    `public.rpc_vincular_identidad_perfil`.
    """
    if not auth_user.get("email_verified"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Debes verificar tu correo electrónico en Supabase Auth antes de vincular un perfil preexistente.",
        )

    tipo_normalizado = "dueno" if req.perfil_tipo in ("dueno", "dueño") else req.perfil_tipo
    try:
        res = db.rpc("rpc_vincular_identidad_perfil", {"p_perfil_tipo": tipo_normalizado}).execute()
        resultado = get_data(res)
    except Exception as exc:
        raise_http_from_postgrest_error(exc, f"No fue posible vincular el perfil '{tipo_normalizado}'")

    return {
        "mensaje": f"Perfil '{tipo_normalizado}' vinculado correctamente a tu cuenta.",
        "resultado": resultado,
        "auth_user_id": auth_user["auth_user_id"],
    }


@router.get("/me", status_code=status.HTTP_200_OK)
async def get_me(current_user: Dict[str, Any] = Depends(get_current_user)):
    """
    Devuelve el perfil activo, todos los perfiles disponibles, gimnasios y sucursales
    autorizadas del usuario autenticado consultando a través de su cliente RLS.
    """
    rol = current_user.get("rol")
    db: Client = current_user["supabase"]

    if rol == "socio" and current_user.get("socio"):
        socio = current_user["socio"]
        try:
            # Join real PostgREST respetando FKs compuestas:
            # membresia_socio -> socio_gimnasio (FK compuesta) -> gimnasio (FK directa)
            res_m = (
                db.from_("membresia_socio")
                .select(
                    "id_membresia_socio, id_socio_gimnasio, id_gimnasio, id_membresia_gimnasio, "
                    "precio, duracion_dias_contratados, limite_asistencias_semanales, "
                    "limite_asistencias_totales, fecha_inicio, fecha_vencimiento, estado, "
                    "socio_gimnasio!inner("
                    "id_socio_gimnasio, id_socio, id_gimnasio, estado, "
                    "gimnasio(id_gimnasio, nombre, telefono, email, estado)"
                    "), "
                    "membresia_gimnasio(id_membresia_gimnasio, nombre, descripcion)"
                )
                .eq("socio_gimnasio.id_socio", socio["id_socio"])
                .order("fecha_vencimiento", desc=True)
                .execute()
            )
            raw_membresias = get_data(res_m)
        except Exception as exc:
            raise_http_from_postgrest_error(exc, "Error al consultar las membresías del socio")

        membresias = raw_membresias if isinstance(raw_membresias, list) else []

        socio_resp = {
            **socio,
            "rol": "socio",
            "perfiles_disponibles": current_user.get("perfiles_disponibles"),
            "id_gimnasio": current_user.get("id_gimnasio"),
            "id_socio_gimnasio": current_user.get("id_socio_gimnasio"),
            "gimnasio": current_user.get("gimnasio"),
            "gimnasios": current_user.get("gimnasios"),
        }
        return {
            "usuario": socio_resp,
            "gimnasios": current_user.get("gimnasios"),
            "membresias": membresias,
        }

    if rol == "empleado" and current_user.get("empleado"):
        emp = current_user["empleado"]
        emp_resp = {
            **emp,
            "rol": "empleado",
            "perfiles_disponibles": current_user.get("perfiles_disponibles"),
            "roles_empleado": current_user.get("roles_empleado"),
            "permisos": current_user.get("permisos"),
            "sucursales_asignadas": current_user.get("sucursales_asignadas"),
            "id_gimnasio": current_user.get("id_gimnasio"),
            "id_sucursal": current_user.get("id_sucursal"),
            "gimnasio": current_user.get("gimnasio"),
            "gimnasios": current_user.get("gimnasios"),
        }
        return {
            "usuario": emp_resp,
            "gimnasios": current_user.get("gimnasios"),
            "sucursales_asignadas": current_user.get("sucursales_asignadas"),
        }

    if rol == "administrador" and current_user.get("administrador"):
        admin = current_user["administrador"]
        admin_resp = {
            **admin,
            "rol": "administrador",
            "perfiles_disponibles": current_user.get("perfiles_disponibles"),
            "permisos": current_user.get("permisos"),
            "administrador_gimnasios": current_user.get("administrador_gimnasios"),
            "id_gimnasio": current_user.get("id_gimnasio"),
            "id_sucursal": current_user.get("id_sucursal"),
            "gimnasio": current_user.get("gimnasio"),
            "gimnasios": current_user.get("gimnasios"),
            "sucursales": current_user.get("sucursales"),
        }
        return {
            "usuario": admin_resp,
            "gimnasios": current_user.get("gimnasios"),
            "sucursales": current_user.get("sucursales"),
        }

    # Caso Dueño
    dueno = current_user.get("dueno") or {}
    estadisticas = None
    active_gym_id = current_user.get("id_gimnasio")
    if active_gym_id:
        try:
            res_sg = (
                db.from_("socio_gimnasio")
                .select("id_socio_gimnasio, id_socio, estado")
                .eq("id_gimnasio", active_gym_id)
                .execute()
            )
            sg_list = get_data(res_sg) or []
            total_socios = len(sg_list)
            socios_activos = sum(1 for s in sg_list if s.get("estado") is True)
            sucs_gym = [
                s for s in (current_user.get("sucursales") or [])
                if s.get("id_gimnasio") == active_gym_id
            ]
            estadisticas = {
                "totalSocios": total_socios,
                "sociosActivos": socios_activos,
                "totalSucursales": len(sucs_gym),
                "sucursalesActivas": sum(1 for s in sucs_gym if s.get("estado") is not False),
            }
        except Exception as exc:
            raise_http_from_postgrest_error(exc, "Error al consultar estadísticas del gimnasio")

    dueno_resp = {
        **dueno,
        "rol": "dueño",
        "perfiles_disponibles": current_user.get("perfiles_disponibles"),
        "permisos": current_user.get("permisos"),
        "id_gimnasio": active_gym_id,
        "gimnasio": current_user.get("gimnasio"),
        "gimnasios": current_user.get("gimnasios"),
        "sucursales": current_user.get("sucursales"),
        "estadisticas": estadisticas,
    }
    return {
        "usuario": dueno_resp,
        "gimnasios": current_user.get("gimnasios"),
    }


@router.put("/me", status_code=status.HTTP_200_OK)
@router.put("/perfil", status_code=status.HTTP_200_OK)
async def update_owner_profile(
    req: UpdateOwnerProfileRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Actualiza los datos personales del perfil activo en su tabla definitiva
    (`public.dueno`, `public.administrador`, `public.empleado` o `public.socio`)
    respetando RLS, y gestiona cambios de contraseña o solicitudes de cambio de correo
    exclusivamente mediante el access token del usuario autenticado (`PUT /auth/v1/user`),
    sin usar `service_role` ni depender de una sesión GoTrue local.
    """
    rol = current_user.get("rol")
    db: Client = current_user["supabase"]
    access_token: str = str(current_user.get("access_token") or "").strip()

    table_map = {
        "dueño": ("dueno", "id_dueno", current_user.get("id_dueno"), current_user.get("dueno") or {}),
        "administrador": (
            "administrador",
            "id_administrador",
            current_user.get("id_administrador"),
            current_user.get("administrador") or {},
        ),
        "empleado": ("empleado", "id_empleado", current_user.get("id_empleado"), current_user.get("empleado") or {}),
        "socio": ("socio", "id_socio", current_user.get("id_socio"), current_user.get("socio") or {}),
    }

    if rol not in table_map or not table_map[rol][2]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No se encontró un perfil activo autorizado para actualizar.",
        )

    table_name, pk_col, pk_val, current_profile = table_map[rol]
    update_fields: Dict[str, Any] = {}

    if req.nombre is not None:
        if not req.nombre.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre es obligatorio.")
        update_fields["nombre"] = req.nombre.strip()

    if req.apellido is not None:
        if not req.apellido.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El apellido es obligatorio.")
        update_fields["apellido"] = req.apellido.strip()

    if req.telefono is not None and table_name in ("administrador", "empleado", "socio"):
        update_fields["telefono"] = req.telefono.strip() if req.telefono.strip() else None

    if req.dni is not None and table_name in ("administrador", "empleado", "socio"):
        update_fields["dni"] = req.dni.strip() if req.dni.strip() else None

    email_cambio: Optional[str] = None
    if req.email is not None:
        clean_email = req.email.strip().lower()
        if not EMAIL_REGEX.match(clean_email):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El formato del email no es válido.")
        current_email = str(current_profile.get("email") or current_user.get("email") or "").lower()
        if clean_email != current_email:
            email_cambio = clean_email

    nuevo_token: Optional[str] = None
    nuevo_refresh_token: Optional[str] = None
    cambio_email_pendiente: bool = False

    if req.nueva_contrasena and req.nueva_contrasena.strip():
        pass_limpia = req.nueva_contrasena.strip()
        if len(pass_limpia) < 6:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La nueva contraseña debe tener al menos 6 caracteres.",
            )
        if not req.contrasena_actual:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Debes ingresar tu contraseña actual para confirmar el cambio.",
            )
        if not access_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Sesión inválida o expirada. Inicia sesión nuevamente.",
            )

        anon_client = _require_supabase_anon()
        user_email = current_user.get("email") or current_profile.get("email")
        try:
            anon_client.auth.sign_in_with_password(
                {"email": user_email, "password": req.contrasena_actual}
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La contraseña actual ingresada es incorrecta.",
            ) from exc

        # Actualizar contraseña vía endpoint oficial de Supabase Auth con el propio Bearer token del usuario
        await _update_supabase_auth_user_with_token(access_token, {"password": pass_limpia})

        try:
            re_auth = anon_client.auth.sign_in_with_password(
                {"email": user_email, "password": pass_limpia}
            )
            re_session = getattr(re_auth, "session", None)
            if re_session:
                nuevo_token = getattr(re_session, "access_token", None)
                nuevo_refresh_token = getattr(re_session, "refresh_token", None)
        except Exception:
            pass

    if email_cambio:
        if not access_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Sesión inválida o expirada. Inicia sesión nuevamente.",
            )
        # Solicitar cambio de correo a Supabase Auth respetando confirmación real (jamás email_confirm=True)
        auth_upd = await _update_supabase_auth_user_with_token(
            nuevo_token or access_token,
            {"email": email_cambio},
        )
        new_email_pending = str(auth_upd.get("new_email") or "").strip().lower()
        confirmed_email_now = str(auth_upd.get("email") or "").strip().lower()
        if confirmed_email_now == email_cambio and not new_email_pending:
            update_fields["email"] = email_cambio
        else:
            cambio_email_pendiente = True

    if not update_fields and not nuevo_token and not cambio_email_pendiente and not (req.nueva_contrasena and req.nueva_contrasena.strip()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se enviaron datos para actualizar.",
        )

    updated_profile = dict(current_profile)
    if update_fields:
        try:
            res_upd = (
                db.from_(table_name)
                .update(update_fields)
                .eq(pk_col, pk_val)
                .execute()
            )
        except Exception as exc:
            raise_http_from_postgrest_error(exc, "Error al actualizar los datos del perfil")
        rows = get_data(res_upd) or []
        if not rows:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Perfil no encontrado o sin permisos para actualizar.",
            )
        updated_profile = rows[0]

    resp_user = {
        **updated_profile,
        "rol": rol,
        "id_gimnasio": current_user.get("id_gimnasio"),
        "gimnasio": current_user.get("gimnasio"),
        "gimnasios": current_user.get("gimnasios"),
    }

    mensaje = "Perfil actualizado exitosamente."
    if cambio_email_pendiente:
        mensaje = (
            "Solicitud de cambio de correo enviada. Confirma el nuevo correo electrónico "
            "desde el enlace enviado por Supabase Auth para completar el cambio."
        )

    response_payload: Dict[str, Any] = {
        "mensaje": mensaje,
        "usuario": resp_user,
    }
    if cambio_email_pendiente:
        response_payload["cambio_email_pendiente_confirmacion"] = True
        response_payload["email_pendiente"] = email_cambio
    if nuevo_token:
        response_payload["token"] = nuevo_token
        response_payload["refresh_token"] = nuevo_refresh_token

    return response_payload
