from typing import Dict, Any, Optional, List
from fastapi import APIRouter, HTTPException, status, Depends, Query, Path
from supabase import Client

from app.core.dependencies import get_current_user
from app.services.utils import get_data
from app.models.ejercicio import EjercicioSocioCreate, EjercicioSocioUpdate
from app.routers.socios import (
    _resolve_socio_profile_context,
    _fetch_socio_gimnasios_for_socio,
)
from app.routers.ejercicios import (
    EJERCICIO_SOCIO_COLUMNS,
    EJERCICIO_GIMNASIO_COLUMNS,
    _filter_ejercicios,
)

router = APIRouter(prefix="/api/socio/ejercicios", tags=["Socio - Ejercicios Personales"])


def _format_ejercicio_socio(ej: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id_ejercicio_socio": ej.get("id_ejercicio_socio"),
        "id_socio": ej.get("id_socio"),
        "nombre": ej.get("nombre"),
        "descripcion": ej.get("descripcion"),
        "grupo_muscular": ej.get("grupo_muscular") or "General",
        "dificultad": ej.get("dificultad") or "Intermedia",
        "estado": ej.get("estado", True),
        "creado_en": ej.get("creado_en"),
        "origen": "Mi ejercicio",
        "tipo_origen": "propio",
        "es_propio": True,
        "es_gimnasio": False,
        "puede_editar": True,
        "puede_eliminar": True,
    }


def _format_ejercicio_gym_para_socio(ej: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id_ejercicio_gimnasio": ej.get("id_ejercicio_gimnasio"),
        "id_gimnasio": ej.get("id_gimnasio"),
        "nombre": ej.get("nombre"),
        "descripcion": ej.get("descripcion"),
        "grupo_muscular": ej.get("grupo_muscular") or "General",
        "dificultad": ej.get("dificultad") or "Intermedia",
        "estado": ej.get("estado", True),
        "creado_en": ej.get("creado_en"),
        "origen": "Gimnasio",
        "tipo_origen": "gimnasio",
        "es_propio": False,
        "es_gimnasio": True,
        "puede_editar": False,
        "puede_eliminar": False,
    }


def _fetch_ejercicio_socio_or_404(db: Client, id_ejercicio_socio: int) -> Dict[str, Any]:
    res = (
        db.from_("ejercicio_socio")
        .select(EJERCICIO_SOCIO_COLUMNS)
        .eq("id_ejercicio_socio", id_ejercicio_socio)
        .maybe_single()
        .execute()
    )
    ej = get_data(res)
    if not isinstance(ej, dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ejercicio personal no encontrado.",
        )
    return ej


@router.get("", status_code=status.HTTP_200_OK)
async def get_ejercicios_socio(
    origen: str = Query(default="propio"),
    id_gimnasio: Optional[int] = Query(default=None, gt=0),
    grupo_muscular: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Consulta los ejercicios personales (`public.ejercicio_socio`) del socio autenticado y,
    opcionalmente (`origen='todos'` o `'gimnasio'`), los ejercicios activos de sus gimnasios
    asociados (`public.ejercicio_gimnasio`), manteniéndolos claramente diferenciados.
    """
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]

    origen_norm = origen.strip().lower()
    resultado: List[Dict[str, Any]] = []

    if origen_norm in ("propio", "todos"):
        res_p = (
            db.from_("ejercicio_socio")
            .select(EJERCICIO_SOCIO_COLUMNS)
            .eq("id_socio", own_socio_id)
            .order("nombre", desc=False)
            .execute()
        )
        raw_p = get_data(res_p) or []
        for ej in _filter_ejercicios(
            raw_p if isinstance(raw_p, list) else [],
            grupo_muscular=grupo_muscular,
            search=search,
        ):
            if int(ej.get("id_socio") or 0) == own_socio_id:
                resultado.append(_format_ejercicio_socio(ej))

    if origen_norm in ("gimnasio", "todos"):
        asocs = _fetch_socio_gimnasios_for_socio(db, own_socio_id)
        active_gyms = {
            int(a["id_gimnasio"])
            for a in asocs
            if a.get("estado") is True and a.get("id_gimnasio") is not None
        }
        effective_gym_id = id_gimnasio if id_gimnasio is not None else current_user.get("requested_gym_id")
        if effective_gym_id is not None and int(effective_gym_id) not in active_gyms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No posees asociación activa con el gimnasio solicitado.",
            )
        target_gyms = [int(effective_gym_id)] if effective_gym_id is not None else sorted(active_gyms)
        if target_gyms:
            q_g = db.from_("ejercicio_gimnasio").select(EJERCICIO_GIMNASIO_COLUMNS)
            if len(target_gyms) == 1:
                q_g = q_g.eq("id_gimnasio", target_gyms[0])
            else:
                q_g = q_g.in_("id_gimnasio", target_gyms)
            res_g = q_g.order("nombre", desc=False).execute()
            raw_g = get_data(res_g) or []
            for ej in _filter_ejercicios(
                raw_g if isinstance(raw_g, list) else [],
                grupo_muscular=grupo_muscular,
                search=search,
                only_active=True,
            ):
                if int(ej.get("id_gimnasio") or 0) in set(target_gyms):
                    resultado.append(_format_ejercicio_gym_para_socio(ej))

    return resultado


@router.post("", status_code=status.HTTP_201_CREATED)
async def crear_ejercicio_socio(
    req: EjercicioSocioCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Crea un ejercicio personal en `public.ejercicio_socio` propiedad exclusiva del socio autenticado."""
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])

    if req.id_socio is not None and str(req.id_socio).strip() != str(own_socio_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No puedes crear ejercicios personales para otro socio.",
        )

    clean_nombre = req.nombre.strip()
    if not clean_nombre:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre del ejercicio es obligatorio.")

    db: Client = socio_ctx["supabase"]
    nuevo = {
        "id_socio": own_socio_id,
        "nombre": clean_nombre,
        "grupo_muscular": req.grupo_muscular.strip() if req.grupo_muscular and req.grupo_muscular.strip() else "General",
        "descripcion": req.descripcion.strip() if req.descripcion and req.descripcion.strip() else None,
        "dificultad": req.dificultad.strip() if req.dificultad and req.dificultad.strip() else "Intermedia",
        "estado": bool(req.estado),
    }

    res = db.from_("ejercicio_socio").insert([nuevo]).execute()
    rows = get_data(res) or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al crear el ejercicio personal.")

    return {
        "mensaje": "Ejercicio personal creado exitosamente.",
        "ejercicio": _format_ejercicio_socio(rows[0]),
    }


