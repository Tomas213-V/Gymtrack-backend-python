import json
from datetime import date
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, HTTPException, status, Query, Path, Depends
from app.config import supabase
from app.core.dependencies import get_optional_user
from app.services.utils import get_data
from app.models.rutina import (
    RutinaCreate,
    RutinaUpdate,
    RutinaAdoptar,
    RutinaEjercicioAdd,
    RutinaEjercicioUpdate,
)

router = APIRouter(prefix="/api/rutinas", tags=["Rutinas"])


def enriquecer_ejercicios_rutina(rutina_ejercicios: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Enriquecer listado de ejercicios asociados con origen ('Gimnasio' vs 'Mi ejercicio')."""
    if not rutina_ejercicios or not isinstance(rutina_ejercicios, list):
        return []
    enriquecidos = []
    for re_item in rutina_ejercicios:
        ej = re_item.get("ejercicio") or {}
        es_propio = bool(ej.get("id_socio"))
        enriquecidos.append({
            **re_item,
            "origen": "Mi ejercicio" if es_propio else "Gimnasio",
            "tipo_origen": "propio" if es_propio else "gimnasio",
            "es_propio": es_propio,
            "es_gimnasio": not es_propio,
        })
    return enriquecidos


# 1. Catálogo de Rutinas Ofrecidas por el Gimnasio (Programas Oficiales)
@router.get("/gym/ofrecidas", status_code=status.HTTP_200_OK)
@router.get("/ofrecidas-gym", status_code=status.HTTP_200_OK)
@router.get("/ofrecidas", status_code=status.HTTP_200_OK)
async def get_rutinas_ofrecidas_gym(
    current_user: Optional[Dict[str, Any]] = Depends(get_optional_user),
):
    """Devuelve las plantillas y programas oficiales recomendados por el gimnasio con ejercicios enlazados."""
    gym_id = current_user.get("id_gimnasio") if current_user else None

    # Consultar ejercicios oficiales del gimnasio para vincularlos con las plantillas
    ej_query = (
        supabase.from_("ejercicio")
        .select("id_ejercicio, nombre, grupo_muscular, dificultad, descripcion")
        .is_("id_socio", "null")
    )
    if gym_id:
        ej_query = ej_query.or_(f"id_gimnasio.eq.{gym_id},id_gimnasio.is.null")

    res_ej = ej_query.limit(100).execute()
    lista_ej = res_ej.data or []

    def encontrar_ej(nombre_parcial: str, musculo: Optional[str] = None):
        for e in lista_ej:
            if nombre_parcial.lower() in (e.get("nombre") or "").lower():
                return e
        if musculo:
            for e in lista_ej:
                if (e.get("grupo_muscular") or "").lower() == musculo.lower():
                    return e
        return lista_ej[0] if lista_ej else None

    programas_gym = [
        {
            "id_plantilla": "gym-fullbody-intro",
            "nombre": "Full Body: Acondicionamiento & Fuerza Integral",
            "tipo": "Full Body",
            "dificultad": "Principiante",
            "duracion": "50 min",
            "dias": "Lunes, Miércoles, Viernes",
            "objetivo": "Acondicionamiento físico, ganancia de fuerza inicial y adaptación neuromuscular.",
            "notas": "Ideal para quienes inician o retoman el gimnasio. Realizar calentamiento previo de 5 a 10 min.",
            "nivel_badge": "Principiante",
            "frecuencia": "3 días por semana",
            "ejercicios_plantilla": [
                {"query": "Sentadilla", "musculo": "Piernas", "series": 3, "repeticiones": 12, "peso": 20, "descanso": 60},
                {"query": "banca plano", "musculo": "Pecho", "series": 3, "repeticiones": 10, "peso": 30, "descanso": 75},
                {"query": "Jalón al pecho", "musculo": "Espalda", "series": 3, "repeticiones": 12, "peso": 35, "descanso": 60},
                {"query": "Press militar", "musculo": "Hombros", "series": 3, "repeticiones": 10, "peso": 12, "descanso": 60},
                {"query": "Curl de bíceps", "musculo": "Brazos", "series": 3, "repeticiones": 12, "peso": 15, "descanso": 45},
                {"query": "Plancha", "musculo": "Abdomen", "series": 3, "repeticiones": 30, "peso": 0, "descanso": 45},
            ],
        },
        {
            "id_plantilla": "gym-torso-pierna",
            "nombre": "Torso / Pierna: Hipertrofia & Rendimiento",
            "tipo": "Torso - Pierna",
            "dificultad": "Intermedia",
            "duracion": "65 min",
            "dias": "Lunes, Martes, Jueves, Viernes",
            "objetivo": "Maximizar el crecimiento muscular con balance perfecto entre volumen y recuperación.",
            "notas": "Separar los días de torso y pierna para permitir la recuperación muscular adecuada.",
            "nivel_badge": "Intermedio",
            "frecuencia": "4 días por semana",
            "ejercicios_plantilla": [
                {"query": "banca plano", "musculo": "Pecho", "series": 4, "repeticiones": 8, "peso": 50, "descanso": 90},
                {"query": "Remo con barra", "musculo": "Espalda", "series": 4, "repeticiones": 10, "peso": 45, "descanso": 75},
                {"query": "Press inclinado", "musculo": "Pecho", "series": 3, "repeticiones": 12, "peso": 20, "descanso": 60},
                {"query": "Dominadas", "musculo": "Espalda", "series": 3, "repeticiones": 8, "peso": 0, "descanso": 90},
                {"query": "Sentadilla", "musculo": "Piernas", "series": 4, "repeticiones": 10, "peso": 60, "descanso": 90},
                {"query": "Prensa de piernas", "musculo": "Piernas", "series": 4, "repeticiones": 12, "peso": 100, "descanso": 75},
                {"query": "Extensiones de cuádriceps", "musculo": "Piernas", "series": 3, "repeticiones": 15, "peso": 40, "descanso": 60},
                {"query": "Curl femoral", "musculo": "Piernas", "series": 3, "repeticiones": 12, "peso": 35, "descanso": 60},
            ],
        },
        {
            "id_plantilla": "gym-ppl-elite",
            "nombre": "Push / Pull / Legs (PPL): Élite Muscular",
            "tipo": "Push - Pull - Legs",
            "dificultad": "Avanzada",
            "duracion": "70 min",
            "dias": "Lunes a Viernes",
            "objetivo": "Enfoque biomecánico en empujes, jalones y piernas para atletas comprometidos.",
            "notas": "Controlar la fase excéntrica del movimiento (2-3 segundos) para máximo reclutamiento.",
            "nivel_badge": "Avanzado",
            "frecuencia": "5 días por semana",
            "ejercicios_plantilla": [
                {"query": "banca plano", "musculo": "Pecho", "series": 4, "repeticiones": 8, "peso": 65, "descanso": 90},
                {"query": "Press militar", "musculo": "Hombros", "series": 4, "repeticiones": 10, "peso": 22, "descanso": 75},
                {"query": "Aperturas en polea", "musculo": "Pecho", "series": 3, "repeticiones": 15, "peso": 15, "descanso": 60},
                {"query": "Extensiones de tríceps", "musculo": "Brazos", "series": 4, "repeticiones": 12, "peso": 25, "descanso": 45},
                {"query": "Dominadas", "musculo": "Espalda", "series": 4, "repeticiones": 8, "peso": 0, "descanso": 90},
                {"query": "Remo con barra", "musculo": "Espalda", "series": 4, "repeticiones": 10, "peso": 55, "descanso": 75},
                {"query": "Curl de bíceps", "musculo": "Brazos", "series": 4, "repeticiones": 12, "peso": 20, "descanso": 60},
                {"query": "Sentadilla", "musculo": "Piernas", "series": 4, "repeticiones": 8, "peso": 75, "descanso": 120},
            ],
        },
        {
            "id_plantilla": "gym-fatburn-hiit",
            "nombre": "Quema de Grasa & HIIT Funcional",
            "tipo": "Funcional & HIIT",
            "dificultad": "Intermedia",
            "duracion": "45 min",
            "dias": "Lunes, Miércoles, Viernes",
            "objetivo": "Acelerar el metabolismo basal, elevar pulsaciones y tonificar cuerpo completo.",
            "notas": "Descansos cortos y dinámicos para optimizar el consumo de oxígeno post-entrenamiento (EPOC).",
            "nivel_badge": "Dinámico",
            "frecuencia": "3 días por semana",
            "ejercicios_plantilla": [
                {"query": "Sentadilla", "musculo": "Piernas", "series": 4, "repeticiones": 15, "peso": 20, "descanso": 45},
                {"query": "banca plano", "musculo": "Pecho", "series": 4, "repeticiones": 12, "peso": 25, "descanso": 45},
                {"query": "Jalón al pecho", "musculo": "Espalda", "series": 4, "repeticiones": 12, "peso": 30, "descanso": 45},
                {"query": "Elevaciones laterales", "musculo": "Hombros", "series": 3, "repeticiones": 15, "peso": 8, "descanso": 30},
                {"query": "Plancha", "musculo": "Abdomen", "series": 4, "repeticiones": 40, "peso": 0, "descanso": 30},
            ],
        },
        {
            "id_plantilla": "gym-strength-5x5",
            "nombre": "Fuerza Bruta 5×5 Powerlifting",
            "tipo": "Fuerza Pura",
            "dificultad": "Avanzada",
            "duracion": "60 min",
            "dias": "Lunes, Miércoles, Viernes",
            "objetivo": "Desarrollo de fuerza máxima en movimientos multiarticulares con progresión lineal.",
            "notas": "5 series de 5 repeticiones efectivas con 80-85% del 1RM. Descansos largos de 2 a 3 minutos.",
            "nivel_badge": "Fuerza",
            "frecuencia": "3 días por semana",
            "ejercicios_plantilla": [
                {"query": "Sentadilla", "musculo": "Piernas", "series": 5, "repeticiones": 5, "peso": 80, "descanso": 120},
                {"query": "banca plano", "musculo": "Pecho", "series": 5, "repeticiones": 5, "peso": 70, "descanso": 120},
                {"query": "Remo con barra", "musculo": "Espalda", "series": 5, "repeticiones": 5, "peso": 60, "descanso": 120},
                {"query": "Press militar", "musculo": "Hombros", "series": 5, "repeticiones": 5, "peso": 40, "descanso": 90},
            ],
        },
        {
            "id_plantilla": "gym-gluteos-piernas",
            "nombre": "Glúteos & Tren Inferior Esculpido",
            "tipo": "Piernas & Glúteos",
            "dificultad": "Intermedia",
            "duracion": "55 min",
            "dias": "Martes, Jueves, Sábado",
            "objetivo": "Focalización en desarrollo de glúteos, isquiosurales, cuádriceps y abdomen firme.",
            "notas": "Concentrarse en la contracción en el punto máximo y tensión mecánica constante.",
            "nivel_badge": "Especializado",
            "frecuencia": "3 días por semana",
            "ejercicios_plantilla": [
                {"query": "Sentadilla", "musculo": "Piernas", "series": 4, "repeticiones": 10, "peso": 40, "descanso": 90},
                {"query": "Prensa de piernas", "musculo": "Piernas", "series": 4, "repeticiones": 12, "peso": 80, "descanso": 75},
                {"query": "Extensiones de cuádriceps", "musculo": "Piernas", "series": 3, "repeticiones": 15, "peso": 30, "descanso": 60},
                {"query": "Curl femoral", "musculo": "Piernas", "series": 4, "repeticiones": 12, "peso": 25, "descanso": 60},
                {"query": "Plancha", "musculo": "Abdomen", "series": 3, "repeticiones": 45, "peso": 0, "descanso": 45},
            ],
        },
    ]

    programas_completos = []
    for prog in programas_gym:
        ejercicios_asociados = []
        for idx, item in enumerate(prog["ejercicios_plantilla"]):
            ej_db = encontrar_ej(item["query"], item["musculo"])
            ejercicios_asociados.append({
                "id_ejercicio": ej_db["id_ejercicio"] if ej_db else idx + 1,
                "nombre": ej_db["nombre"] if ej_db else f"{item['query']} oficial",
                "grupo_muscular": ej_db["grupo_muscular"] if ej_db else item["musculo"],
                "dificultad": ej_db["dificultad"] if ej_db else prog["dificultad"],
                "descripcion": ej_db["descripcion"] if ej_db else "Ejercicio oficial recomendado por el gimnasio",
                "series": item["series"],
                "repeticiones": item["repeticiones"],
                "peso": item["peso"],
                "descanso": item["descanso"],
                "orden": idx + 1,
            })

        programas_completos.append({
            "id_plantilla": prog["id_plantilla"],
            "nombre": prog["nombre"],
            "tipo": prog["tipo"],
            "dificultad": prog["dificultad"],
            "duracion": prog["duracion"],
            "dias": prog["dias"],
            "objetivo": prog["objetivo"],
            "notas": prog["notas"],
            "nivel_badge": prog["nivel_badge"],
            "frecuencia": prog["frecuencia"],
            "es_ofrecida_gym": True,
            "total_ejercicios": len(ejercicios_asociados),
            "ejercicios": ejercicios_asociados,
        })

    return programas_completos


# 2. Adoptar / Comenzar una Rutina del Gimnasio
@router.post("/adoptar", status_code=status.HTTP_201_CREATED)
@router.post("/adoptar-gym", status_code=status.HTTP_201_CREATED)
async def adoptar_rutina_gym(
    req: RutinaAdoptar,
    current_user: Optional[Dict[str, Any]] = Depends(get_optional_user),
):
    """Permite al socio adoptar una rutina recomendada del catálogo del gimnasio."""
    target_socio_id = req.id_socio or (current_user.get("id_socio") if current_user else None)

    if not target_socio_id and current_user and current_user.get("id_usuario"):
        res_s = (
            supabase.from_("socio")
            .select("id_socio, id_gimnasio")
            .eq("id_usuario", current_user["id_usuario"])
            .maybe_single()
            .execute()
        )
        soc_data = get_data(res_s)
        if soc_data:
            target_socio_id = soc_data.get("id_socio")

    if not target_socio_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Se requiere el ID del socio para adoptar la rutina.",
        )

    plan = req.plantilla
    if not plan and req.id_plantilla:
        # Si vino el ID de la plantilla, consultar el catálogo oficial
        catalogo = await get_rutinas_ofrecidas_gym(current_user)
        plan = next((p for p in catalogo if p["id_plantilla"] == req.id_plantilla), None)

    if not plan:
        plan = {
            "nombre": req.nombre_personalizado or "Rutina Adoptada del Gimnasio",
            "tipo": "Full Body",
            "dificultad": "Intermedia",
            "duracion": "60 min",
            "dias": "Lunes, Miércoles, Viernes",
            "objetivo": "Plan de entrenamiento sugerido por el gimnasio",
            "notas": "Adoptada del catálogo de rutinas del gimnasio.",
            "ejercicios": [],
        }

    # Obtener gimnasio del socio
    res_soc = (
        supabase.from_("socio")
        .select("id_gimnasio")
        .eq("id_socio", target_socio_id)
        .maybe_single()
        .execute()
    )
    socio_db = get_data(res_soc)
    gym_id = (socio_db.get("id_gimnasio") if socio_db else None) or (current_user.get("id_gimnasio") if current_user else None)

    # Desactivar cualquier otra rutina activa previa del socio
    supabase.from_("rutina").update({"estado": "asignada"}).eq("id_socio", target_socio_id).eq("estado", "activa").execute()

    meta_descripcion = json.dumps({
        "tipo": plan.get("tipo", "Full Body"),
        "dificultad": plan.get("dificultad", "Intermedia"),
        "duracion": plan.get("duracion", "60 min"),
        "dias": plan.get("dias", "Lunes, Miércoles, Viernes"),
        "notas": plan.get("notas", "Plan adoptado del gimnasio."),
        "objetivo": plan.get("objetivo", ""),
        "origen_oficial_gym": True,
    })

    nueva_rutina = {
        "nombre": (req.nombre_personalizado or plan.get("nombre") or "Mi Rutina del Gimnasio").strip(),
        "descripcion": meta_descripcion,
        "id_socio": target_socio_id,
        "id_gimnasio": gym_id,
        "fecha_inicio": date.today().isoformat(),
        "estado": "activa",
    }

    res_ins = supabase.from_("rutina").insert([nueva_rutina]).execute()
    if not res_ins.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al crear la rutina para el socio.")

    rutina_creada = res_ins.data[0]
    id_rutina_nueva = rutina_creada["id_rutina"]

    ejercicios_para_insertar = plan.get("ejercicios") or []
    asociados = []

    for i, item in enumerate(ejercicios_para_insertar):
        id_ej = item.get("id_ejercicio")
        if id_ej:
            insert_ej = {
                "id_rutina": id_rutina_nueva,
                "id_ejercicio": int(id_ej),
                "series": int(item.get("series") or 4),
                "repeticiones": int(item.get("repeticiones") or 10),
                "peso": float(item.get("peso") or 0.0),
                "descanso": int(item.get("descanso") or 60),
                "orden": i + 1,
            }
            try:
                res_re = (
                    supabase.from_("rutina_ejercicio")
                    .insert([insert_ej])
                    .execute()
                )
                if res_re.data:
                    id_re = res_re.data[0]["id_rutina_ejercicio"]
                    res_item_full = (
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
                        .eq("id_rutina_ejercicio", id_re)
                        .maybe_single()
                        .execute()
                    )
                    item_full = get_data(res_item_full)
                    if item_full:
                        asociados.append(item_full)
            except Exception:
                pass

    rutina_creada["rutina_ejercicio"] = asociados

    return {
        "mensaje": "¡Rutina adoptada con éxito! Ya está configurada como tu rutina activa.",
        "rutina": rutina_creada,
    }


# 3. Activar Rutina (Establecer como la principal del socio)
@router.patch("/{id}/activar", status_code=status.HTTP_200_OK)
@router.put("/{id}/activar", status_code=status.HTTP_200_OK)
@router.post("/{id}/activar", status_code=status.HTTP_200_OK)
async def activar_rutina(id: int = Path(...)):
    """Activa una rutina específica y pasa a estado 'asignada' cualquier otra rutina previa del socio."""
    res_r = (
        supabase.from_("rutina")
        .select("id_rutina, id_socio")
        .eq("id_rutina", id)
        .maybe_single()
        .execute()
    )
    rutina = get_data(res_r)
    if not rutina:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="La rutina especificada no existe.")

    socio_id = rutina.get("id_socio")

    if socio_id:
        supabase.from_("rutina").update({"estado": "asignada"}).eq("id_socio", socio_id).eq("estado", "activa").execute()

    res_act = (
        supabase.from_("rutina")
        .update({"estado": "activa"})
        .eq("id_rutina", id)
        .execute()
    )
    if not res_act.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al activar la rutina.")

    res_full = (
        supabase.from_("rutina")
        .select("""
            id_rutina,
            nombre,
            descripcion,
            estado,
            id_socio,
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
        .single()
        .execute()
    )

    activada = res_full.data
    if activada:
        activada["rutina_ejercicio"] = enriquecer_ejercicios_rutina(activada.get("rutina_ejercicio"))

    return {
        "mensaje": "¡Rutina activada correctamente! Se mostrará en tu panel principal de entrenamiento.",
        "rutina": activada,
    }


