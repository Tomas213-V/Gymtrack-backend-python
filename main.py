# ==============================================================================
# SERVIDOR PRINCIPAL DE GYMTRACK (FASTAPI + PYTHON)
# ==============================================================================

from datetime import datetime, timezone
import uvicorn
from fastapi import FastAPI, status, Response, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import PORT, HOST, supabase
from app.routers import (
    auth,
    gimnasios,
    socios,
    planes_membresia,
    membresias,
    pagos,
    ejercicios,
    rutinas,
    asistencias,
    dashboard_socio,
)

# 1. INICIALIZACIÓN DE LA APLICACIÓN FASTAPI
app = FastAPI(
    title="GymTrack API",
    version="1.0.0",
    description="API REST Backend en Python (FastAPI) para el sistema de gestión de gimnasios GymTrack.",
    docs_url="/docs",
    redoc_url="/redoc",
)

# 2. MIDDLEWARES GLOBALES (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": detail,
            "error": detail if isinstance(detail, str) else str(detail),
        },
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    first_msg = "Error de validación en la solicitud."
    if exc.errors():
        first_msg = exc.errors()[0].get("msg", first_msg)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": exc.errors(),
            "error": first_msg,
        },
    )

# 3. RUTAS DE PRUEBA Y SALUD

@app.get("/", tags=["General"])
async def root():
    """Ruta raíz de comprobación básica."""
    return {"message": "API de GymTrack funcionando"}


@app.get("/api/health", tags=["General"])
async def health_check():
    """Endpoint para verificar el estado del servidor y la conexión a Supabase."""
    try:
        if not supabase:
            raise Exception("Cliente de Supabase no configurado.")

        # Llamada ligera a Supabase para certificar credenciales y comunicación
        session = supabase.auth.get_session()

        return {
            "status": "ok",
            "database": "Supabase conectado correctamente",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as err:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status": "error",
                "database": "Error al conectar con Supabase",
                "message": str(err),
            },
        )


# 4. REGISTRAR LAS RUTAS DE LA API
app.include_router(auth.router)
app.include_router(gimnasios.router)
app.include_router(socios.router)
app.include_router(planes_membresia.router)
app.include_router(membresias.router)
app.include_router(pagos.router)
app.include_router(ejercicios.router)
app.include_router(rutinas.router)
app.include_router(asistencias.router)
app.include_router(dashboard_socio.router)


# 5. PUESTA EN MARCHA DEL SERVIDOR
if __name__ == "__main__":
    print(f"Iniciando GymTrack API en http://{HOST}:{PORT}")
    print(f"Documentación Swagger disponible en: http://localhost:{PORT}/docs")
    print(f"Documentación ReDoc disponible en: http://localhost:{PORT}/redoc")
    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
