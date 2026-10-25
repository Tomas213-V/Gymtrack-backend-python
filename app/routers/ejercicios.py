from typing import Optional, Dict, Any, List, Union
from fastapi import APIRouter, HTTPException, status, Query, Path, Depends
from app.config import supabase
from app.core.dependencies import get_optional_user
from app.services.utils import get_data
from app.models.ejercicio import (
    EjercicioCreate,
    EjercicioPersonalizadoCreate,
    EjercicioUpdate,
)

router = APIRouter(prefix="/api/ejercicios", tags=["Ejercicios"])


def enriquecer_ejercicio(ej: Dict[str, Any], target_socio_id: Optional[int] = None) -> Dict[str, Any]:
    """Enriquece un ejercicio con metadatos de origen y pertenencia."""
    id_socio = ej.get("id_socio")
    es_propio = bool(id_socio)
    pertenece_al_socio = bool(id_socio and target_socio_id and int(id_socio) == int(target_socio_id))

    return {
        **ej,
        "origen": "Mi ejercicio" if es_propio else "Gimnasio",
        "tipo_origen": "propio" if es_propio else "gimnasio",
        "es_propio": es_propio,
        "es_gimnasio": not es_propio,
        "pertenece_al_socio": pertenece_al_socio if target_socio_id else es_propio,
    }


async def resolver_socio_y_gym(current_user: Optional[Dict[str, Any]], id_socio_param: Optional[int] = None):
    """Resuelve el ID de socio y el ID de gimnasio a partir del usuario y/o parámetros."""
    target_socio_id = id_socio_param or (current_user.get("id_socio") if current_user else None)

    if not target_socio_id and current_user and current_user.get("id_usuario"):
        res = (
            supabase.from_("socio")
            .select("id_socio, id_gimnasio")
            .eq("id_usuario", current_user["id_usuario"])
            .maybe_single()
            .execute()
        )
        s_auth = get_data(res)
        if s_auth:
            target_socio_id = s_auth.get("id_socio")

    id_gimnasio = current_user.get("id_gimnasio") if current_user else None

    if target_socio_id and not id_gimnasio:
        res = (
            supabase.from_("socio")
            .select("id_gimnasio")
            .eq("id_socio", target_socio_id)
            .maybe_single()
            .execute()
        )
        s_data = get_data(res)
        if s_data:
            id_gimnasio = s_data.get("id_gimnasio")

    return {
        "target_socio_id": int(target_socio_id) if target_socio_id else None,
        "id_gimnasio": int(id_gimnasio) if id_gimnasio else None,
    }


async def _get_ejercicios_disponibles_socio(
    target_socio_id: Optional[int],
    id_gimnasio: Optional[int],
    origen: str = "todos",
    grupo_muscular: Optional[str] = None,
    search: Optional[str] = None,
    limit: Optional[str] = "100",
    page: int = 1,
) -> List[Dict[str, Any]]:
    """Consulta ejercicios disponibles combinando los oficiales del gimnasio y los propios del socio."""
    query = supabase.from_("ejercicio").select("*")

    if origen == "gimnasio":
        if id_gimnasio:
            query = query.eq("id_gimnasio", id_gimnasio).is_("id_socio", "null")
        else:
            query = query.is_("id_socio", "null")
    elif origen == "propio":
        if target_socio_id:
            query = query.eq("id_socio", target_socio_id)
        else:
            return []
    else:
        # 'todos': gimnasio + propios del socio
        if target_socio_id and id_gimnasio:
            query = query.or_(f"and(id_gimnasio.eq.{id_gimnasio},id_socio.is.null),id_socio.eq.{target_socio_id}")
        elif target_socio_id:
            query = query.or_(f"id_socio.is.null,id_socio.eq.{target_socio_id}")
        elif id_gimnasio:
            query = query.or_(f"id_gimnasio.eq.{id_gimnasio},id_socio.is.null")

    if grupo_muscular and grupo_muscular.lower() != "todos":
        query = query.ilike("grupo_muscular", f"%{grupo_muscular.strip()}%")

    if search and search.strip():
        query = query.ilike("nombre", f"%{search.strip()}%")

    query = query.order("nombre", desc=False)

    if limit and limit.lower() != "all":
        try:
            num_limit = int(limit)
            offset = (page - 1) * num_limit
            query = query.range(offset, offset + num_limit - 1)
        except ValueError:
            pass

    res = query.execute()
    ejercicios = res.data or []

    return [enriquecer_ejercicio(ej, target_socio_id) for ej in ejercicios]


