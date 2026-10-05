# 🏋️ GymTrack Backend (Python / FastAPI)

Backend RESTful para **GymTrack**, migrado a **Python** utilizando **FastAPI**, **Pydantic** y el cliente oficial de **Supabase**.

---

## 🚀 Características Principales

- **Alto Rendimiento y Asincronía**: Desarrollado con FastAPI y ASGI (Uvicorn).
- **Validación Automática y Tipado**: Modelos robustos con Pydantic v2.
- **Autenticación JWT y Roles**: Middleware y dependencias para roles (`dueño`, `administrador`, `socio`).
- **Seguridad**: Hash de contraseñas con `bcrypt` (10 rounds) y compatibilidad retroactiva.
- **Documentación Interactiva Automática**:
  - **Swagger UI**: [http://localhost:3000/docs](http://localhost:3000/docs)
  - **ReDoc**: [http://localhost:3000/redoc](http://localhost:3000/redoc)
- **Base de Datos en Tiempo Real**: Integración completa con **Supabase** (PostgreSQL).
- **Pasarelas de Pago**: Integración para **Mercado Pago** y **Ualá Bis** con modo Sandbox y producción.

---

## 📁 Estructura del Proyecto

```text
Gymtrack-backend-python/
├── app/
│   ├── config.py                 # Configuración de entorno y cliente Supabase
│   ├── core/
│   │   ├── dependencies.py       # Inyección de dependencias (JWT, roles)
│   │   └── security.py           # Bcrypt y utilidades de tokens JWT
│   ├── models/                   # Schemas Pydantic
│   │   ├── asistencia.py
│   │   ├── auth.py
│   │   ├── dashboard_socio.py
│   │   ├── ejercicio.py
│   │   ├── gimnasio.py
│   │   ├── membresia.py
│   │   ├── pago.py
│   │   ├── plan_membresia.py
│   │   ├── rutina.py
│   │   └── socio.py
│   ├── routers/                  # Controladores y Endpoints de la API
│   │   ├── asistencias.py        # /api/asistencias
│   │   ├── auth.py               # /api/auth
│   │   ├── dashboard_socio.py    # /api/socio
│   │   ├── ejercicios.py         # /api/ejercicios
│   │   ├── gimnasios.py          # /api/gimnasios
│   │   ├── membresias.py         # /api/membresias
│   │   ├── pagos.py              # /api/pagos
│   │   ├── planes_membresia.py   # /api/planes-membresia
│   │   ├── rutinas.py            # /api/rutinas
│   │   └── socios.py             # /api/socios
│   └── services/
│       ├── payment_service.py    # Pagos Mercado Pago y Ualá
│       └── utils.py              # Formateadores de moneda ARS, fechas y métricas
├── .env                          # Variables de entorno locales
├── .env.example                  # Plantilla de variables de entorno
├── .gitignore                    # Reglas de exclusión para Git
├── main.py                       # Punto de entrada de la aplicación FastAPI
├── requirements.txt              # Dependencias de Python
└── README.md                     # Documentación general
```

---

## ⚙️ Requisitos Previos

- **Python 3.10+** (probado con Python 3.11)
- Proyecto activo en **Supabase**

---

## 🛠️ Instalación y Puesta en Marcha

### 1. Clonar el repositorio y navegar a la carpeta
```bash
git clone https://github.com/Tomas213-V/Gymtrack-backend-python.git
cd Gymtrack-backend-python
```

### 2. Crear y activar el entorno virtual
En Windows (PowerShell):
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```
En Linux / macOS:
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Instalar las dependencias
```bash
pip install -r requirements.txt
```

### 4. Configurar variables de entorno
Copia el archivo `.env.example` a `.env` si aún no lo tienes:
```bash
cp .env.example .env
```
Verifica que los valores de `SUPABASE_URL` y `SUPABASE_KEY` sean los correctos.

### 5. Iniciar el servidor
```bash
python main.py
```
O con Uvicorn directamente con autoreload:
```bash
uvicorn main:app --reload --port 3000
```

El servidor estará corriendo en: **http://localhost:3000**  
Documentación interactiva disponible en: **http://localhost:3000/docs**

---

## 📡 Endpoints de la API

| Módulo | Prefijo | Descripción |
| :--- | :--- | :--- |
| **Salud** | `GET /api/health` | Estado del backend y conexión con Supabase |
| **Autenticación** | `/api/auth` | Registro de dueño, login general, login socio, perfil (`/me`) |
| **Gimnasios** | `/api/gimnasios` | Crear gimnasio, obtener mi gimnasio |
| **Socios** | `/api/socios` | CRUD completo, paginación, filtros, perfil y cambio de estado |
| **Planes** | `/api/planes-membresia` | Gestión de planes de membresía |
| **Membresías** | `/api/membresias` | Asignación y extensión de membresías de socios |
| **Pagos** | `/api/pagos` | Registro, métricas, resumen de recaudación y estados |
| **Ejercicios** | `/api/ejercicios` | Catálogo de ejercicios y filtrado por grupo muscular |
| **Rutinas** | `/api/rutinas` | Gestión de rutinas y asociación de ejercicios |
| **Asistencias** | `/api/asistencias` | Registro de ingreso, salida (checkout) y estadísticas |
| **Panel Socio** | `/api/socio` | Dashboard del socio, check-in, progreso y pasarelas de pago |
