from datetime import datetime, timezone
from typing import Dict, Any
from fastapi import APIRouter, HTTPException, status, Depends
from app.config import supabase
from app.core.security import (
    get_password_hash,
    verify_password,
    create_access_token,
)
from app.core.dependencies import get_current_user
from app.services.utils import get_data
from app.models.auth import (
    RegisterOwnerRequest,
    LoginRequest,
    LoginSocioRequest,
)

router = APIRouter(prefix="/api/auth", tags=["Autenticación"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register_owner(req: RegisterOwnerRequest):
    """Registro de gimnasio y dueño."""
    clean_email = str(req.email).strip().lower()

    # 1. Comprobar si el email ya existe
    res_existente = (
        supabase.from_("usuario")
        .select("id_usuario")
        .ilike("email", clean_email)
        .maybe_single()
        .execute()
    )
    if get_data(res_existente):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El email ingresado ya está registrado."
        )

    # 2. Encriptar contraseña
    contrasena_hash = get_password_hash(req.contrasena)

    # 3. Insertar Usuario Dueño
    now_iso = datetime.now(timezone.utc).isoformat()
    nuevo_usuario_data = {
        "nombre": req.nombre.strip(),
        "apellido": req.apellido.strip(),
        "email": clean_email,
        "contrasena": contrasena_hash,
        "rol": "dueño",
        "estado": "activo",
        "fecha_creacion": now_iso,
    }
    res_user = (
        supabase.from_("usuario")
        .insert([nuevo_usuario_data])
        .execute()
    )
    if not res_user.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al registrar el usuario dueño."
        )
    nuevo_usuario = res_user.data[0]
    id_usuario = nuevo_usuario["id_usuario"]

    # 4. Insertar Gimnasio (si se especificó nombre)
    nuevo_gimnasio = None
    if req.nombreGimnasio and req.nombreGimnasio.strip():
        gym_data = {
            "nombre": req.nombreGimnasio.strip(),
            "direccion": req.direccion.strip() if req.direccion else None,
            "telefono": req.telefono.strip() if req.telefono else None,
            "email": req.emailGimnasio.strip().lower() if req.emailGimnasio else clean_email,
            "fecha_registro": now_iso,
            "estado": "activo",
            "id_usuario": id_usuario,
        }
        res_gym = supabase.from_("gimnasio").insert([gym_data]).execute()
        if res_gym.data:
            nuevo_gimnasio = res_gym.data[0]

    # 5. Generar token JWT
    id_gimnasio = nuevo_gimnasio["id_gimnasio"] if nuevo_gimnasio else None
    token_payload = {
        "id_usuario": id_usuario,
        "id_gimnasio": id_gimnasio,
        "email": clean_email,
        "rol": "dueño",
    }
    token = create_access_token(token_payload)

    # Quitar contraseña del retorno
    usuario_seguro = {k: v for k, v in nuevo_usuario.items() if k != "contrasena"}
    usuario_seguro["gimnasio"] = nuevo_gimnasio

    mensaje = (
        "Dueño y Gimnasio registrados exitosamente."
        if nuevo_gimnasio
        else "Usuario registrado exitosamente."
    )
    return {
        "mensaje": mensaje,
        "token": token,
        "usuario": usuario_seguro,
        "gimnasio": nuevo_gimnasio,
    }


@router.post("/login", status_code=status.HTTP_200_OK)
async def login(req: LoginRequest):
    """Inicio de sesión general (Dueño / Administrador)."""
    password_input = req.contrasena or req.password
    if not req.email or not password_input:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El correo electrónico y la contraseña son obligatorios."
        )

    clean_email = req.email.strip().lower()

    # 1. Buscar usuario
    res = (
        supabase.from_("usuario")
        .select("id_usuario, nombre, apellido, email, contrasena, rol, estado, fecha_creacion")
        .ilike("email", clean_email)
        .maybe_single()
        .execute()
    )
    usuario = get_data(res)

    if not usuario:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas. Verifica tu email y contraseña."
        )

    if usuario.get("estado") != "activo":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tu cuenta se encuentra inactiva o suspendida. Por favor, contacta al administrador."
        )

    # 2. Validar contraseña
    if not verify_password(password_input, usuario.get("contrasena")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas. Verifica tu email y contraseña."
        )

    # 3. Buscar gimnasio del usuario
    res_gym = (
        supabase.from_("gimnasio")
        .select("id_gimnasio, nombre, direccion, telefono, estado")
        .eq("id_usuario", usuario["id_usuario"])
        .maybe_single()
        .execute()
    )
    gimnasio = get_data(res_gym)

    # 4. Generar token
    token_payload = {
        "id_usuario": usuario["id_usuario"],
        "id_gimnasio": gimnasio["id_gimnasio"] if gimnasio else None,
        "email": usuario["email"],
        "rol": usuario["rol"],
    }
    token = create_access_token(token_payload)

    usuario_seguro = {
        "id_usuario": usuario["id_usuario"],
        "nombre": usuario.get("nombre"),
        "apellido": usuario.get("apellido"),
        "email": usuario.get("email"),
        "rol": usuario.get("rol"),
        "estado": usuario.get("estado"),
        "fecha_creacion": usuario.get("fecha_creacion"),
        "gimnasio": gimnasio or None,
    }

    return {
        "mensaje": "Inicio de sesión exitoso.",
        "token": token,
        "usuario": usuario_seguro,
    }


