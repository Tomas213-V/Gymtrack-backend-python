from datetime import datetime, date, timedelta
from typing import Dict, Any, Optional
import math
from fastapi import APIRouter, HTTPException, status, Depends, Query, Path
from app.config import supabase
from app.core.dependencies import get_current_user
from app.core.security import get_password_hash, verify_password
from app.services.utils import get_data
from app.models.socio import (
    SocioCreate,
    SocioUpdate,
    SocioChangeEstado,
    SocioUpdatePerfil,
)

router = APIRouter(prefix="/api/socios", tags=["Socios"])


@router.get("/perfil", status_code=status.HTTP_200_OK)
async def get_perfil_socio(
    id: Optional[int] = Query(None),
    id_socio: Optional[int] = Query(None),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Obtiene el perfil completo del socio autenticado o consultado por ID."""
    target_id = id_socio or id or current_user.get("id_socio")
    user_rol = current_user.get("rol")

    # Si es socio, solo puede ver su propio perfil
    if user_rol == "socio":
        if not target_id or int(current_user.get("id_socio", 0)) != int(target_id):
            target_id = current_user.get("id_socio")

    if not target_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se especificó el ID del socio."
        )

    res = (
        supabase.from_("socio")
        .select("""
            *,
            gimnasio (*),
            membresia (
                *,
                plan_membresia (*)
            ),
            asistencia (*),
            progreso (*)
        """)
        .eq("id_socio", target_id)
        .maybe_single()
        .execute()
    )
    socio = get_data(res)

    if not socio:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Socio no encontrado.")

    if "contrasena" in socio:
        del socio["contrasena"]

    # Procesar membresías
    raw_memb = socio.get("membresia")
    membresias_lista = raw_memb if isinstance(raw_memb, list) else ([raw_memb] if raw_memb else [])

    # Últimos pagos vinculados
    ultimos_pagos = []
    if membresias_lista:
        memb_ids = [m["id_membresia"] for m in membresias_lista if m and "id_membresia" in m]
        if memb_ids:
            res_pagos = (
                supabase.from_("pago")
                .select("id_pago, monto, fecha_pago, metodo_pago, estado, id_membresia")
                .in_("id_membresia", memb_ids)
                .order("fecha_pago", desc=True)
                .limit(5)
                .execute()
            )
            ultimos_pagos = res_pagos.data or []

    # Membresía activa o más reciente
    membresia_activa = next(
        (m for m in membresias_lista if (m.get("estado") or "").lower() == "activo"),
        membresias_lista[0] if membresias_lista else None
    )

    # Asistencias
    raw_asist = socio.get("asistencia")
    asistencias_lista = raw_asist if isinstance(raw_asist, list) else ([raw_asist] if raw_asist else [])
    asistencias_ordenadas = sorted(
        asistencias_lista,
        key=lambda x: str(x.get("hora_ingreso") or ""),
        reverse=True
    )
    total_asistencias = len(asistencias_ordenadas)
    ultimas_asistencias = asistencias_ordenadas[:5]

    # Días restantes
    dias_restantes = None
    if membresia_activa and membresia_activa.get("fecha_vencimiento"):
        try:
            fv = date.fromisoformat(str(membresia_activa["fecha_vencimiento"]).split("T")[0])
            dias_restantes = (fv - date.today()).days
        except Exception:
            pass

    # Socios disponibles del gimnasio para admins
    socios_del_gimnasio = []
    if user_rol != "socio" and current_user.get("id_gimnasio"):
        res_sg = (
            supabase.from_("socio")
            .select("id_socio, nombre, apellido, dni, estado")
            .eq("id_gimnasio", current_user["id_gimnasio"])
            .order("id_socio", desc=False)
            .execute()
        )
        socios_del_gimnasio = res_sg.data or []

    return {
        "tipo": "socio",
        "socio": {
            "id_socio": socio["id_socio"],
            "id_gimnasio": socio.get("id_gimnasio"),
            "dni": socio.get("dni"),
            "telefono": socio.get("telefono"),
            "fecha_alta": socio.get("fecha_alta"),
            "estado": socio.get("estado"),
            "nombre": socio.get("nombre"),
            "apellido": socio.get("apellido"),
            "email": socio.get("email"),
        },
        "gimnasio": socio.get("gimnasio"),
        "membresia": membresia_activa,
        "historialMembresias": membresias_lista,
        "ultimosPagos": ultimos_pagos,
        "ultimasAsistencias": ultimas_asistencias,
        "estadisticas": {
            "totalAsistencias": total_asistencias,
            "totalPagos": len(ultimos_pagos),
            "estadoMembresia": membresia_activa.get("estado") if membresia_activa else "sin_membresia",
            "diasRestantes": dias_restantes,
        },
        "sociosDisponibles": socios_del_gimnasio,
    }


@router.put("/perfil", status_code=status.HTTP_200_OK)
async def update_perfil_socio(
    req: SocioUpdatePerfil,
    id: Optional[int] = Query(None),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Actualiza el perfil del socio (datos personales, contraseña)."""
    target_id = req.id_socio or id or current_user.get("id_socio")
    user_rol = current_user.get("rol")

    if user_rol == "socio":
        if not target_id or int(current_user.get("id_socio", 0)) != int(target_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes autorización para modificar otro perfil."
            )

    if not target_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se especificó el ID del socio a actualizar."
        )

    res_fetch = (
        supabase.from_("socio")
        .select("*")
        .eq("id_socio", target_id)
        .maybe_single()
        .execute()
    )
    socio_actual = get_data(res_fetch)
    if not socio_actual:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Socio no encontrado.")

    if current_user.get("id_gimnasio") and user_rol != "socio":
        if socio_actual.get("id_gimnasio") != current_user["id_gimnasio"]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="El socio no pertenece a tu gimnasio.")

    # Cambio de contraseña
    new_pass = req.nueva_contrasena or req.contrasena or req.password
    nuevo_hash = None
    if new_pass and str(new_pass).strip():
        pass_limpia = str(new_pass).strip()
        if len(pass_limpia) < 6:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La nueva contraseña debe tener al menos 6 caracteres."
            )

        if user_rol == "socio":
            if not req.contrasena_actual:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Debes proporcionar tu contraseña actual para autorizar el cambio."
                )
            if not verify_password(req.contrasena_actual, socio_actual.get("contrasena")):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="La contraseña actual ingresada es incorrecta."
                )
        nuevo_hash = get_password_hash(pass_limpia)

    # Validar DNI único dentro del gimnasio si cambia
    clean_dni = str(req.dni).strip() if req.dni is not None else None
    if clean_dni and clean_dni != str(socio_actual.get("dni", "")):
        res_dni = (
            supabase.from_("socio")
            .select("id_socio")
            .eq("dni", clean_dni)
            .eq("id_gimnasio", socio_actual["id_gimnasio"])
            .neq("id_socio", target_id)
            .maybe_single()
            .execute()
        )
        if get_data(res_dni):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El DNI ingresado ya está asignado a otro socio en este gimnasio."
            )

    update_payload = {}
    if req.nombre:
        update_payload["nombre"] = req.nombre.strip()
    if req.apellido is not None:
        update_payload["apellido"] = req.apellido.strip()
    if req.telefono is not None:
        update_payload["telefono"] = req.telefono.strip() if req.telefono else None
    if req.email is not None:
        update_payload["email"] = req.email.strip().lower() if req.email else None
    if clean_dni:
        update_payload["dni"] = clean_dni
    if nuevo_hash is not None:
        update_payload["contrasena"] = nuevo_hash

    res_upd = (
        supabase.from_("socio")
        .update(update_payload)
        .eq("id_socio", target_id)
        .execute()
    )
    if not res_upd.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al actualizar el perfil.")

    socio_actualizado = res_upd.data[0]
    if "contrasena" in socio_actualizado:
        del socio_actualizado["contrasena"]

    return {
        "mensaje": "Perfil actualizado exitosamente.",
        "socio": socio_actualizado,
    }


