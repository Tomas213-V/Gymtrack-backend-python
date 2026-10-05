from datetime import datetime, date, timedelta
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException, status, Depends, Query, Path
from app.config import supabase
from app.core.dependencies import get_current_user
from app.services.utils import get_data
from app.models.pago import PagoCreate, PagoUpdateEstado

router = APIRouter(prefix="/api/pagos", tags=["Pagos"])


@router.get("/resumen", status_code=status.HTTP_200_OK)
async def get_resumen_pagos(
    desde: Optional[str] = Query(None),
    hasta: Optional[str] = Query(None),
    id_gimnasio: Optional[int] = Query(None),
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user)
):
    """Obtener resumen y métricas de pagos (total recaudado, realizados, pendientes, vencidos)."""
    gym_id = id_gimnasio or (current_user.get("id_gimnasio") if current_user else None)

    if gym_id:
        query = (
            supabase.from_("pago")
            .select("monto, estado, membresia!inner(socio!inner(id_gimnasio))")
            .eq("membresia.socio.id_gimnasio", gym_id)
        )
    else:
        query = supabase.from_("pago").select("monto, estado, membresia(socio(id_gimnasio))")

    if desde:
        query = query.gte("fecha_pago", desde)
    if hasta:
        query = query.lte("fecha_pago", hasta)

    res = query.execute()
    target_data = res.data or []

    resumen = {
        "total_recaudado": 0,
        "pagos_realizados": 0,
        "pagos_pendientes": 0,
        "pagos_vencidos": 0,
    }

    for pago in target_data:
        estado_norm = str(pago.get("estado") or "").strip().lower()
        if estado_norm == "pagado":
            resumen["total_recaudado"] += float(pago.get("monto") or 0)
            resumen["pagos_realizados"] += 1
        elif estado_norm == "pendiente":
            resumen["pagos_pendientes"] += 1
        elif estado_norm == "vencido":
            resumen["pagos_vencidos"] += 1

    return resumen


@router.get("/membresias", status_code=status.HTTP_200_OK)
async def get_pagos_membresias(
    id_gimnasio: Optional[int] = Query(None),
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user)
):
    """Listado de socios y membresías del gimnasio para registrar pagos."""
    gym_id = id_gimnasio or (current_user.get("id_gimnasio") if current_user else None)

    query = (
        supabase.from_("socio")
        .select("""
            id_socio,
            dni,
            telefono,
            nombre,
            apellido,
            email,
            estado,
            id_gimnasio,
            membresia (
                id_membresia,
                precio,
                fecha_inicio,
                fecha_vencimiento,
                estado,
                id_plan_membresia,
                plan_membresia (
                    id_plan_membresia,
                    nombre,
                    precio,
                    duracion_dias
                )
            )
        """)
        .order("id_socio", desc=True)
    )

    if gym_id:
        query = query.eq("id_gimnasio", gym_id)

    res = query.execute()
    socios = res.data or []

    resultado = []
    for s in socios:
        m_list = s.get("membresia")
        memb_list = m_list if isinstance(m_list, list) else ([m_list] if m_list else [])
        m_activa = next(
            (m for m in memb_list if (m.get("estado") or "").lower() == "activo"),
            memb_list[0] if memb_list else None
        )
        resultado.append({
            **s,
            "membresia_activa": m_activa,
        })

    return resultado


@router.get("/planes", status_code=status.HTTP_200_OK)
async def get_pagos_planes(
    id_gimnasio: Optional[int] = Query(None),
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user)
):
    """Listar planes de membresía disponibles para pagos (siembra planes iniciales si está vacío)."""
    gym_id = id_gimnasio or (current_user.get("id_gimnasio") if current_user else None)

    query = supabase.from_("plan_membresia").select("*").order("id_plan_membresia", desc=False)
    if gym_id:
        query = query.eq("id_gimnasio", gym_id)

    res = query.execute()
    data = res.data or []

    # Sembrado inicial si está vacío
    if not data and gym_id:
        planes_iniciales = [
            {"id_gimnasio": int(gym_id), "nombre": "Plan Básico", "precio": 15000, "duracion_dias": 30, "estado": "Activo"},
            {"id_gimnasio": int(gym_id), "nombre": "Plan Premium", "precio": 22000, "duracion_dias": 30, "estado": "Activo"},
            {"id_gimnasio": int(gym_id), "nombre": "Pase Libre", "precio": 28000, "duracion_dias": 30, "estado": "Activo"},
        ]
        res_ins = supabase.from_("plan_membresia").insert(planes_iniciales).execute()
        if res_ins.data:
            data = res_ins.data

    return data


