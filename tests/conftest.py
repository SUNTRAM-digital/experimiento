"""
Configuracion de pytest para el proyecto WeatherBot.
Registra marks personalizados para evitar warnings.
"""
import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: tests que requieren conexion a internet y APIs externas"
    )
