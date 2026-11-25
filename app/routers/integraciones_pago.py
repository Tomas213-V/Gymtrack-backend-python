"""
Router de Integración de Pagos por Gimnasio (Mercado Pago OAuth oficial).

Tabla existente (sin modificar esquema): `public.integracion_pago_gimnasio`
Columnas utilizadas:
  id_integracion_pago, id_gimnasio, proveedor, external_user_id,
  access_token_encrypted, refresh_token_encrypted, token_expires_at,
  estado, fecha_conexion, fecha_actualizacion

Reglas de seguridad:
- Nunca expone `access_token`, `refresh_token`, `access_token_encrypted` ni `refresh_token_encrypted`.
- Valida permisos de administración del gimnasio (`verify_dueno_or_org_admin_of_gimnasio`)
  antes de consultar, iniciar, completar (callback) o desconectar una integración.
- Impide conectar más de una cuenta de Mercado Pago por gimnasio (salvo revinculación explícita).
- Valida `state` firmado + nonce de un solo uso para evitar ataques CSRF y replay.
- La desconexión jamás elimina registros de `public.pago`.
"""

from datetime import datetime, timedelta, timezone
import threading
import urllib.parse
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from supabase import Client

import app.config as app_config
from app.core.dependencies import (
    get_current_user,
    resolver_contexto_negocio,
)
from app.core.security import verify_supabase_access_token
from app.models.integracion_pago import (
    CallbackSimuladoMPRequest,
    DesconectarMPRequest,
    IniciarConexionMPRequest,
)
from app.routers.gimnasios import verify_dueno_or_org_admin_of_gimnasio
from app.services.mercadopago_oauth import (
    PROVEEDOR_MERCADOPAGO,
    build_mercadopago_authorization_url,
    consume_and_validate_oauth_state,
    create_oauth_state,
    encrypt_oauth_token,
    exchange_mercadopago_oauth_code,
    get_demo_account_public_info,
    get_mercadopago_oauth_config_status,
    is_mercadopago_demo_mode,
    require_mercadopago_oauth_configured,
)
from app.services.utils import get_data, raise_http_from_postgrest_error

router = APIRouter(
    prefix="/api/integraciones/mercadopago",
    tags=["Integración de Pagos (Mercado Pago)"],
)

# Almacén en memoria fallback exclusivamente para modo presentación cuando no hay service_role local
_DEMO_STORE_LOCK = threading.Lock()
_DEMO_INTEGRATIONS_BY_GYM: Dict[int, Dict[str, Any]] = {}
_DEMO_SEQ = 100


def reset_demo_integrations_store() -> None:
    with _DEMO_STORE_LOCK:
        _DEMO_INTEGRATIONS_BY_GYM.clear()

# Exclusivamente columnas NO sensibles para lectura y respuesta HTTP
INTEGRACION_PUBLIC_COLUMNS = (
    "id_integracion_pago, id_gimnasio, proveedor, external_user_id, "
    "token_expires_at, estado, fecha_conexion, fecha_actualizacion"
)


def _resolve_target_gym_id(
    current_user: Dict[str, Any],
    explicit_gym_id: Optional[int] = None,
) -> tuple[Dict[str, Any], int]:
    """Resuelve y autoriza el `id_gimnasio` objetivo exigiendo permisos de Dueño o Admin Organizacional."""
    target_gym_id = explicit_gym_id or current_user.get("id_gimnasio")
    if not target_gym_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes seleccionar un gimnasio válido para gestionar la integración de pagos.",
        )
    mgr_ctx = verify_dueno_or_org_admin_of_gimnasio(current_user, int(target_gym_id))
    return mgr_ctx, int(target_gym_id)


def _get_admin_db_or_503() -> Client:
    """
    Retorna el cliente administrativo de Supabase (`service_role`) para operaciones
    exclusivas de servidor sobre `public.integracion_pago_gimnasio` (RLS habilitada sin
    permisos a `anon` ni `authenticated`).
    Solo debe llamarse tras autenticar al usuario y verificar su pertenencia/rol sobre el gimnasio.
    """
    try:
        return app_config.get_supabase_admin_client()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


def _get_admin_db_or_none() -> Optional[Client]:
    try:
        return app_config.get_supabase_admin_client()
    except Exception:
        return None


