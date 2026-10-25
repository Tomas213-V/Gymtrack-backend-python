from typing import Dict, Any, Optional, List
from fastapi import APIRouter, HTTPException, status, Depends, Query, Path
from pydantic import BaseModel

from app.config import supabase
from app.core.dependencies import get_current_user
from app.services.utils import get_data

router = APIRouter(prefix="/api/socio/ejercicios", tags=["Socio - Ejercicios"])


class SocioEjercicioCreate(BaseModel):
    nombre: str
    grupo_muscular: Optional[str] = "General"
    descripcion: Optional[str] = None
    dificultad: Optional[str] = "Intermedia"


class SocioEjercicioUpdate(BaseModel):
    nombre: Optional[str] = None
    grupo_muscular: Optional[str] = None
    descripcion: Optional[str] = None
    dificultad: Optional[str] = None


async def resolver_socio_autenticado(current_user: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """Resuelve el id_socio e id_gimnasio del usuario autenticado."""
    id_socio = current_user.get("id_socio")
    id_gimnasio = current_user.get("id_gimnasio")

    if not id_socio and current_user.get("id_usuario"):
        res = (
            supabase.from_("socio")
            .select("id_socio, id_gimnasio")
            .eq("id_usuario", current_user["id_usuario"])
            .maybe_single()
            .execute()
        )
        s_auth = get_data(res)
        if s_auth:
            id_socio = s_auth.get("id_socio")
            id_gimnasio = s_auth.get("id_gimnasio")

    if id_socio and not id_gimnasio:
        res = (
            supabase.from_("socio")
            .select("id_gimnasio")
            .eq("id_socio", id_socio)
            .maybe_single()
            .execute()
        )
        s_data = get_data(res)
        if s_data:
            id_gimnasio = s_data.get("id_gimnasio")

    return {
        "id_socio": int(id_socio) if id_socio else None,
        "id_gimnasio": int(id_gimnasio) if id_gimnasio else None,
    }


def enriquecer_ejercicio_socio(ej: Dict[str, Any], id_socio: int) -> Dict[str, Any]:
    es_propio = bool(ej.get("id_socio") and int(ej["id_socio"]) == int(id_socio))
    return {
        "id_ejercicio": ej.get("id_ejercicio"),
        "nombre": ej.get("nombre"),
        "descripcion": ej.get("descripcion") or "Sin descripción técnica disponible",
        "grupo_muscular": ej.get("grupo_muscular") or "General",
        "dificultad": ej.get("dificultad") or "Intermedia",
        "id_gimnasio": ej.get("id_gimnasio"),
        "id_socio": ej.get("id_socio"),
        "origen": "Mi ejercicio" if es_propio else "Gimnasio",
        "tipo_origen": "propio" if es_propio else "gimnasio",
        "es_propio": es_propio,
        "es_gimnasio": not bool(ej.get("id_socio")),
        "puede_editar": es_propio,
        "puede_eliminar": es_propio,
    }


@router.get("", status_code=status.HTTP_200_OK)
async def get_ejercicios_socio(
    origen: str = Query("todos"),
    grupo_muscular: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Consultar ejercicios disponibles del gimnasio y propios creados por el socio.
    Filtros opcionales: origen ('todos' | 'gimnasio' | 'propio'), grupo_muscular, search.
    """
    ident = await resolver_socio_autenticado(current_user)
    id_socio = ident["id_socio"]
    id_gimnasio = ident["id_gimnasio"]

    if not id_socio:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Acceso no autorizado: Perfil de socio no encontrado.",
        )

    query = supabase.from_("ejercicio").select("*")

    # Filtrar según el origen solicitado:
    if origen == "gimnasio":
        if id_gimnasio:
            query = query.eq("id_gimnasio", id_gimnasio).is_("id_socio", "null")
        else:
            query = query.is_("id_socio", "null")
    elif origen == "propio":
        query = query.eq("id_socio", id_socio)
    else:
        # 'todos': ejercicios oficiales del gimnasio + propios del socio
        if id_gimnasio:
            query = query.or_(f"and(id_gimnasio.eq.{id_gimnasio},id_socio.is.null),id_socio.eq.{id_socio}")
        else:
            query = query.or_(f"id_socio.is.null,id_socio.eq.{id_socio}")

    if grupo_muscular and grupo_muscular.lower() != "todos":
        query = query.ilike("grupo_muscular", f"%{grupo_muscular.strip()}%")

    if search and search.strip():
        query = query.ilike("nombre", f"%{search.strip()}%")

    res = query.order("nombre", desc=False).execute()
    ejercicios_raw = res.data or []

    ejercicios_diferenciados = [
        enriquecer_ejercicio_socio(ej, id_socio) for ej in ejercicios_raw
    ]

    return ejercicios_diferenciados


@router.post("", status_code=status.HTTP_201_CREATED)
async def crear_ejercicio_socio(
    req: SocioEjercicioCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Crear un nuevo ejercicio personalizado propio del socio."""
    ident = await resolver_socio_autenticado(current_user)
    id_socio = ident["id_socio"]
    id_gimnasio = ident["id_gimnasio"]

    if not id_socio:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Acceso no autorizado: Perfil de socio no encontrado.",
        )

    if not req.nombre or not req.nombre.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El nombre del ejercicio es obligatorio.",
        )

    nuevo = {
        "nombre": req.nombre.strip(),
        "grupo_muscular": req.grupo_muscular.strip() if req.grupo_muscular else "General",
        "descripcion": req.descripcion.strip() if req.descripcion else None,
        "dificultad": req.dificultad or "Intermedia",
        "id_socio": id_socio,
        "id_gimnasio": id_gimnasio,
    }

    res = supabase.from_("ejercicio").insert([nuevo]).execute()
    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al crear el ejercicio propio.",
        )

    creado = res.data[0]
    return {
        "mensaje": "¡Ejercicio propio creado exitosamente!",
        "ejercicio": enriquecer_ejercicio_socio(creado, id_socio),
    }


