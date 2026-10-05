from datetime import date
from typing import Optional
from fastapi import APIRouter, HTTPException, status, Query, Path
from app.config import supabase
from app.services.utils import get_data
from app.models.rutina import (
    RutinaCreate,
    RutinaUpdate,
    RutinaEjercicioAdd,
    RutinaEjercicioUpdate,
)

router = APIRouter(prefix="/api/rutinas", tags=["Rutinas"])


@router.get("", status_code=status.HTTP_200_OK)
async def get_all_rutinas(
    search: Optional[str] = Query(None),
    id_socio: Optional[str] = Query(None),
    estado: Optional[str] = Query(None),
):
    """Obtener todas las rutinas con socio y ejercicios asociados."""
    query = (
        supabase.from_("rutina")
        .select("""
            id_rutina,
            nombre,
            descripcion,
            fecha_inicio,
            fecha_fin,
            estado,
            id_socio,
            socio (
                id_socio,
                nombre,
                apellido,
                dni,
                id_gimnasio
            ),
            rutina_ejercicio (
                id_rutina_ejercicio,
                id_ejercicio,
                series,
                repeticiones,
                peso,
                descanso,
                orden,
                ejercicio (
                    id_ejercicio,
                    nombre,
                    grupo_muscular,
                    dificultad
                )
            )
        """)
        .order("id_rutina", desc=True)
    )

    if id_socio and id_socio.lower() != "todos":
        query = query.eq("id_socio", int(id_socio))
    if estado and estado.lower() != "todos":
        query = query.eq("estado", estado)
    if search and search.strip():
        query = query.ilike("nombre", f"%{search.strip()}%")

    res = query.execute()
    rutinas = res.data or []

    # Ordenar los ejercicios de cada rutina por campo orden
    for r in rutinas:
        re_list = r.get("rutina_ejercicio")
        if re_list and isinstance(re_list, list):
            re_list.sort(key=lambda x: x.get("orden") or 0)

    return rutinas


@router.get("/{id}", status_code=status.HTTP_200_OK)
async def get_rutina_by_id(id: int = Path(...)):
    """Obtener detalle de rutina por ID."""
    res = (
        supabase.from_("rutina")
        .select("""
            id_rutina,
            nombre,
            descripcion,
            fecha_inicio,
            fecha_fin,
            estado,
            id_socio,
            socio (
                id_socio,
                nombre,
                apellido,
                dni,
                id_gimnasio
            ),
            rutina_ejercicio (
                id_rutina_ejercicio,
                id_ejercicio,
                series,
                repeticiones,
                peso,
                descanso,
                orden,
                ejercicio (
                    id_ejercicio,
                    nombre,
                    grupo_muscular,
                    dificultad
                )
            )
        """)
        .eq("id_rutina", id)
        .maybe_single()
        .execute()
    )

    data = get_data(res)
    if not data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rutina con ID {id} no encontrada."
        )

    if data.get("rutina_ejercicio") and isinstance(data["rutina_ejercicio"], list):
        data["rutina_ejercicio"].sort(key=lambda x: x.get("orden") or 0)

    return data


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_rutina(req: RutinaCreate):
    """Crear una nueva rutina."""
    if not req.nombre or not req.nombre.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre de la rutina es obligatorio.")

    nueva_rutina = {
        "nombre": req.nombre.strip(),
        "descripcion": req.descripcion.strip() if req.descripcion else None,
        "id_socio": int(req.id_socio) if req.id_socio else None,
        "fecha_inicio": req.fecha_inicio or date.today().isoformat(),
        "fecha_fin": req.fecha_fin or None,
        "estado": req.estado or "asignada",
    }

    res = supabase.from_("rutina").insert([nueva_rutina]).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al crear rutina.")

    return res.data[0]


@router.put("/{id}", status_code=status.HTTP_200_OK)
async def update_rutina(
    id: int = Path(...),
    req: RutinaUpdate = ...
):
    """Actualizar rutina existente."""
    update_data = {}
    if req.nombre is not None:
        update_data["nombre"] = req.nombre.strip()
    if req.descripcion is not None:
        update_data["descripcion"] = req.descripcion.strip() if req.descripcion else None
    if req.id_socio is not None:
        update_data["id_socio"] = int(req.id_socio) if req.id_socio else None
    if req.fecha_inicio is not None:
        update_data["fecha_inicio"] = req.fecha_inicio
    if req.fecha_fin is not None:
        update_data["fecha_fin"] = req.fecha_fin
    if req.estado is not None:
        update_data["estado"] = req.estado

    res = supabase.from_("rutina").update(update_data).eq("id_rutina", id).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No se encontró la rutina {id} para actualizar.")

    return res.data[0]