@router.get("", status_code=status.HTTP_200_OK)
async def get_ejercicios(
    page: int = Query(1, ge=1),
    limit: Optional[str] = Query("50"),
    grupo_muscular: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    id_socio: Optional[int] = Query(None),
    origen: Optional[str] = Query(None),
    current_user: Optional[Dict[str, Any]] = Depends(get_optional_user),
):
    """
    Consultar catálogo general de ejercicios.
    Si se suministra id_socio, origen o el usuario autenticado es un socio,
    devuelve la lista de ejercicios disponibles enriquecidos (oficiales + personalizados).
    """
    user_rol = current_user.get("rol") if current_user else None

    # Redirigir a ejercicios disponibles si es socio o se pasa filtro socio/origen
    if id_socio or user_rol == "socio" or origen:
        ident = await resolver_socio_y_gym(current_user, id_socio)
        return await _get_ejercicios_disponibles_socio(
            target_socio_id=ident["target_socio_id"],
            id_gimnasio=ident["id_gimnasio"],
            origen=origen or "todos",
            grupo_muscular=grupo_muscular,
            search=search,
            limit=limit,
            page=page,
        )

    # Catálogo general para administradores / dueños
    query = supabase.from_("ejercicio").select("*", count="exact")

    gym_id = current_user.get("id_gimnasio") if current_user else None
    if gym_id:
        query = query.or_(f"id_gimnasio.eq.{gym_id},id_gimnasio.is.null")

    if grupo_muscular and grupo_muscular.lower() != "todos":
        query = query.ilike("grupo_muscular", f"%{grupo_muscular.strip()}%")

    if search and search.strip():
        query = query.ilike("nombre", f"%{search.strip()}%")

    query = query.order("id_ejercicio", desc=True)

    parsed_limit = 50
    if limit and limit.lower() != "all":
        try:
            parsed_limit = int(limit)
            offset = (page - 1) * parsed_limit
            query = query.range(offset, offset + parsed_limit - 1)
        except ValueError:
            parsed_limit = 50

    res = query.execute()
    data = res.data or []
    datos_enriquecidos = [enriquecer_ejercicio(ej) for ej in data]

    return {
        "total": res.count or 0,
        "pagina": page,
        "limite": parsed_limit if limit != "all" else (res.count or 0),
        "datos": datos_enriquecidos,
    }


@router.get("/disponibles", status_code=status.HTTP_200_OK)
@router.get("/disponibles-socio", status_code=status.HTTP_200_OK)
async def get_ejercicios_disponibles_socio_endpoint(
    id_socio: Optional[int] = Query(None),
    origen: str = Query("todos"),
    grupo_muscular: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: Optional[str] = Query("100"),
    page: int = Query(1, ge=1),
    current_user: Optional[Dict[str, Any]] = Depends(get_optional_user),
):
    """Endpoint directo para consultar ejercicios disponibles para un socio específico."""
    ident = await resolver_socio_y_gym(current_user, id_socio)
    return await _get_ejercicios_disponibles_socio(
        target_socio_id=ident["target_socio_id"],
        id_gimnasio=ident["id_gimnasio"],
        origen=origen,
        grupo_muscular=grupo_muscular,
        search=search,
        limit=limit,
        page=page,
    )


@router.get("/{id}", status_code=status.HTTP_200_OK)
async def get_ejercicio_by_id(id: int = Path(...)):
    """Obtener un ejercicio por ID enriquecido con sus metadatos de origen."""
    res = supabase.from_("ejercicio").select("*").eq("id_ejercicio", id).maybe_single().execute()
    data = get_data(res)
    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ejercicio no encontrado.")

    return enriquecer_ejercicio(data)


