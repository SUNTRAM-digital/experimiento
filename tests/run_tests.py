"""
Runner de tests del proyecto WeatherBot.

Uso:
    python tests/run_tests.py              # tests unitarios (rapidos, sin internet)
    python tests/run_tests.py --all        # unitarios + integracion (requiere internet)
    python tests/run_tests.py --unit       # solo unitarios
    python tests/run_tests.py --integration # solo integracion

Equivalente con pytest directamente:
    pytest tests/ -m "not integration" -v          # unitarios
    pytest tests/ -v                               # todos
    pytest tests/test_kalman.py -v                 # un modulo especifico
"""
import sys
import subprocess
import argparse
from pathlib import Path

TESTS_DIR = Path(__file__).parent


def run(args: list[str]) -> int:
    cmd = [sys.executable, "-m", "pytest"] + args
    print(f"\n>> {' '.join(cmd)}\n")
    result = subprocess.run(cmd, cwd=TESTS_DIR.parent)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description="Runner de tests WeatherBot")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all",         action="store_true", help="Todos los tests")
    group.add_argument("--unit",        action="store_true", help="Solo tests unitarios")
    group.add_argument("--integration", action="store_true", help="Solo tests de integracion")
    opts = parser.parse_args()

    if opts.all:
        exit_code = run([str(TESTS_DIR), "-v", "--tb=short"])
    elif opts.integration:
        exit_code = run([str(TESTS_DIR), "-v", "-m", "integration", "--tb=short", "-s"])
    else:
        # Default: solo unitarios (rapidos)
        exit_code = run([str(TESTS_DIR), "-v", "-m", "not integration", "--tb=short"])

    print("\n" + ("PASS" if exit_code == 0 else "FAIL") + f" (codigo: {exit_code})")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
