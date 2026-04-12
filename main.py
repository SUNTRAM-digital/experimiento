"""
Punto de entrada: inicia el servidor FastAPI con Uvicorn.
Accede a la interfaz en http://localhost:8000
"""
import os
import sys
import subprocess
from pathlib import Path
from dotenv import load_dotenv

# Cargar .env desde el directorio del proyecto
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

PORT = 8000


def _free_port(port: int):
    """Mata cualquier proceso que esté usando el puerto antes de arrancar."""
    try:
        result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True)
        pids = set()
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.strip().split()
                if parts:
                    pids.add(parts[-1])
        if not pids:
            return
        current_pid = str(os.getpid())
        for pid in pids:
            if pid == current_pid:
                continue
            r = subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, text=True)
            if "Correcto" in r.stdout or "SUCCESS" in r.stdout.upper():
                print(f"  [cleanup] Proceso previo ({pid}) terminado.")
            # Silenciar error si el proceso ya no existe
    except Exception as e:
        print(f"  [cleanup] No se pudo limpiar el puerto: {e}")


import uvicorn

if __name__ == "__main__":
    print("=" * 60)
    print("  WEATHERBOT POLYMARKET")
    print(f"  Interfaz: http://localhost:{PORT}")
    print("=" * 60)

    _free_port(PORT)

    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=PORT,
        reload=False,
        log_level="warning",
    )