@router.get("", status_code=status.HTTP_200_OK)
async def get_pagos(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1),
    estado: Optional[str] = Query(None),
    desde: Optional[str] = Query(None),
    hasta: Optional[str] = Query(None),
    id_gimnasio: Optional[int] = Query(None),
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user)
):
    """Listado paginado de pagos con filtros."""
    gym_id = id_gimnasio or (current_user.get("id_gimnasio") if current_user else None)
    offset = (page - 1) * limit

    if gym_id:
        query = (
            supabase.from_("pago")
            .select("*, membresia!inner(*, plan_membresia(*), socio!inner(*))", count="exact")
            .order("id_pago", desc=True)
            .range(offset, offset + limit - 1)
            .eq("membresia.socio.id_gimnasio", gym_id)
        )
    else:
        query = (
            supabase.from_("pago")
            .select("*, membresia(*, plan_membresia(*), socio(*))", count="exact")
            .order("id_pago", desc=True)
            .range(offset, offset + limit - 1)
        )

    if estado and estado.lower() != "todos":
        query = query.eq("estado", estado.lower())
    if desde:
        query = query.gte("fecha_pago", desde)
    if hasta:
        query = query.lte("fecha_pago", hasta)

    res = query.execute()

    return {
        "total": res.count or 0,
        "pagina": page,
        "limite": limit,
        "datos": res.data or [],
    }


@router.get("/{id}", status_code=status.HTTP_200_OK)
async def get_pago_by_id(id: int = Path(...)):
    """Obtener pago por ID con detalles de socio y membresía."""
    res = (
        supabase.from_("pago")
        .select("*, membresia(*, plan_membresia(*), socio(*))")
        .eq("id_pago", id)
        .maybe_single()
        .execute()
    )

    pago = get_data(res)
    if not pago:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pago no encontrado.")

    return pago


@router.post("", status_code=status.HTTP_201_CREATED)
async def registrar_pago(
    req: PagoCreate,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user)
):
    """Registrar un nuevo pago en el sistema."""
    gym_id = req.id_gimnasio or (current_user.get("id_gimnasio") if current_user else None)
    socio_id = req.id_socio or req.socio_id

    # 1. Crear socio al paso si no existe
    if not socio_id and (req.nombre or req.dni):
        if not gym_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Se requiere id_gimnasio para registrar un nuevo socio."
            )
        clean_dni = str(req.dni).strip() if req.dni else f"DNI-{int(datetime.now().timestamp())}"
        nuevo_socio = {
            "id_gimnasio": gym_id,
            "nombre": (req.nombre or "Socio").strip(),
            "apellido": (req.apellido or "").strip(),
            "dni": clean_dni,
            "telefono": req.telefono.strip() if req.telefono else None,
            "email": req.email.strip().lower() if req.email else None,
            "fecha_alta": date.today().isoformat(),
            "estado": "activo",
        }
        res_ns = supabase.from_("socio").insert([nuevo_socio]).execute()
        if res_ns.data:
            socio_id = res_ns.data[0]["id_socio"]

    if not req.id_membresia and not socio_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes seleccionar o registrar un socio para procesar el pago."
        )

    # 2. Gestionar membresía asociada
    id_membresia = req.id_membresia
    fecha_pago_str = req.fecha_pago or date.today().isoformat()
    estado_pago = (req.estado or "pagado").strip().lower()

    if not id_membresia and socio_id:
        res_mex = (
            supabase.from_("membresia")
            .select("id_membresia, id_plan_membresia")
            .eq("id_socio", socio_id)
            .eq("estado", "activo")
            .maybe_single()
            .execute()
        )
        mex_data = get_data(res_mex)
        if mex_data:
            id_membresia = mex_data["id_membresia"]
        else:
            plan_id = req.id_plan_membresia
            duracion_dias = 30
            if plan_id:
                res_p = (
                    supabase.from_("plan_membresia")
                    .select("duracion_dias")
                    .eq("id_plan_membresia", plan_id)
                    .maybe_single()
                    .execute()
                )
                p_data = get_data(res_p)
                if p_data and p_data.get("duracion_dias"):
                    duracion_dias = p_data["duracion_dias"]
            elif req.plan:
                pq = supabase.from_("plan_membresia").select("id_plan_membresia, duracion_dias").ilike("nombre", f"%{req.plan}%")
                if gym_id:
                    pq = pq.eq("id_gimnasio", gym_id)
                res_pf = pq.limit(1).maybe_single().execute()
                pf_data = get_data(res_pf)
                if pf_data:
                    plan_id = pf_data["id_plan_membresia"]
                    duracion_dias = pf_data.get("duracion_dias") or 30

            if not plan_id and gym_id:
                res_pdef = supabase.from_("plan_membresia").select("id_plan_membresia, duracion_dias").eq("id_gimnasio", gym_id).limit(1).maybe_single().execute()
                pdef_data = get_data(res_pdef)
                if pdef_data:
                    plan_id = pdef_data["id_plan_membresia"]
                    duracion_dias = pdef_data.get("duracion_dias") or 30
                else:
                    res_pn = supabase.from_("plan_membresia").insert([{
                        "id_gimnasio": int(gym_id),
                        "nombre": req.plan or "Plan Premium",
                        "precio": float(req.monto) or 18000.0,
                        "duracion_dias": 30,
                        "estado": "Activo"
                    }]).execute()
                    if res_pn.data:
                        plan_id = res_pn.data[0]["id_plan_membresia"]
                        duracion_dias = 30

            f_base = date.fromisoformat(fecha_pago_str.split("T")[0])
            nueva_memb = {
                "id_socio": socio_id,
                "precio": float(req.monto),
                "fecha_inicio": f_base.isoformat(),
                "fecha_vencimiento": (f_base + timedelta(days=duracion_dias)).isoformat(),
                "estado": "activo" if estado_pago == "pagado" else ("vencido" if estado_pago == "vencido" else "activo"),
            }
            if plan_id:
                nueva_memb["id_plan_membresia"] = plan_id

            res_ins_m = supabase.from_("membresia").insert([nueva_memb]).execute()
            if res_ins_m.data:
                id_membresia = res_ins_m.data[0]["id_membresia"]

    if not id_membresia:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se pudo vincular una membresía para este socio."
        )

    insert_pago = {
        "id_membresia": id_membresia,
        "monto": float(req.monto),
        "fecha_pago": fecha_pago_str,
        "metodo_pago": (req.metodo_pago or "efectivo").lower(),
        "estado": estado_pago,
        "comprobante": req.comprobante or None,
    }

    res_pago = supabase.from_("pago").insert([insert_pago]).execute()
    if not res_pago.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al registrar el pago.")

    id_nuevo_pago = res_pago.data[0]["id_pago"]
    res_full = (
        supabase.from_("pago")
        .select("*, membresia(*, plan_membresia(*), socio(*))")
        .eq("id_pago", id_nuevo_pago)
        .single()
        .execute()
    )

    return {
        "mensaje": "Pago registrado con éxito.",
        "pago": res_full.data or res_pago.data[0],
    }