# 4. Obtener todas las rutinas con socio y ejercicios asociados
@router.get("", status_code=status.HTTP_200_OK)
async def get_all_rutinas(
    search: Optional[str] = Query(None),
    id_socio: Optional[str] = Query(None),
    estado: Optional[str] = Query(None),
    current_user: Optional[Dict[str, Any]] = Depends(get_optional_user),
):
    """Obtener todas las rutinas con socio y ejercicios asociados enriquecidos."""
    # Si el usuario es socio y no especifica id_socio, filtrar automáticamente por su socio
    if not id_socio and current_user and current_user.get("rol") == "socio" and current_user.get("id_socio"):
        id_socio = str(current_user["id_socio"])

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
            id_gimnasio,
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
                    dificultad,
                    descripcion,
                    id_gimnasio,
                    id_socio
                )
            )
        """)
        .order("id_rutina", desc=True)
    )

    if id_socio and id_socio.lower() != "todos":
        try:
            query = query.eq("id_socio", int(id_socio))
        except ValueError:
            pass

    if estado and estado.lower() != "todos":
        query = query.eq("estado", estado)

    if search and search.strip():
        query = query.ilike("nombre", f"%{search.strip()}%")

    res = query.execute()
    rutinas_raw = res.data or []

    # Enriquecer y ordenar ejercicios de cada rutina
    rutinas_formateadas = []
    for r in rutinas_raw:
        re_list = r.get("rutina_ejercicio")
        ejercicios = enriquecer_ejercicios_rutina(re_list)
        ejercicios.sort(key=lambda x: x.get("orden") or 0)
        rutinas_formateadas.append({
            **r,
            "rutina_ejercicio": ejercicios,
        })

    # Ordenar para que la rutina activa aparezca en primer lugar
    rutinas_formateadas.sort(
        key=lambda x: (
            0 if x.get("estado") == "activa" else 1,
            -(x.get("id_rutina") or 0)
        )
    )

    return rutinas_formateadas


# 5. Obtener rutina por ID junto a sus ejercicios completos
@router.get("/{id}", status_code=status.HTTP_200_OK)
async def get_rutina_by_id(id: int = Path(...)):
    """Obtener detalle de rutina por ID enriquecido con sus ejercicios."""
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
            id_gimnasio,
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
                    dificultad,
                    descripcion,
                    id_gimnasio,
                    id_socio
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
            detail=f"Rutina con ID {id} no encontrada.",
        )

    data["rutina_ejercicio"] = enriquecer_ejercicios_rutina(data.get("rutina_ejercicio"))
    data["rutina_ejercicio"].sort(key=lambda x: x.get("orden") or 0)

    return data


# 6. Crear una nueva rutina
@router.post("", status_code=status.HTTP_201_CREATED)
async def create_rutina(
    req: RutinaCreate,
    current_user: Optional[Dict[str, Any]] = Depends(get_optional_user),
):
    """Crear una nueva rutina."""
    if not req.nombre or not req.nombre.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre de la rutina es obligatorio.")

    socio_asignado = req.id_socio
    if not socio_asignado and current_user and current_user.get("rol") == "socio" and current_user.get("id_socio"):
        socio_asignado = current_user["id_socio"]

    gym_id = current_user.get("id_gimnasio") if current_user else None
    if socio_asignado and not gym_id:
        res_s = (
            supabase.from_("socio")
            .select("id_gimnasio")
            .eq("id_socio", socio_asignado)
            .maybe_single()
            .execute()
        )
        s_data = get_data(res_s)
        if s_data:
            gym_id = s_data.get("id_gimnasio")

    desc_val = req.descripcion
    if isinstance(desc_val, (dict, list)):
        desc_val = json.dumps(desc_val)
    elif desc_val:
        desc_val = str(desc_val).strip()

    nueva_rutina = {
        "nombre": req.nombre.strip(),
        "descripcion": desc_val,
        "id_socio": socio_asignado,
        "id_gimnasio": gym_id,
        "fecha_inicio": req.fecha_inicio or date.today().isoformat(),
        "fecha_fin": req.fecha_fin or None,
        "estado": req.estado or "asignada",
    }

    res = supabase.from_("rutina").insert([nueva_rutina]).execute()
    if not res.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al crear rutina.")

    rutina_creada = res.data[0]
    rutina_creada["rutina_ejercicio"] = []
    return rutina_creada


# 7. Actualizar rutina existente
@router.put("/{id}", status_code=status.HTTP_200_OK)
async def update_rutina(
    id: int = Path(...),
    req: RutinaUpdate = ...,
):
    """Actualizar rutina existente."""
    update_data = {}
    if req.nombre is not None:
        update_data["nombre"] = req.nombre.strip()
    if req.descripcion is not None:
        if isinstance(req.descripcion, (dict, list)):
            update_data["descripcion"] = json.dumps(req.descripcion)
        else:
            update_data["descripcion"] = str(req.descripcion).strip() if req.descripcion else None
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


# 8. Eliminar rutina y limpiar dependencias
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


# 9. Endpoints de relación con Ejercicios

@router.get("/{id}/ejercicios", status_code=status.HTTP_200_OK)
async def get_ejercicios_by_rutina(id: int = Path(...)):
    """Consultar los ejercicios pertenecientes a una rutina enriquecidos."""
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
                dificultad,
                descripcion,
                id_gimnasio,
                id_socio
            )
        """)
        .eq("id_rutina", id)
        .order("orden", desc=False)
        .execute()
    )

    data = res.data or []
    return enriquecer_ejercicios_rutina(data)


@router.post("/{id}/ejercicios", status_code=status.HTTP_201_CREATED)
async def add_ejercicio_to_rutina(
    id: int = Path(...),
    req: RutinaEjercicioAdd = ...,
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
            detail="Error al asociar ejercicio a la rutina. Verifica que existan ambos IDs.",
        )

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
                dificultad,
                descripcion,
                id_gimnasio,
                id_socio
            )
        """)
        .eq("id_rutina_ejercicio", id_rel)
        .single()
        .execute()
    )

    item = res_full.data
    enriquecido = enriquecer_ejercicios_rutina([item])
    return enriquecido[0] if enriquecido else item


@router.put("/{id}/ejercicios/{ejercicio_id}", status_code=status.HTTP_200_OK)
async def update_ejercicio_en_rutina(
    id: int = Path(...),
    ejercicio_id: int = Path(...),
    req: RutinaEjercicioUpdate = ...,
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
    ejercicio_id: int = Path(...),
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

    return {"message": "Ejercicio desvinculado con éxito de la rutina."}