def _fetch_mp_integration_public(admin_db: Optional[Client], gym_id: int) -> Optional[Dict[str, Any]]:
    if admin_db is None:
        with _DEMO_STORE_LOCK:
            row = _DEMO_INTEGRATIONS_BY_GYM.get(int(gym_id))
            if not row:
                return None
            return {
                k: v for k, v in row.items()
                if k not in ("access_token_encrypted", "refresh_token_encrypted")
            }
    res = (
        admin_db.from_("integracion_pago_gimnasio")
        .select(INTEGRACION_PUBLIC_COLUMNS)
        .eq("id_gimnasio", int(gym_id))
        .eq("proveedor", PROVEEDOR_MERCADOPAGO)
        .maybe_single()
        .execute()
    )
    row = get_data(res)
    return row if isinstance(row, dict) else None


def _sanitize_public_integration(row: Optional[Dict[str, Any]], gym_id: int) -> Dict[str, Any]:
    """Construye la respuesta pública sin exponer jamás tokens, secretos ni campos cifrados."""
    oauth_status = get_mercadopago_oauth_config_status()
    oauth_ready = bool(oauth_status["configurado"])
    demo_mode = is_mercadopago_demo_mode()
    demo_info = get_demo_account_public_info(gym_id) if demo_mode else None
    modo = "oauth_real" if oauth_ready else ("simulacion_prueba" if demo_mode else "no_configurado")

    if not row:
        return {
            "id_gimnasio": int(gym_id),
            "proveedor": PROVEEDOR_MERCADOPAGO,
            "conectado": False,
            "estado": "no_conectado",
            "oauth_configurado": oauth_ready,
            "simulacion_disponible": demo_mode,
            "modo": modo,
            "cuenta_prueba": demo_info,
            "variables_faltantes": oauth_status["variables_faltantes"],
            "variables_invalidas": oauth_status["variables_invalidas"],
            "configuracion_mensaje": oauth_status["mensaje"],
            "cuenta": None,
        }

    raw_estado = str(row.get("estado") or "").strip().lower()
    if raw_estado in ("conectado", "activo", "connected"):
        estado_norm = "conectado"
        conectado = True
    elif raw_estado == "error":
        estado_norm = "error"
        conectado = False
    else:
        estado_norm = "no_conectado"
        conectado = False

    cuenta_publica = None
    if conectado or estado_norm == "error":
        cuenta_publica = {
            "id_integracion_pago": row.get("id_integracion_pago"),
            "external_user_id": row.get("external_user_id"),
            "alias": demo_info["alias"] if demo_info else None,
            "fecha_conexion": row.get("fecha_conexion"),
            "fecha_actualizacion": row.get("fecha_actualizacion"),
            "token_expires_at": row.get("token_expires_at"),
        }

    return {
        "id_gimnasio": int(gym_id),
        "proveedor": PROVEEDOR_MERCADOPAGO,
        "conectado": conectado,
        "estado": estado_norm,
        "oauth_configurado": oauth_ready,
        "simulacion_disponible": demo_mode,
        "modo": modo,
        "cuenta_prueba": demo_info,
        "variables_faltantes": oauth_status["variables_faltantes"],
        "variables_invalidas": oauth_status["variables_invalidas"],
        "configuracion_mensaje": oauth_status["mensaje"],
        "cuenta": cuenta_publica,
    }


def _build_frontend_callback_redirect(mp_status: str, mp_reason: Optional[str] = None) -> str:
    base = str(getattr(app_config, "FRONTEND_URL", "http://localhost:5173") or "http://localhost:5173").rstrip("/")
    params: Dict[str, str] = {
        "tab": "integracion-pagos",
        "mp_status": mp_status,
    }
    if mp_reason:
        params["mp_reason"] = mp_reason
    return f"{base}/perfil-dueño?{urllib.parse.urlencode(params)}"


