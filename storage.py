"""
Capa de abstracción de datos (Data Access Layer).

BACKEND ACTUAL: JSON files en data/
PRÓXIMO PASO:   SQLite  → cambiar BACKEND = "sqlite"
MÁS ADELANTE:  PostgreSQL → cambiar BACKEND = "postgres"

Para migrar: solo cambiar la clase _JsonBackend por _SqliteBackend o
_PostgresBackend. El resto del código no cambia.

Esquema de colecciones (futuras tablas):
────────────────────────────────────────────────────────────────────
  state              → clave-valor del estado global del bot
  params             → parámetros de configuración
  trades_phantom     → trades phantom VPS (vps_phantom_experiment)
  trades_real        → trades reales ejecutados (updown_recent_trades)
  learner_phantom    → stats aprendidas del phantom (phantom_learner)
  learner_real       → stats adaptativas de trades reales (updown_learner)
  chats              → historial de conversaciones Claude
  logs               → logs del bot (eventos importantes)
  risk_state         → estado del circuit breaker / risk manager
  wallets            → smart wallets Telonex
  category_stats     → stats por categoría de mercado de clima
  strategy_notes     → notas del analista Claude
────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger("weatherbot.storage")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# ── Backend actual: JSON ───────────────────────────────────────────────────────

class _JsonBackend:
    """
    Implementación JSON del storage.

    Cada 'colección' es un archivo JSON en data/{collection}.json
    Para estructuras simples (clave-valor o lista de registros).

    Reemplazar esta clase por _SqliteBackend o _PostgresBackend cuando
    sea el momento de migrar. La interfaz pública no cambia.
    """

    def __init__(self, data_dir: str) -> None:
        self._dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

    def _path(self, name: str) -> str:
        return os.path.join(self._dir, f"{name}.json")

    # ── Operaciones de documento completo (backward-compat) ───────────────────

    def load_doc(self, name: str, default: Any = None) -> Any:
        """
        Carga un documento completo desde data/{name}.json.
        Equivalente directo de: json.load(open(file))
        Usar en código que todavía maneja la estructura completa.
        """
        path = self._path(name)
        if not os.path.exists(path):
            return default if default is not None else {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[Storage] load_doc '{name}' error: {e}")
            return default if default is not None else {}

    def save_doc(self, name: str, data: Any) -> None:
        """
        Guarda un documento completo en data/{name}.json.
        Equivalente directo de: json.dump(data, open(file, 'w'))
        """
        path = self._path(name)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[Storage] save_doc '{name}' error: {e}")

    # ── Operaciones orientadas a registros (futuras filas de tabla) ───────────

    def get_record(self, collection: str, key: str,
                   default: Optional[dict] = None) -> Optional[dict]:
        """
        Obtiene un registro por clave de un documento dict-like.
        En SQLite: SELECT * FROM {collection} WHERE id = {key}
        """
        doc = self.load_doc(collection, {})
        if isinstance(doc, dict):
            return doc.get(key, default)
        return default

    def set_record(self, collection: str, key: str, record: dict) -> None:
        """
        Inserta o actualiza un registro.
        En SQLite: INSERT OR REPLACE INTO {collection} VALUES (...)
        """
        doc = self.load_doc(collection, {})
        if not isinstance(doc, dict):
            doc = {}
        doc[key] = record
        self.save_doc(collection, doc)

    def append_record(self, collection: str, record: dict) -> None:
        """
        Añade un registro a una colección tipo lista.
        En SQLite: INSERT INTO {collection} VALUES (...)
        """
        doc = self.load_doc(collection, [])
        if not isinstance(doc, list):
            doc = []
        doc.append(record)
        self.save_doc(collection, doc)

    def list_records(self, collection: str, **filters) -> list[dict]:
        """
        Retorna todos los registros de una colección, con filtros opcionales.
        En SQLite: SELECT * FROM {collection} WHERE k=v AND ...
        Solo funciona con colecciones tipo lista[dict].
        """
        doc = self.load_doc(collection, [])
        if not isinstance(doc, list):
            return []
        if not filters:
            return doc
        return [
            r for r in doc
            if all(r.get(k) == v for k, v in filters.items())
        ]

    def count_records(self, collection: str, **filters) -> int:
        """
        Cuenta registros con filtros opcionales.
        En SQLite: SELECT COUNT(*) FROM {collection} WHERE ...
        """
        return len(self.list_records(collection, **filters))

    def update_records(self, collection: str, match: dict, update: dict) -> int:
        """
        Actualiza campos en todos los registros que coincidan con 'match'.
        En SQLite: UPDATE {collection} SET k=v WHERE match_k=match_v
        Retorna el número de registros actualizados.
        """
        doc = self.load_doc(collection, [])
        if not isinstance(doc, list):
            return 0
        updated = 0
        for r in doc:
            if all(r.get(k) == v for k, v in match.items()):
                r.update(update)
                updated += 1
        if updated:
            self.save_doc(collection, doc)
        return updated

    def delete_records(self, collection: str, **filters) -> int:
        """
        Elimina registros que coincidan con los filtros.
        En SQLite: DELETE FROM {collection} WHERE k=v
        Retorna el número de registros eliminados.
        """
        doc = self.load_doc(collection, [])
        if not isinstance(doc, list):
            return 0
        original_len = len(doc)
        doc = [r for r in doc if not all(r.get(k) == v for k, v in filters.items())]
        if len(doc) != original_len:
            self.save_doc(collection, doc)
        return original_len - len(doc)

    # ── Info y diagnóstico ────────────────────────────────────────────────────

    def collection_exists(self, name: str) -> bool:
        return os.path.exists(self._path(name))

    def backend_info(self) -> dict:
        return {
            "backend": "json",
            "data_dir": self._dir,
            "collections": [
                f[:-5] for f in os.listdir(self._dir)
                if f.endswith(".json")
            ] if os.path.isdir(self._dir) else [],
        }


# ── Placeholder para SQLite (implementar cuando sea el momento) ───────────────

class _SqliteBackend:
    """
    TODO: Implementar cuando sea el momento de migrar.

    Uso previsto:
        import sqlite3
        self._conn = sqlite3.connect("data/weatherbot.db")

    Cada método debe replicar la interfaz de _JsonBackend usando SQL.
    La migración de datos existentes se hace con migrate_json_to_sqlite()
    definida al final de este archivo.
    """
    def __init__(self, db_path: str) -> None:
        raise NotImplementedError(
            "SQLite backend pendiente de implementación. "
            "Ver docstring de _SqliteBackend para guía de migración."
        )


# ── Instancia global ───────────────────────────────────────────────────────────

# Para cambiar de backend, solo modificar esta línea:
#   _backend = _SqliteBackend("data/weatherbot.db")   ← SQLite
#   _backend = _PostgresBackend(os.environ["DB_URL"]) ← PostgreSQL
_backend = _JsonBackend(DATA_DIR)


class Storage:
    """
    Interfaz pública de acceso a datos.
    Delega todo al backend configurado.

    Ejemplo de uso:
        from storage import storage

        # Cargar documento completo (backward-compat)
        state = storage.load_doc("state")

        # Guardar documento completo
        storage.save_doc("state", state)

        # Operaciones orientadas a registros
        storage.append_record("trades_phantom", {id: 1, result: "WIN"})
        wins = storage.list_records("trades_phantom", result="WIN")
    """

    def __init__(self, backend) -> None:
        self._b = backend

    # Backward-compat (para código existente que ya usa json directamente)
    def load_doc(self, name: str, default: Any = None) -> Any:
        return self._b.load_doc(name, default)

    def save_doc(self, name: str, data: Any) -> None:
        self._b.save_doc(name, data)

    # Operaciones de registros
    def get_record(self, collection: str, key: str,
                   default: Optional[dict] = None) -> Optional[dict]:
        return self._b.get_record(collection, key, default)

    def set_record(self, collection: str, key: str, record: dict) -> None:
        self._b.set_record(collection, key, record)

    def append_record(self, collection: str, record: dict) -> None:
        self._b.append_record(collection, record)

    def list_records(self, collection: str, **filters) -> list[dict]:
        return self._b.list_records(collection, **filters)

    def count_records(self, collection: str, **filters) -> int:
        return self._b.count_records(collection, **filters)

    def update_records(self, collection: str, match: dict, update: dict) -> int:
        return self._b.update_records(collection, match, update)

    def delete_records(self, collection: str, **filters) -> int:
        return self._b.delete_records(collection, **filters)

    def collection_exists(self, name: str) -> bool:
        return self._b.collection_exists(name)

    def backend_info(self) -> dict:
        return self._b.backend_info()


# Singleton — importar así en todos los módulos:
#   from storage import storage
storage = Storage(_backend)


# ── Utilidad de migración futura ──────────────────────────────────────────────

def migrate_json_to_sqlite(sqlite_path: str = "data/weatherbot.db") -> dict:
    """
    Migra todos los archivos JSON actuales a una base de datos SQLite.
    Llamar UNA VEZ al momento de migrar.

    Mapa JSON → tabla SQLite:
        state.json              → tabla 'bot_state'      (clave-valor)
        params.json             → tabla 'params'         (clave-valor)
        vps_phantom_experiment  → tabla 'trades_phantom' (lista de trades)
        updown_stats.json       → tabla 'learner_real'   (clave-valor por intervalo)
        phantom_learner_stats   → tabla 'learner_phantom'(clave-valor por intervalo)
        chats.json              → tabla 'chats'          (lista de conversaciones)
        logs.json               → tabla 'logs'           (lista de eventos)
        risk_state.json         → tabla 'risk_state'     (clave-valor)
        telonex_top_wallets     → tabla 'wallets'        (lista de wallets)
        category_stats.json     → tabla 'category_stats' (clave-valor)
        strategy_notes.json     → tabla 'strategy_notes' (lista)

    Retorna resumen de registros migrados.
    """
    # Implementar cuando sea el momento.
    # El esquema DDL básico para SQLite sería:
    #
    # CREATE TABLE IF NOT EXISTS trades_phantom (
    #     id          INTEGER PRIMARY KEY AUTOINCREMENT,
    #     trade_id    INTEGER,
    #     slug        TEXT UNIQUE,
    #     timestamp   TEXT,
    #     market      TEXT,
    #     signal      TEXT,
    #     confidence_pct  REAL,
    #     confidence_tier TEXT,
    #     result      TEXT,
    #     pnl_vps     REAL,
    #     pnl_fixed   REAL,
    #     ta_scores   TEXT,   -- JSON string
    #     created_at  TEXT DEFAULT CURRENT_TIMESTAMP
    # );
    #
    # CREATE TABLE IF NOT EXISTS kv_store (
    #     collection  TEXT NOT NULL,
    #     key         TEXT NOT NULL,
    #     value       TEXT,  -- JSON string
    #     updated_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    #     PRIMARY KEY (collection, key)
    # );
    raise NotImplementedError(
        "migrate_json_to_sqlite() pendiente de implementación. "
        "Ver comentarios de la función para el esquema DDL."
    )