@router.delete("/{id}", status_code=status.HTTP_200_OK)
async def delete_rutina(id: int = Path(...)):
    """Eliminar rutina y limpiar relaciones de ejercicios."""
    # 1. Eliminar relaciones en rutina_ejercicio
    supabase.from_("rutina_ejercicio").delete().eq("id_rutina", id).execute()

    # 2. Eliminar rutina
    res = supabase.from_("rutina").delete().eq("id_rutina", id).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"La rutina {id} no existe.")

    return {"message": f"Rutina {id} eliminada correctamente."}


# Endpoints de relación con Ejercicios

@router.get("/{id}/ejercicios", status_code=status.HTTP_200_OK)
async def get_ejercicios_by_rutina(id: int = Path(...)):
    """Consultar los ejercicios pertenecientes a una rutina."""
    res = (
        supabase.from_("rutina_ejercicio")
        .select("""
            id_rutina_ejercicio,
            id_ejercicio,
            series,
            repeticiones,
            peso,
            descanso,
            orden,
            ejercicio (
                id_ejercicio,
                nombre,
                grupo_muscular,
                dificultad
            )
        """)
        .eq("id_rutina", id)
        .order("orden", desc=False)
        .execute()
    )

    return res.data or []


@router.post("/{id}/ejercicios", status_code=status.HTTP_201_CREATED)
async def add_ejercicio_to_rutina(
    id: int = Path(...),
    req: RutinaEjercicioAdd = ...
):
    """Asociar un ejercicio a una rutina."""
    num_series = req.series or req.serie or 4
    num_reps = req.repeticiones if req.repeticiones is not None else 12
    num_peso = float(req.peso) if req.peso is not None else 0.0
    num_descanso = req.descanso if req.descanso is not None else 60
    num_orden = req.orden if req.orden is not None else 1

    insert_data = {
        "id_rutina": id,
        "id_ejercicio": int(req.id_ejercicio),
        "series": int(num_series),
        "repeticiones": int(num_reps),
        "peso": num_peso,
        "descanso": int(num_descanso),
        "orden": int(num_orden),
    }

    res = (
        supabase.from_("rutina_ejercicio")
        .insert([insert_data])
        .execute()
    )
    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Error al asociar ejercicio a la rutina. Verifica que existan ambos IDs."
        )

    # Devolver con detalle del ejercicio
    id_rel = res.data[0]["id_rutina_ejercicio"]
    res_full = (
        supabase.from_("rutina_ejercicio")
        .select("""
            id_rutina_ejercicio,
            id_ejercicio,
            series,
            repeticiones,
            peso,
            descanso,
            orden,
            ejercicio (
                id_ejercicio,
                nombre,
                grupo_muscular,
                dificultad
            )
        """)
        .eq("id_rutina_ejercicio", id_rel)
        .single()
        .execute()
    )

    return res_full.data


@router.put("/{id}/ejercicios/{ejercicio_id}", status_code=status.HTTP_200_OK)
async def update_ejercicio_en_rutina(
    id: int = Path(...),
    ejercicio_id: int = Path(...),
    req: RutinaEjercicioUpdate = ...
):
    """Actualizar parámetros de un ejercicio en una rutina (series, repeticiones, peso, etc.)."""
    update_data = {}
    if req.series is not None:
        update_data["series"] = int(req.series)
    elif req.serie is not None:
        update_data["series"] = int(req.serie)
    if req.repeticiones is not None:
        update_data["repeticiones"] = int(req.repeticiones)
    if req.peso is not None:
        update_data["peso"] = float(req.peso)
    if req.descanso is not None:
        update_data["descanso"] = int(req.descanso)
    if req.orden is not None:
        update_data["orden"] = int(req.orden)

    query = (
        supabase.from_("rutina_ejercicio")
        .update(update_data)
        .eq("id_rutina", id)
        .or_(f"id_rutina_ejercicio.eq.{ejercicio_id},id_ejercicio.eq.{ejercicio_id}")
    )

    res = query.execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ejercicio no encontrado en la rutina.")

    return res.data[0]


@router.delete("/{id}/ejercicios/{ejercicio_id}", status_code=status.HTTP_200_OK)
async def remove_ejercicio_from_rutina(
    id: int = Path(...),
    ejercicio_id: int = Path(...)
):
    """Desvincular ejercicio de una rutina."""
    res = (
        supabase.from_("rutina_ejercicio")
        .delete()
        .eq("id_rutina", id)
        .or_(f"id_rutina_ejercicio.eq.{ejercicio_id},id_ejercicio.eq.{ejercicio_id}")
        .execute()
    )

    if not res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="El ejercicio no estaba asociado a esta rutina.")

    return {"message": "Ejercicio desvinculado con éxito."}