@router.get("", status_code=status.HTTP_200_OK)
async def obtener_estado_integracion_mp(
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Consulta el estado de conexión de Mercado Pago del gimnasio autorizado.
    - Autentica y valida pertenencia/rol del usuario con su cliente RLS (`authenticated`).
    - Consulta la tabla privada `public.integracion_pago_gimnasio` desde el servidor con
      `service_role` (`supabase_admin`), filtrando estrictamente por `id_gimnasio` y `proveedor`.
    - Funciona incluso cuando las credenciales OAuth todavía no están configuradas, informando
      qué variables faltan sin revelar secretos.
    - Nunca devuelve `access_token`, `refresh_token` ni valores cifrados.
    """
    _mgr_ctx, gym_id = _resolve_target_gym_id(current_user, explicit_gym_id=id_gimnasio)
    admin_db: Optional[Client] = _get_admin_db_or_none() if is_mercadopago_demo_mode() else _get_admin_db_or_503()

    try:
        row = _fetch_mp_integration_public(admin_db, gym_id)
    except Exception as exc:
        raise_http_from_postgrest_error(exc, "Error al consultar la integración de Mercado Pago")

    return _sanitize_public_integration(row, gym_id)


@router.post("/conectar", status_code=status.HTTP_200_OK)
async def iniciar_conexion_mp(
    req: Optional[IniciarConexionMPRequest] = Body(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Inicia el flujo OAuth oficial de Mercado Pago (o la vinculación simulada con cuenta de prueba
    cuando el servidor corre en modo presentación/demo):
    1. Valida permisos de administración del usuario sobre el gimnasio.
    2. Si OAuth real está configurado, exige `service_role` y redirige a Mercado Pago oficial.
    3. Si está en modo presentación (`MERCADOPAGO_OAUTH_DEMO_MODE=true`), emite `state` firmado
       y datos no sensibles de la cuenta de prueba para confirmar en modal.
    4. Impide duplicar cuenta si ya hay una conectada (salvo `reconectar=True`).
    """
    explicit_gid = req.id_gimnasio if req else None
    reconectar = bool(req.reconectar) if req else False

    mgr_ctx, gym_id = _resolve_target_gym_id(current_user, explicit_gym_id=explicit_gid)
    demo_mode = is_mercadopago_demo_mode()

    if demo_mode:
        admin_db: Optional[Client] = _get_admin_db_or_none()
    else:
        require_mercadopago_oauth_configured()
        admin_db = _get_admin_db_or_503()

    try:
        existing = _fetch_mp_integration_public(admin_db, gym_id)
    except Exception as exc:
        raise_http_from_postgrest_error(exc, "Error al verificar la integración actual de Mercado Pago")

    if existing and str(existing.get("estado") or "").strip().lower() in ("conectado", "activo", "connected") and not reconectar:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Este gimnasio ya posee una cuenta de Mercado Pago conectada. "
                "Desconectala primero o utilizá la opción de volver a vincular."
            ),
        )

    state_value = create_oauth_state(
        auth_user_id=str(mgr_ctx["auth_user_id"]),
        id_gimnasio=gym_id,
        rol=str(mgr_ctx.get("rol") or "dueño"),
        user_access_token=str(mgr_ctx["access_token"]),
        reconectar=reconectar,
    )

    if demo_mode:
        return {
            "id_gimnasio": gym_id,
            "proveedor": PROVEEDOR_MERCADOPAGO,
            "modo": "simulacion_prueba",
            "state": state_value,
            "cuenta_prueba": get_demo_account_public_info(gym_id),
        }

    authorization_url = build_mercadopago_authorization_url(state_value)

    return {
        "id_gimnasio": gym_id,
        "proveedor": PROVEEDOR_MERCADOPAGO,
        "modo": "oauth_real",
        "authorization_url": authorization_url,
    }