@router.patch("/{id}/estado", status_code=status.HTTP_200_OK)
async def actualizar_estado_pago(
    id: int = Path(...),
    req: PagoUpdateEstado = ...
):
    """Actualizar estado del pago (pagado, pendiente, vencido) y sincronizar membresía y socio."""
    estados_permitidos = ["pagado", "pendiente", "vencido"]
    nuevo_estado = req.estado.strip().lower()

    if nuevo_estado not in estados_permitidos:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Estado no válido o no proporcionado."
        )

    res_upd = (
        supabase.from_("pago")
        .update({"estado": nuevo_estado})
        .eq("id_pago", id)
        .execute()
    )
    if not res_upd.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pago no encontrado para actualizar.")

    # Sincronizar membresía y socio asociados
    res_full = (
        supabase.from_("pago")
        .select("*, membresia(*, plan_membresia(*), socio(*))")
        .eq("id_pago", id)
        .single()
        .execute()
    )
    pago_actualizado = res_full.data

    if pago_actualizado and pago_actualizado.get("id_membresia"):
        estado_memb = "activo" if nuevo_estado == "pagado" else ("vencido" if nuevo_estado == "vencido" else "pendiente")
        supabase.from_("membresia").update({"estado": estado_memb}).eq("id_membresia", pago_actualizado["id_membresia"]).execute()

        socio_data = pago_actualizado.get("membresia", {}).get("socio")
        if socio_data and socio_data.get("id_socio"):
            estado_socio = "activo" if nuevo_estado == "pagado" else ("inactivo" if nuevo_estado == "vencido" else "activo")
            supabase.from_("socio").update({"estado": estado_socio}).eq("id_socio", socio_data["id_socio"]).execute()

    return {
        "mensaje": "Estado actualizado correctamente.",
        "pago": pago_actualizado,
    }


@router.delete("/{id}", status_code=status.HTTP_200_OK)
async def eliminar_pago(id: int = Path(...)):
    """Eliminar un registro de pago."""
    res = supabase.from_("pago").delete().eq("id_pago", id).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="El pago que deseas eliminar no existe.")

    return {
        "mensaje": "Pago eliminado exitosamente.",
        "pago": res.data[0],
    }
