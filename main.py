"""
Punto de entrada: inicia el servidor FastAPI con Uvicorn.
Accede a la interfaz en http://localhost:8000
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Cargar .env desde el directorio del proyecto
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

import uvicorn

if __name__ == "__main__":
    print("=" * 60)
    print("  WEATHERBOT POLYMARKET")
    print("  Interfaz: http://localhost:8000")
    print("=" * 60)
    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="warning",
    )