@router.put("/{id}", status_code=status.HTTP_200_OK)
async def actualizar_ejercicio_socio(
    id: int = Path(...),
    req: SocioEjercicioUpdate = ...,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Modificar un ejercicio propio.
    Valida que el socio SOLO pueda modificar ejercicios creados por él.
    """
    ident = await resolver_socio_autenticado(current_user)
    id_socio = ident["id_socio"]

    if not id_socio:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Acceso no autorizado.",
        )

    # 1. Consultar el ejercicio a editar
    res_ex = (
        supabase.from_("ejercicio")
        .select("id_ejercicio, id_socio, id_gimnasio, nombre")
        .eq("id_ejercicio", id)
        .maybe_single()
        .execute()
    )
    ej_existente = get_data(res_ex)
    if not ej_existente:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El ejercicio solicitado no fue encontrado.",
        )

    # 2. VALIDACIÓN ESTRICTA: El socio solamente puede modificar sus propios ejercicios
    if not ej_existente.get("id_socio") or int(ej_existente["id_socio"]) != int(id_socio):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permiso denegado: No tienes autorización para modificar ejercicios del gimnasio o pertenecientes a otros socios.",
        )

    update_fields = {}
    if req.nombre is not None and req.nombre.strip():
        update_fields["nombre"] = req.nombre.strip()
    if req.grupo_muscular is not None:
        update_fields["grupo_muscular"] = req.grupo_muscular.strip()
    if req.descripcion is not None:
        update_fields["descripcion"] = req.descripcion.strip() if req.descripcion else None
    if req.dificultad is not None:
        update_fields["dificultad"] = req.dificultad

    res_upd = (
        supabase.from_("ejercicio")
        .update(update_fields)
        .eq("id_ejercicio", id)
        .execute()
    )
    if not res_upd.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al actualizar el ejercicio.",
        )

    return {
        "mensaje": "Ejercicio propio actualizado con éxito.",
        "ejercicio": enriquecer_ejercicio_socio(res_upd.data[0], id_socio),
    }


@router.delete("/{id}", status_code=status.HTTP_200_OK)
async def eliminar_ejercicio_socio(
    id: int = Path(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Eliminar un ejercicio propio.
    Valida que el socio SOLO pueda eliminar ejercicios creados por él.
    """
    ident = await resolver_socio_autenticado(current_user)
    id_socio = ident["id_socio"]

    if not id_socio:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Acceso no autorizado.",
        )

    # 1. Consultar el ejercicio a eliminar
    res_ex = (
        supabase.from_("ejercicio")
        .select("id_ejercicio, id_socio, id_gimnasio, nombre")
        .eq("id_ejercicio", id)
        .maybe_single()
        .execute()
    )
    ej_existente = get_data(res_ex)
    if not ej_existente:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El ejercicio solicitado no fue encontrado.",
        )

    # 2. VALIDACIÓN ESTRICTA: El socio solamente puede eliminar sus propios ejercicios
    if not ej_existente.get("id_socio") or int(ej_existente["id_socio"]) != int(id_socio):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permiso denegado: No tienes autorización para eliminar ejercicios oficiales del gimnasio.",
        )

    # 3. Limpiar dependencias en rutinas antes de eliminar
    supabase.from_("rutina_ejercicio").delete().eq("id_ejercicio", id).execute()

    res_del = supabase.from_("ejercicio").delete().eq("id_ejercicio", id).execute()
    if not res_del.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al eliminar el ejercicio propio.",
        )

    return {
        "mensaje": f"El ejercicio propio \"{ej_existente.get('nombre')}\" fue eliminado exitosamente."
    }