@router.post("/login-socio", status_code=status.HTTP_200_OK)
async def login_socio(req: LoginSocioRequest):
    """Inicio de sesión exclusivo para Socios."""
    password_input = req.contrasena or req.password
    if not req.email or not req.dni or not password_input:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El correo electrónico, DNI y contraseña son obligatorios."
        )

    clean_email = req.email.strip().lower()
    clean_dni = str(req.dni).strip()
    print(f"INFO:     [AUTH] Intento de login socio: email='{clean_email}', dni='{clean_dni}'")

    # 1. Buscar al socio
    res = (
        supabase.from_("socio")
        .select("id_socio, id_gimnasio, dni, telefono, fecha_alta, estado, nombre, apellido, email, contrasena")
        .ilike("email", clean_email)
        .eq("dni", clean_dni)
        .maybe_single()
        .execute()
    )
    socio = get_data(res)

    if not socio:
        print(f"WARNING:  [AUTH] Socio no encontrado con email='{clean_email}' y dni='{clean_dni}'")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas. Verifique su correo electrónico y DNI."
        )

    if (socio.get("estado") or "").lower() != "activo":
        print(f"WARNING:  [AUTH] Socio '{clean_email}' inactivo (estado='{socio.get('estado')}')")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El socio se encuentra inactivo. Comuníquese con la administración del gimnasio."
        )

    if not socio.get("contrasena"):
        print(f"WARNING:  [AUTH] Socio '{clean_email}' sin contraseña asignada en la BD.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Este socio aún no tiene una contraseña asignada. Solicite su registro en el gimnasio."
        )

    # 2. Validar contraseña
    if not verify_password(password_input, socio.get("contrasena")):
        print(f"WARNING:  [AUTH] Contraseña incorrecta para socio '{clean_email}'")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas. Contraseña incorrecta."
        )

    print(f"INFO:     [AUTH] Login exitoso para socio '{clean_email}' (id_socio={socio['id_socio']})")

    # 3. Obtener datos del gimnasio
    gimnasio = None
    if socio.get("id_gimnasio"):
        res_gym = (
            supabase.from_("gimnasio")
            .select("id_gimnasio, nombre, direccion, telefono, estado")
            .eq("id_gimnasio", socio["id_gimnasio"])
            .maybe_single()
            .execute()
        )
        gimnasio = get_data(res_gym)

    # 4. Generar token
    token_payload = {
        "id_socio": socio["id_socio"],
        "id_gimnasio": socio.get("id_gimnasio"),
        "dni": socio.get("dni"),
        "email": socio.get("email"),
        "rol": "socio",
    }
    token = create_access_token(token_payload)

    socio_seguro = {
        "id_socio": socio["id_socio"],
        "id_gimnasio": socio.get("id_gimnasio"),
        "nombre": socio.get("nombre"),
        "apellido": socio.get("apellido"),
        "dni": socio.get("dni"),
        "email": socio.get("email"),
        "telefono": socio.get("telefono"),
        "estado": socio.get("estado"),
        "rol": "socio",
        "gimnasio": gimnasio,
    }

    return {
        "mensaje": "Inicio de sesión exitoso.",
        "token": token,
        "usuario": socio_seguro,
        "socio": socio_seguro,
    }


@router.get("/me", status_code=status.HTTP_200_OK)
async def get_me(usuario_auth: Dict[str, Any] = Depends(get_current_user)):
    """Obtener datos del usuario/socio actualmente autenticado."""
    id_usuario = usuario_auth.get("id_usuario")
    id_socio = usuario_auth.get("id_socio")

    if not id_usuario and not id_socio:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario no autenticado."
        )

    # Caso Socio
    if id_socio:
        res_s = (
            supabase.from_("socio")
            .select("id_socio, id_gimnasio, dni, telefono, fecha_alta, estado, nombre, apellido, email")
            .eq("id_socio", id_socio)
            .maybe_single()
            .execute()
        )
        socio = get_data(res_s)
        if not socio:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Socio no encontrado.")

        gimnasio = None
        if socio.get("id_gimnasio"):
            res_g = (
                supabase.from_("gimnasio")
                .select("id_gimnasio, nombre, direccion, telefono, estado")
                .eq("id_gimnasio", socio["id_gimnasio"])
                .maybe_single()
                .execute()
            )
            gimnasio = get_data(res_g)

        socio_resp = dict(socio)
        socio_resp["rol"] = "socio"
        socio_resp["gimnasio"] = gimnasio
        return {"usuario": socio_resp}

    # Caso Usuario / Dueño
    res_u = (
        supabase.from_("usuario")
        .select("id_usuario, nombre, apellido, email, rol, estado, fecha_creacion")
        .eq("id_usuario", id_usuario)
        .maybe_single()
        .execute()
    )
    usuario = get_data(res_u)
    if not usuario:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado.")

    res_gym = (
        supabase.from_("gimnasio")
        .select("id_gimnasio, nombre, direccion, telefono, estado")
        .eq("id_usuario", usuario["id_usuario"])
        .maybe_single()
        .execute()
    )
    user_resp = dict(usuario)
    user_resp["gimnasio"] = get_data(res_gym)
    return {"usuario": user_resp}