@router.get("", status_code=status.HTTP_200_OK)
async def get_socios(
    page: int = Query(1, ge=1),
    limit: str = Query("6"),
    estado: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Listado paginado de socios del gimnasio con soporte para búsqueda y filtros."""
    id_gimnasio = current_user.get("id_gimnasio")
    if not id_gimnasio:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se identificó el gimnasio autenticado."
        )

    is_all = limit.lower() in ("all", "0")
    parsed_limit = int(limit) if not is_all and limit.isdigit() else 6
    parsed_page = page if not is_all else 1

    query = (
        supabase.from_("socio")
        .select("""
            *,
            membresia (
                *,
                plan_membresia (*)
            )
        """, count="exact")
        .eq("id_gimnasio", id_gimnasio)
    )

    if estado and estado.lower() != "todos":
        query = query.eq("estado", estado)

    if search and search.strip():
        s = search.strip()
        query = query.or_(f"nombre.ilike.%{s}%,apellido.ilike.%{s}%,dni.ilike.%{s}%,telefono.ilike.%{s}%")

    query = query.order("id_socio", desc=True)

    if not is_all:
        offset = (parsed_page - 1) * parsed_limit
        query = query.range(offset, offset + parsed_limit - 1)

    res = query.execute()
    socios = res.data or []
    count = res.count or 0

    # Quitar contraseñas si vinieran
    for s in socios:
        if "contrasena" in s:
            del s["contrasena"]

    return {
        "total": count,
        "page": 1 if is_all else parsed_page,
        "totalPages": 1 if is_all else math.ceil(count / parsed_limit) if parsed_limit else 1,
        "limit": count if is_all else parsed_limit,
        "socios": socios,
    }


@router.get("/{id}", status_code=status.HTTP_200_OK)
async def get_socio_by_id(
    id: int = Path(...),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Obtener socio por ID con sus membresías, asistencias y progresos."""
    id_gimnasio = current_user.get("id_gimnasio")
    query = (
        supabase.from_("socio")
        .select("""
            *,
            gimnasio (*),
            membresia (
                *,
                plan_membresia (*)
            ),
            asistencia (*),
            progreso (*)
        """)
        .eq("id_socio", id)
    )

    if id_gimnasio and current_user.get("rol") != "socio":
        query = query.eq("id_gimnasio", id_gimnasio)

    res = query.maybe_single().execute()
    socio = get_data(res)

    if not socio:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Socio no encontrado o no pertenece a tu gimnasio."
        )

    if "contrasena" in socio:
        del socio["contrasena"]

    return {"socio": socio}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_socio(
    req: SocioCreate,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Registrar un nuevo socio en el gimnasio y asignarle membresía inicial."""
    id_gimnasio = current_user.get("id_gimnasio")
    if not id_gimnasio:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se asoció un gimnasio al usuario."
        )

    if not req.nombre or not req.nombre.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre es obligatorio.")

    clean_dni = str(req.dni).strip()
    if not clean_dni:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El DNI es obligatorio.")

    # Verificar DNI existente en el gimnasio
    res_exist = (
        supabase.from_("socio")
        .select("id_socio")
        .eq("dni", clean_dni)
        .eq("id_gimnasio", id_gimnasio)
        .maybe_single()
        .execute()
    )
    if get_data(res_exist):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ya existe un socio registrado con este DNI."
        )

    # Hash de contraseña si se proporcionó
    raw_pass = req.contrasena or req.password
    contrasena_hash = get_password_hash(str(raw_pass).strip()) if raw_pass else None

    nuevo_socio_data = {
        "id_gimnasio": id_gimnasio,
        "nombre": req.nombre.strip(),
        "apellido": req.apellido.strip() if req.apellido else "",
        "dni": clean_dni,
        "telefono": req.telefono.strip() if req.telefono else None,
        "email": req.email.strip().lower() if req.email else None,
        "contrasena": contrasena_hash,
        "fecha_alta": date.today().isoformat(),
        "estado": req.estado or "activo",
    }

    res_ins = supabase.from_("socio").insert([nuevo_socio_data]).execute()
    if not res_ins.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al crear el socio.")

    nuevo_socio = res_ins.data[0]
    id_nuevo_socio = nuevo_socio["id_socio"]

    # Crear membresía si se especificó plan o fecha
    membresia = None
    if req.plan or req.id_plan_membresia or req.fecha_vencimiento:
        plan_id = req.id_plan_membresia
        plan_duracion = 30
        plan_precio = req.precio

        if not plan_id and req.plan:
            res_p = (
                supabase.from_("plan_membresia")
                .select("id_plan_membresia, duracion_dias, precio")
                .ilike("nombre", f"%{req.plan}%")
                .eq("id_gimnasio", id_gimnasio)
                .limit(1)
                .maybe_single()
                .execute()
            )
            p_data = get_data(res_p)
            if p_data:
                plan_id = p_data["id_plan_membresia"]
                plan_duracion = p_data.get("duracion_dias") or 30
                if plan_precio is None:
                    plan_precio = p_data.get("precio")

        if not plan_id:
            res_def = (
                supabase.from_("plan_membresia")
                .select("id_plan_membresia, duracion_dias, precio")
                .eq("id_gimnasio", id_gimnasio)
                .limit(1)
                .maybe_single()
                .execute()
            )
            def_data = get_data(res_def)
            if def_data:
                plan_id = def_data["id_plan_membresia"]
                plan_duracion = def_data.get("duracion_dias") or 30
                if plan_precio is None:
                    plan_precio = def_data.get("precio")

        f_inicio = date.today().isoformat()
        f_venc = req.fecha_vencimiento or (date.today() + timedelta(days=plan_duracion)).isoformat()

        memb_data = {
            "id_socio": id_nuevo_socio,
            "precio": plan_precio or 0,
            "fecha_inicio": f_inicio,
            "fecha_vencimiento": f_venc,
            "estado": req.estado or "activo",
        }
        if plan_id:
            memb_data["id_plan_membresia"] = plan_id

        res_m = (
            supabase.from_("membresia")
            .insert([memb_data])
            .execute()
        )
        if res_m.data:
            membresia = res_m.data[0]

    if "contrasena" in nuevo_socio:
        del nuevo_socio["contrasena"]

    socio_retorno = dict(nuevo_socio)
    socio_retorno["membresia"] = membresia

    return {
        "mensaje": "Socio registrado exitosamente.",
        "socio": socio_retorno,
    }


@router.put("/{id}", status_code=status.HTTP_200_OK)
async def update_socio(
    id: int = Path(...),
    req: SocioUpdate = ...,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Actualizar datos generales y plan de un socio."""
    id_gimnasio = current_user.get("id_gimnasio")
    query = supabase.from_("socio").select("id_socio, id_gimnasio").eq("id_socio", id)
    if id_gimnasio:
        query = query.eq("id_gimnasio", id_gimnasio)

    res_check = query.maybe_single().execute()
    if not get_data(res_check):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Socio no encontrado o sin permisos.")

    raw_pass = req.contrasena or req.password
    contrasena_hash = get_password_hash(str(raw_pass).strip()) if raw_pass else None

    update_payload = {}
    if req.nombre:
        update_payload["nombre"] = req.nombre.strip()
    if req.apellido is not None:
        update_payload["apellido"] = req.apellido.strip()
    if req.dni is not None:
        update_payload["dni"] = str(req.dni).strip()
    if req.telefono is not None:
        update_payload["telefono"] = req.telefono.strip() if req.telefono else None
    if req.email is not None:
        update_payload["email"] = req.email.strip().lower() if req.email else None
    if req.estado:
        update_payload["estado"] = req.estado.lower()
    if contrasena_hash is not None:
        update_payload["contrasena"] = contrasena_hash

    res_upd = (
        supabase.from_("socio")
        .update(update_payload)
        .eq("id_socio", id)
        .execute()
    )
    if not res_upd.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al actualizar socio.")

    socio_actualizado = res_upd.data[0]
    if "contrasena" in socio_actualizado:
        del socio_actualizado["contrasena"]

    # Actualizar membresía si se especificó plan
    if req.id_plan_membresia or req.plan:
        plan_id = req.id_plan_membresia
        duracion_dias = 30
        plan_precio = None

        if plan_id:
            res_p = (
                supabase.from_("plan_membresia")
                .select("id_plan_membresia, duracion_dias, precio")
                .eq("id_plan_membresia", plan_id)
                .maybe_single()
                .execute()
            )
            p_data = get_data(res_p)
            if p_data:
                duracion_dias = p_data.get("duracion_dias") or 30
                plan_precio = p_data.get("precio")
        elif req.plan:
            pq = supabase.from_("plan_membresia").select("id_plan_membresia, duracion_dias, precio").ilike("nombre", f"%{req.plan}%")
            if id_gimnasio:
                pq = pq.eq("id_gimnasio", id_gimnasio)
            res_pf = pq.limit(1).maybe_single().execute()
            pf_data = get_data(res_pf)
            if pf_data:
                plan_id = pf_data["id_plan_membresia"]
                duracion_dias = pf_data.get("duracion_dias") or 30
                plan_precio = pf_data.get("precio")

        res_mex = (
            supabase.from_("membresia")
            .select("id_membresia")
            .eq("id_socio", id)
            .eq("estado", "activo")
            .maybe_single()
            .execute()
        )
        mex_data = get_data(res_mex)
        if mex_data:
            m_upd = {}
            if plan_id:
                m_upd["id_plan_membresia"] = plan_id
            if plan_precio is not None:
                m_upd["precio"] = plan_precio
            if m_upd:
                supabase.from_("membresia").update(m_upd).eq("id_membresia", mex_data["id_membresia"]).execute()
        elif plan_id:
            supabase.from_("membresia").insert([{
                "id_socio": id,
                "id_plan_membresia": plan_id,
                "precio": plan_precio or 0,
                "fecha_inicio": date.today().isoformat(),
                "fecha_vencimiento": (date.today() + timedelta(days=duracion_dias)).isoformat(),
                "estado": "activo",
            }]).execute()

    return {
        "mensaje": "Socio actualizado correctamente.",
        "socio": socio_actualizado,
    }


@router.patch("/{id}/estado", status_code=status.HTTP_200_OK)
async def change_estado_socio(
    id: int = Path(...),
    req: SocioChangeEstado = ...,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Cambiar estado del socio (activo, pendiente, inactivo)."""
    id_gimnasio = current_user.get("id_gimnasio")
    estados_permitidos = ["activo", "pendiente", "inactivo"]
    estado_limpio = req.estado.strip().lower()

    if estado_limpio not in estados_permitidos:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El estado enviado no es válido. Debe ser: activo, pendiente o inactivo."
        )

    query = supabase.from_("socio").update({"estado": estado_limpio}).eq("id_socio", id)
    if id_gimnasio:
        query = query.eq("id_gimnasio", id_gimnasio)

    res = query.execute()
    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Socio no encontrado o no pertenece a tu gimnasio."
        )

    return {
        "mensaje": f"Estado del socio modificado a '{estado_limpio}' exitosamente.",
        "socio": res.data[0],
    }


@router.delete("/{id}", status_code=status.HTTP_200_OK)
async def delete_socio(
    id: int = Path(...),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Eliminar socio y sus dependencias (pagos, membresías, asistencias, progresos)."""
    id_gimnasio = current_user.get("id_gimnasio")
    query = supabase.from_("socio").select("id_socio, id_gimnasio").eq("id_socio", id)
    if id_gimnasio:
        query = query.eq("id_gimnasio", id_gimnasio)

    res = query.maybe_single().execute()
    if not get_data(res):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Socio no encontrado o sin permisos.")

    # 1. Eliminar pagos y membresías
    res_m = supabase.from_("membresia").select("id_membresia").eq("id_socio", id).execute()
    if res_m.data:
        m_ids = [m["id_membresia"] for m in res_m.data]
        supabase.from_("pago").delete().in_("id_membresia", m_ids).execute()
        supabase.from_("membresia").delete().eq("id_socio", id).execute()

    # 2. Eliminar asistencias y progresos
    supabase.from_("asistencia").delete().eq("id_socio", id).execute()
    supabase.from_("progreso").delete().eq("id_socio", id).execute()

    # 3. Eliminar socio
    supabase.from_("socio").delete().eq("id_socio", id).execute()

    return {"mensaje": "Socio eliminado exitosamente."}
