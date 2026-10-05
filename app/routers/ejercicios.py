from typing import Optional
from fastapi import APIRouter, HTTPException, status, Query, Path
from app.config import supabase
from app.models.ejercicio import EjercicioCreate, EjercicioUpdate

router = APIRouter(prefix="/api/ejercicios", tags=["Ejercicios"])


@router.get("", status_code=status.HTTP_200_OK)
async def get_ejercicios(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1),
    grupo_muscular: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
):
    """Consultar ejercicios con paginación, filtros de grupo muscular y búsqueda por nombre."""
    offset = (page - 1) * limit
    query = supabase.from_("ejercicio").select("*", count="exact").range(offset, offset + limit - 1)

    if grupo_muscular:
        query = query.ilike("grupo_muscular", f"%{grupo_muscular}%")
    if search:
        query = query.ilike("nombre", f"%{search}%")

    res = query.execute()

    return {
        "total": res.count or 0,
        "pagina": page,
        "limite": limit,
        "datos": res.data or [],
    }


@router.get("/{id}", status_code=status.HTTP_200_OK)
async def get_ejercicio_by_id(id: int = Path(...)):
    """Obtener un ejercicio por ID."""
    res = supabase.from_("ejercicio").select("*").eq("id_ejercicio", id).maybe_single().execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ejercicio no encontrado.")

    return res.data


@router.post("", status_code=status.HTTP_201_CREATED)
async def crear_ejercicio(req: EjercicioCreate):
    """Registrar un nuevo ejercicio."""
    if not req.nombre or not req.nombre.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre del ejercicio es obligatorio.")

    nuevo_ej = {
        "nombre": req.nombre.strip(),
        "grupo_muscular": req.grupo_muscular.strip() if req.grupo_muscular else None,
        "descripcion": req.descripcion.strip() if req.descripcion else None,
        "dificultad": req.dificultad.strip() if req.dificultad else None,
    }

    res = supabase.from_("ejercicio").insert([nuevo_ej]).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al crear ejercicio.")

    return {
        "mensaje": "Ejercicio creado con éxito.",
        "ejercicio": res.data[0],
    }


@router.put("/{id}", status_code=status.HTTP_200_OK)
async def actualizar_ejercicio(
    id: int = Path(...),
    req: EjercicioUpdate = ...
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
        "ejercicio": res.data[0],
    }


@router.delete("/{id}", status_code=status.HTTP_200_OK)
async def eliminar_ejercicio(id: int = Path(...)):
    """Eliminar un ejercicio."""
    res = supabase.from_("ejercicio").delete().eq("id_ejercicio", id).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ejercicio no encontrado para eliminar.")

    return {"mensaje": "Ejercicio eliminado con éxito."}