@router.put("/{id_ejercicio_socio}", status_code=status.HTTP_200_OK)
async def actualizar_ejercicio_socio(
    req: EjercicioSocioUpdate,
    id_ejercicio_socio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Actualiza un ejercicio personal de `public.ejercicio_socio` validando propiedad exclusiva del socio."""
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])

    if req.id_socio is not None and str(req.id_socio).strip() != str(own_socio_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No está permitido transferir un ejercicio personal a otro socio.",
        )

    db: Client = socio_ctx["supabase"]
    ej_existente = _fetch_ejercicio_socio_or_404(db, id_ejercicio_socio)
    if int(ej_existente.get("id_socio") or 0) != own_socio_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes autorización para modificar ejercicios personales de otro socio.",
        )

    update_fields: Dict[str, Any] = {}
    if req.nombre is not None:
        clean_nombre = req.nombre.strip()
        if not clean_nombre:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre no puede estar vacío.")
        update_fields["nombre"] = clean_nombre
    if req.grupo_muscular is not None:
        update_fields["grupo_muscular"] = req.grupo_muscular.strip() or None
    if req.descripcion is not None:
        update_fields["descripcion"] = req.descripcion.strip() or None
    if req.dificultad is not None:
        update_fields["dificultad"] = req.dificultad.strip() or None
    if req.estado is not None:
        update_fields["estado"] = bool(req.estado)

    if not update_fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No se enviaron campos para actualizar.")

    res_upd = (
        db.from_("ejercicio_socio")
        .update(update_fields)
        .eq("id_ejercicio_socio", id_ejercicio_socio)
        .eq("id_socio", own_socio_id)
        .execute()
    )
    rows = get_data(res_upd) or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ejercicio personal no encontrado.")

    return {
        "mensaje": "Ejercicio personal actualizado con éxito.",
        "ejercicio": _format_ejercicio_socio(rows[0]),
    }


@router.delete("/{id_ejercicio_socio}", status_code=status.HTTP_200_OK)
async def eliminar_ejercicio_socio(
    id_ejercicio_socio: int = Path(..., gt=0),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Elimina un ejercicio personal de `public.ejercicio_socio` verificando que no esté referenciado en `rutina_ejercicio_socio`."""
    socio_ctx = _resolve_socio_profile_context(current_user)
    own_socio_id = int(socio_ctx["id_socio"])
    db: Client = socio_ctx["supabase"]

    ej_existente = _fetch_ejercicio_socio_or_404(db, id_ejercicio_socio)
    if int(ej_existente.get("id_socio") or 0) != own_socio_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes autorización para eliminar ejercicios personales de otro socio.",
        )

    res_uso = (
        db.from_("rutina_ejercicio_socio")
        .select("id_rutina_ejercicio_socio")
        .eq("id_ejercicio_socio", id_ejercicio_socio)
        .eq("id_socio", own_socio_id)
        .execute()
    )
    usos = get_data(res_uso) or []
    if isinstance(usos, list) and len(usos) > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"No se puede eliminar el ejercicio porque está en uso en {len(usos)} rutina(s) personal(es). Retíralo primero o desactívalo.",
        )

    res_del = (
        db.from_("ejercicio_socio")
        .delete()
        .eq("id_ejercicio_socio", id_ejercicio_socio)
        .eq("id_socio", own_socio_id)
        .execute()
    )
    rows = get_data(res_del) or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ejercicio personal no encontrado.")

    return {
        "mensaje": f'El ejercicio personal "{ej_existente.get("nombre")}" fue eliminado exitosamente.',
    }
