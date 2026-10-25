import urllib.parse
import time
import random
from typing import Dict, Any, Optional
import httpx
from app.config import (
    MERCADOPAGO_ACCESS_TOKEN,
    UALA_CLIENT_ID,
    UALA_CLIENT_SECRET,
    FRONTEND_URL,
)


async def crear_preferencia_mercadopago(
    titulo: str,
    monto: float,
    external_reference: str,
    socio: Dict[str, Any],
    gimnasio: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    precio_unitario = float(monto)
    gym_nombre = gimnasio.get("nombre", "GymTrack") if gimnasio else "GymTrack"
    socio_nombre = socio.get("nombre", "")
    socio_apellido = socio.get("apellido", "")
    socio_email = socio.get("email") or "socio@gymtrack.com"
    socio_dni = str(socio.get("dni") or "00000000")

    # Si hay un token real de Mercado Pago configurado
    if MERCADOPAGO_ACCESS_TOKEN and MERCADOPAGO_ACCESS_TOKEN.startswith("APP_USR-"):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                body = {
                    "items": [
                        {
                            "id": external_reference,
                            "title": f"{titulo} - {gym_nombre}",
                            "description": f"Membresía en {gym_nombre} para {socio_nombre} {socio_apellido}",
                            "quantity": 1,
                            "currency_id": "ARS",
                            "unit_price": precio_unitario,
                        }
                    ],
                    "payer": {
                        "name": socio_nombre,
                        "surname": socio_apellido,
                        "email": socio_email,
                        "identification": {
                            "type": "DNI",
                            "number": socio_dni,
                        },
                    },
                    "external_reference": external_reference,
                    "back_urls": {
                        "success": f"{FRONTEND_URL}/membresia-socio?pago_status=approved&ref={external_reference}",
                        "pending": f"{FRONTEND_URL}/membresia-socio?pago_status=pending&ref={external_reference}",
                        "failure": f"{FRONTEND_URL}/membresia-socio?pago_status=rejected&ref={external_reference}",
                    },
                }
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {MERCADOPAGO_ACCESS_TOKEN}",
                }
                res = await client.post(
                    "https://api.mercadopago.com/checkout/preferences",
                    json=body,
                    headers=headers,
                )
                if res.status_code in (200, 201):
                    mp_data = res.json()
                    init_point = mp_data.get("init_point")
                    sandbox_init_point = mp_data.get("sandbox_init_point") or init_point
                    encoded_ip = urllib.parse.quote(init_point or "")
                    return {
                        "proveedor": "mercadopago",
                        "modo": "api_real",
                        "id_preferencia": mp_data.get("id"),
                        "init_point": init_point,
                        "sandbox_init_point": sandbox_init_point,
                        "external_reference": external_reference,
                        "monto": precio_unitario,
                        "qr_codigo": f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={encoded_ip}",
                    }
        except Exception as err:
            print(f"[MP WARNING] Fallo al conectar con la API en vivo de Mercado Pago, usando fallback simulado: {err}")

    # Modo Sandbox/Simulado
    mock_pref_id = f"MP-PREF-{int(time.time() * 1000)}-{random.randint(100, 999)}"
    mock_checkout_url = (
        f"{FRONTEND_URL}/membresia-socio?checkout=mercadopago"
        f"&ref={external_reference}&pref={mock_pref_id}&monto={precio_unitario}"
    )
    encoded_url = urllib.parse.quote(mock_checkout_url)

    return {
        "proveedor": "mercadopago",
        "modo": "sandbox_simulado",
        "id_preferencia": mock_pref_id,
        "init_point": mock_checkout_url,
        "sandbox_init_point": mock_checkout_url,
        "external_reference": external_reference,
        "monto": precio_unitario,
        "qr_codigo": f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={encoded_url}",
        "mensaje": "Sesión de Mercado Pago preparada correctamente.",
    }


async def crear_orden_uala(
    titulo: str,
    monto: float,
    external_reference: str,
    socio: Dict[str, Any],
    gimnasio: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    precio_unitario = float(monto)
    gym_nombre = gimnasio.get("nombre", "GymTrack") if gimnasio else "GymTrack"
    socio_email = socio.get("email") or f"{socio.get('nombre', 'socio')}.{socio.get('apellido', 'gym')}@gymtrack.com"

    if UALA_CLIENT_ID and UALA_CLIENT_SECRET:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                auth_res = await client.post(
                    "https://auth.prod.bis.com.ar/connect/token",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    data={
                        "grant_type": "client_credentials",
                        "client_id": UALA_CLIENT_ID,
                        "client_secret": UALA_CLIENT_SECRET,
                    },
                )
                if auth_res.status_code == 200:
                    auth_data = auth_res.json()
                    access_token = auth_data.get("access_token")
                    order_res = await client.post(
                        "https://api.prod.bis.com.ar/v2/checkout/order",
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {access_token}",
                        },
                        json={
                            "amount": precio_unitario,
                            "description": f"{titulo} - {gym_nombre}",
                            "userName": socio_email,
                            "callback_fail": f"{FRONTEND_URL}/socio/membresia?pago_status=rejected&ref={external_reference}",
                            "callback_success": f"{FRONTEND_URL}/socio/membresia?pago_status=approved&ref={external_reference}",
                            "notification_url": f"{FRONTEND_URL}/api/socio/pagos/webhook?proveedor=uala",
                            "external_reference": external_reference,
                        },
                    )
                    if order_res.status_code in (200, 201):
                        uala_data = order_res.json()
                        checkout_link = (
                            uala_data.get("links", {}).get("checkoutLink")
                            or uala_data.get("url")
                        )
                        encoded_link = urllib.parse.quote(checkout_link or "")
                        return {
                            "proveedor": "uala",
                            "modo": "api_real",
                            "id_orden": uala_data.get("order_id") or uala_data.get("id"),
                            "init_point": checkout_link,
                            "external_reference": external_reference,
                            "monto": precio_unitario,
                            "qr_codigo": f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={encoded_link}",
                        }
        except Exception as err:
            print(f"[UALA WARNING] Fallo al conectar con la API en vivo de Ualá, usando fallback simulado: {err}")

    # Modo Sandbox/Simulado
    mock_order_id = f"UALA-ORD-{int(time.time() * 1000)}-{random.randint(100, 999)}"
    mock_checkout_url = (
        f"{FRONTEND_URL}/socio/membresia?checkout=uala"
        f"&ref={external_reference}&order={mock_order_id}&monto={precio_unitario}"
    )
    encoded_url = urllib.parse.quote(mock_checkout_url)

    return {
        "proveedor": "uala",
        "modo": "sandbox_simulado",
        "id_orden": mock_order_id,
        "init_point": mock_checkout_url,
        "external_reference": external_reference,
        "monto": precio_unitario,
        "qr_codigo": f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={encoded_url}",
        "mensaje": "Orden de cobro Ualá preparada correctamente.",
    }