@router.post("/callback-simulado", status_code=status.HTTP_200_OK)
async def callback_oauth_mp_simulado(
    req: CallbackSimuladoMPRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Completa la vinculación simulada de cuenta de prueba de Mercado Pago para presentaciones.
    Valida `state` anti-CSRF, permisos reales sobre el gimnasio y almacena tokens cifrados con Fernet.
    """
    global _DEMO_SEQ
    if not is_mercadopago_demo_mode():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El modo de vinculación simulada no está habilitado en este servidor.",
        )

    state_record = consume_and_validate_oauth_state(req.state)
    gym_id = int(state_record["id_gimnasio"])
    reconectar = bool(state_record.get("reconectar"))

    if str(current_user.get("auth_user_id")) != str(state_record["auth_user_id"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El estado OAuth no pertenece a la sesión autenticada actual.",
        )

    _mgr_ctx, _ = _resolve_target_gym_id(current_user, explicit_gym_id=gym_id)
    admin_db: Optional[Client] = _get_admin_db_or_none()

    if not req.autorizado:
        return {
            "mensaje": "Cancelaste la autorización en Mercado Pago. Tu cuenta no fue vinculada.",
            "mp_status": "error",
            "mp_reason": "access_denied",
            **_sanitize_public_integration(_fetch_mp_integration_public(admin_db, gym_id), gym_id),
        }

    demo_info = get_demo_account_public_info(gym_id)
    raw_test_token = str(getattr(app_config, "MERCADOPAGO_ACCESS_TOKEN", "") or "").strip() or f"TEST-SANDBOX-TOKEN-GYM-{gym_id}"
    access_enc = encrypt_oauth_token(raw_test_token)
    refresh_enc = encrypt_oauth_token(f"TEST-SANDBOX-REFRESH-GYM-{gym_id}")

    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    expires_iso = (now_dt + timedelta(days=180)).isoformat()

    if admin_db is None:
        with _DEMO_STORE_LOCK:
            existing_mem = _DEMO_INTEGRATIONS_BY_GYM.get(gym_id)
            if existing_mem and existing_mem.get("estado") == "conectado" and not reconectar:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Este gimnasio ya posee una cuenta de Mercado Pago conectada.",
                )
            integ_id = existing_mem["id_integracion_pago"] if existing_mem else (_DEMO_SEQ := _DEMO_SEQ + 1)
            _DEMO_INTEGRATIONS_BY_GYM[gym_id] = {
                "id_integracion_pago": integ_id,
                "id_gimnasio": gym_id,
                "proveedor": PROVEEDOR_MERCADOPAGO,
                "external_user_id": demo_info["external_user_id"],
                "access_token_encrypted": access_enc,
                "refresh_token_encrypted": refresh_enc,
                "token_expires_at": expires_iso,
                "estado": "conectado",
                "fecha_conexion": now_iso,
                "fecha_actualizacion": now_iso,
            }
        updated = _fetch_mp_integration_public(None, gym_id)
    else:
        try:
            existing = _fetch_mp_integration_public(admin_db, gym_id)
            if existing and existing.get("id_integracion_pago") is not None:
                est_actual = str(existing.get("estado") or "").strip().lower()
                if est_actual in ("conectado", "activo", "connected") and not reconectar:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Este gimnasio ya posee una cuenta de Mercado Pago conectada.",
                    )
                (
                    admin_db.from_("integracion_pago_gimnasio")
                    .update(
                        {
                            "external_user_id": demo_info["external_user_id"],
                            "access_token_encrypted": access_enc,
                            "refresh_token_encrypted": refresh_enc,
                            "token_expires_at": expires_iso,
                            "estado": "conectado",
                            "fecha_conexion": now_iso,
                            "fecha_actualizacion": now_iso,
                        }
                    )
                    .eq("id_integracion_pago", existing["id_integracion_pago"])
                    .eq("id_gimnasio", gym_id)
                    .eq("proveedor", PROVEEDOR_MERCADOPAGO)
                    .execute()
                )
            else:
                admin_db.from_("integracion_pago_gimnasio").insert(
                    [
                        {
                            "id_gimnasio": gym_id,
                            "proveedor": PROVEEDOR_MERCADOPAGO,
                            "external_user_id": demo_info["external_user_id"],
                            "access_token_encrypted": access_enc,
                            "refresh_token_encrypted": refresh_enc,
                            "token_expires_at": expires_iso,
                            "estado": "conectado",
                            "fecha_conexion": now_iso,
                            "fecha_actualizacion": now_iso,
                        }
                    ]
                ).execute()
            updated = _fetch_mp_integration_public(admin_db, gym_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise_http_from_postgrest_error(exc, "No se pudo guardar la vinculación de Mercado Pago en base de datos")

    return {
        "mensaje": "Cuenta de prueba de Mercado Pago vinculada correctamente a tu gimnasio.",
        "mp_status": "connected",
        **_sanitize_public_integration(updated, gym_id),
    }


@router.get("/callback")
async def callback_oauth_mp(
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
    error_description: Optional[str] = Query(default=None),
):
    """
    Callback oficial de Mercado Pago OAuth:
    1. Valida y consume el `state` (anti-CSRF / anti-replay). Si el `state` es inválido -> 400.
    2. Revalida que el dueño/admin del `state` siga autorizado sobre el gimnasio.
    3. Intercambia el `code` por tokens en Mercado Pago, cifra ambos tokens y los persiste en
       `public.integracion_pago_gimnasio` asegurando 1 sola fila por gimnasio y proveedor.
    4. Redirige de vuelta al perfil del gimnasio en el frontend sin exponer tokens ni códigos.
    """
    # 1. Validación estricta de state (rechaza CSRF / replay con 400 Bad Request)
    state_record = consume_and_validate_oauth_state(state)
    gym_id = int(state_record["id_gimnasio"])
    user_access_token = str(state_record["user_access_token"])
    expected_uid = str(state_record["auth_user_id"])
    reconectar = bool(state_record.get("reconectar"))

    # 2. Revalidar permisos reales del usuario sobre el gimnasio antes de completar la integración
    try:
        auth_identity = verify_supabase_access_token(user_access_token)
        if str(auth_identity.get("auth_user_id")) != expected_uid:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="La identidad de la sesión no coincide con el estado OAuth.",
            )
        user_db = app_config.create_supabase_client_for_token(user_access_token)
        contexto = await resolver_contexto_negocio(
            db=user_db,
            auth_user=auth_identity,
            requested_gym_id=gym_id,
            use_cache=False,
            store_cache=False,
        )
        verify_dueno_or_org_admin_of_gimnasio(contexto, gym_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No fue posible verificar tus permisos sobre el gimnasio al completar OAuth.",
        ) from exc

    # Si el usuario canceló o Mercado Pago devolvió error en la pantalla de autorización
    if error:
        safe_reason = "access_denied" if "denied" in str(error).lower() else "oauth_error"
        return RedirectResponse(
            url=_build_frontend_callback_redirect("error", safe_reason),
            status_code=status.HTTP_302_FOUND,
        )

    if not code or not str(code).strip():
        return RedirectResponse(
            url=_build_frontend_callback_redirect("error", "missing_code"),
            status_code=status.HTTP_302_FOUND,
        )

    # 3. Intercambiar código por tokens en Mercado Pago y cifrar antes de persistir
    try:
        token_data = await exchange_mercadopago_oauth_code(code)
        access_enc = encrypt_oauth_token(token_data["access_token"])
        refresh_enc = (
            encrypt_oauth_token(token_data["refresh_token"])
            if token_data.get("refresh_token")
            else None
        )
    except HTTPException:
        return RedirectResponse(
            url=_build_frontend_callback_redirect("error", "token_exchange_failed"),
            status_code=status.HTTP_302_FOUND,
        )

    # 4. Persistir en `public.integracion_pago_gimnasio` con `service_role` (máximo 1 cuenta MP por gimnasio)
    now_iso = datetime.now(timezone.utc).isoformat()
    payload_db = {
        "id_gimnasio": gym_id,
        "proveedor": PROVEEDOR_MERCADOPAGO,
        "external_user_id": token_data["external_user_id"],
        "access_token_encrypted": access_enc,
        "refresh_token_encrypted": refresh_enc,
        "token_expires_at": token_data.get("token_expires_at"),
        "estado": "conectado",
        "fecha_conexion": now_iso,
        "fecha_actualizacion": now_iso,
    }

    try:
        admin_db: Client = _get_admin_db_or_503()
        existing = _fetch_mp_integration_public(admin_db, gym_id)
        if existing and existing.get("id_integracion_pago") is not None:
            est_actual = str(existing.get("estado") or "").strip().lower()
            if est_actual in ("conectado", "activo", "connected") and not reconectar:
                return RedirectResponse(
                    url=_build_frontend_callback_redirect("error", "already_connected"),
                    status_code=status.HTTP_302_FOUND,
                )
            (
                admin_db.from_("integracion_pago_gimnasio")
                .update(
                    {
                        "external_user_id": payload_db["external_user_id"],
                        "access_token_encrypted": payload_db["access_token_encrypted"],
                        "refresh_token_encrypted": payload_db["refresh_token_encrypted"],
                        "token_expires_at": payload_db["token_expires_at"],
                        "estado": "conectado",
                        "fecha_conexion": now_iso,
                        "fecha_actualizacion": now_iso,
                    }
                )
                .eq("id_integracion_pago", existing["id_integracion_pago"])
                .eq("id_gimnasio", gym_id)
                .eq("proveedor", PROVEEDOR_MERCADOPAGO)
                .execute()
            )
        else:
            table_builder = admin_db.from_("integracion_pago_gimnasio")
            try:
                table_builder.insert([payload_db], returning="minimal").execute()
            except TypeError:
                table_builder.insert([payload_db]).execute()
    except Exception:
        return RedirectResponse(
            url=_build_frontend_callback_redirect("error", "storage_error"),
            status_code=status.HTTP_302_FOUND,
        )

    return RedirectResponse(
        url=_build_frontend_callback_redirect("connected"),
        status_code=status.HTTP_302_FOUND,
    )


async def _desconectar_mp_logic(
    current_user: Dict[str, Any],
    explicit_gym_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Desconecta la integración de Mercado Pago del gimnasio limpiando los tokens cifrados
    y marcando `estado = 'desconectado'` mediante el cliente administrativo (`service_role`),
    siempre tras validar pertenencia/rol del usuario sobre el gimnasio.
    Jamás elimina ni altera `public.pago`.
    """
    _mgr_ctx, gym_id = _resolve_target_gym_id(current_user, explicit_gym_id=explicit_gym_id)
    admin_db: Optional[Client] = _get_admin_db_or_none() if is_mercadopago_demo_mode() else _get_admin_db_or_503()

    try:
        existing = _fetch_mp_integration_public(admin_db, gym_id)
    except Exception as exc:
        raise_http_from_postgrest_error(exc, "Error al consultar la integración de Mercado Pago")

    if not existing or existing.get("id_integracion_pago") is None:
        return {
            "mensaje": "El gimnasio no tenía una cuenta de Mercado Pago conectada.",
            **_sanitize_public_integration(None, gym_id),
        }

    now_iso = datetime.now(timezone.utc).isoformat()
    if admin_db is None:
        with _DEMO_STORE_LOCK:
            mem_row = _DEMO_INTEGRATIONS_BY_GYM.get(gym_id)
            if mem_row:
                mem_row["access_token_encrypted"] = None
                mem_row["refresh_token_encrypted"] = None
                mem_row["token_expires_at"] = None
                mem_row["estado"] = "desconectado"
                mem_row["fecha_actualizacion"] = now_iso
    else:
        try:
            (
                admin_db.from_("integracion_pago_gimnasio")
                .update(
                    {
                        "access_token_encrypted": None,
                        "refresh_token_encrypted": None,
                        "token_expires_at": None,
                        "estado": "desconectado",
                        "fecha_actualizacion": now_iso,
                    }
                )
                .eq("id_integracion_pago", existing["id_integracion_pago"])
                .eq("id_gimnasio", gym_id)
                .eq("proveedor", PROVEEDOR_MERCADOPAGO)
                .execute()
            )
        except Exception as exc:
            raise_http_from_postgrest_error(exc, "No se pudo desconectar la cuenta de Mercado Pago")

    return {
        "mensaje": "Cuenta de Mercado Pago desconectada correctamente. El historial de pagos se conserva intacto.",
        **_sanitize_public_integration(None, gym_id),
    }


@router.post("/desconectar", status_code=status.HTTP_200_OK)
async def desconectar_mp_post(
    req: Optional[DesconectarMPRequest] = Body(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Desconecta la cuenta de Mercado Pago vinculada al gimnasio autorizado."""
    return await _desconectar_mp_logic(
        current_user=current_user,
        explicit_gym_id=req.id_gimnasio if req else None,
    )


@router.delete("", status_code=status.HTTP_200_OK)
async def desconectar_mp_delete(
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Alias REST para desconectar la cuenta de Mercado Pago del gimnasio autorizado."""
    return await _desconectar_mp_logic(
        current_user=current_user,
        explicit_gym_id=id_gimnasio,
    )