@router.post("/personalizado", status_code=status.HTTP_201_CREATED)
async def crear_ejercicio_personalizado(
    req: EjercicioPersonalizadoCreate,
    current_user: Optional[Dict[str, Any]] = Depends(get_optional_user),
):
    """Registrar un ejercicio personalizado creado por o para un socio."""
    if not req.nombre or not req.nombre.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El nombre del ejercicio es obligatorio.",
        )

    ident = await resolver_socio_y_gym(current_user, req.id_socio)
    socio_id = ident["target_socio_id"]
    gym_id = req.id_gimnasio or ident["id_gimnasio"]

    if not socio_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se pudo identificar el socio creador. Proporciona id_socio o inicia sesión como socio.",
        )

    nuevo = {
        "nombre": req.nombre.strip(),
        "grupo_muscular": req.grupo_muscular.strip() if req.grupo_muscular else "General",
        "descripcion": req.descripcion.strip() if req.descripcion else None,
        "dificultad": req.dificultad or "Intermedia",
        "id_socio": socio_id,
        "id_gimnasio": gym_id,
    }

    res = supabase.from_("ejercicio").insert([nuevo]).execute()
    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al crear el ejercicio personalizado.",
        )

    creado = res.data[0]
    return {
        "mensaje": "Ejercicio personalizado creado con éxito.",
        "ejercicio": enriquecer_ejercicio(creado, socio_id),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def crear_ejercicio(
    req: EjercicioCreate,
    current_user: Optional[Dict[str, Any]] = Depends(get_optional_user),
):
    """Registrar un nuevo ejercicio estándar o personalizado si lo solicita un socio."""
    if not req.nombre or not req.nombre.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre del ejercicio es obligatorio.")

    # Si viene id_socio o el rol es socio, redirigir a ejercicio personalizado
    user_rol = current_user.get("rol") if current_user else None
    if req.id_socio or user_rol == "socio":
        perso_req = EjercicioPersonalizadoCreate(
            nombre=req.nombre,
            grupo_muscular=req.grupo_muscular,
            descripcion=req.descripcion,
            dificultad=req.dificultad,
            id_socio=req.id_socio,
            id_gimnasio=req.id_gimnasio,
        )
        return await crear_ejercicio_personalizado(perso_req, current_user)

    gym_id = req.id_gimnasio or (current_user.get("id_gimnasio") if current_user else None)

    nuevo_ej = {
        "nombre": req.nombre.strip(),
        "grupo_muscular": req.grupo_muscular.strip() if req.grupo_muscular else "General",
        "descripcion": req.descripcion.strip() if req.descripcion else None,
        "dificultad": req.dificultad.strip() if req.dificultad else "Intermedia",
        "id_gimnasio": gym_id,
        "id_socio": None,
    }

    res = supabase.from_("ejercicio").insert([nuevo_ej]).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al crear ejercicio.")

    creado = res.data[0]
    return {
        "mensaje": "Ejercicio creado con éxito.",
        "ejercicio": enriquecer_ejercicio(creado),
    }


@router.put("/{id}", status_code=status.HTTP_200_OK)
async def actualizar_ejercicio(
    id: int = Path(...),
    req: EjercicioUpdate = ...,
):
    """Actualizar datos de un ejercicio."""
    update_data = {}
    if req.nombre is not None:
        update_data["nombre"] = req.nombre.strip()
    if req.grupo_muscular is not None:
        update_data["grupo_muscular"] = req.grupo_muscular.strip() if req.grupo_muscular else None
    if req.descripcion is not None:
        update_data["descripcion"] = req.descripcion.strip() if req.descripcion else None
    if req.dificultad is not None:
        update_data["dificultad"] = req.dificultad.strip() if req.dificultad else None

    res = supabase.from_("ejercicio").update(update_data).eq("id_ejercicio", id).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ejercicio no encontrado para actualizar.")

    return {
        "mensaje": "Ejercicio actualizado con éxito.",
        "ejercicio": enriquecer_ejercicio(res.data[0]),
    }


@router.delete("/{id}", status_code=status.HTTP_200_OK)
async def eliminar_ejercicio(id: int = Path(...)):
    """Eliminar un ejercicio validando previamente si está asignado a rutinas."""
    # Verificar si está en uso en alguna rutina antes de eliminar
    res_uso = (
        supabase.from_("rutina_ejercicio")
        .select("id_rutina_ejercicio", count="exact")
        .eq("id_ejercicio", id)
        .execute()
    )
    uso_count = res_uso.count or 0

    if uso_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No se puede eliminar el ejercicio porque está asignado a {uso_count} rutina(s). Primero retíralo de las rutinas.",
        )

    res = supabase.from_("ejercicio").delete().eq("id_ejercicio", id).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ejercicio no encontrado para eliminar.")

    return {"mensaje": "Ejercicio eliminado con éxito."}
