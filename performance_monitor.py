"""
Performance Monitor — monitoreo de recursos del sistema y tiempos de ejecucion.

Fase: snapshot en tiempo real cuando ocurre un WARN:
  - Que operacion estaba activa (timer activo)
  - Top de memoria por modulo (tracemalloc)
  - Threads activos del proceso
  - I/O del proceso (bytes leidos/escritos)
  - Conexiones de red abiertas
  - Historial de las ultimas 5 operaciones antes del evento
"""
import time
import threading
import tracemalloc
from collections import deque, defaultdict
from datetime import datetime, timezone
from typing import Optional
import os

try:
    import psutil
    _PSUTIL_OK = True
except ImportError:
    _PSUTIL_OK = False

# tracemalloc solo si se pide explícitamente (tiene overhead significativo)
import os as _os
_TRACEMALLOC_ENABLED = _os.getenv("TRACEMALLOC_ENABLED", "false").lower() == "true"
if _TRACEMALLOC_ENABLED:
    tracemalloc.start(10)

# Numero de muestras historicas
_HISTORY_LEN   = 60
_TIMING_LEN    = 100
_RESLOG_LEN    = 500

# Umbrales de alerta
_SLOW_MS       = 3000
_CPU_SPIKE_PCT = 75.0
_RAM_SPIKE_MB  = 15.0

# Categorias de componentes
_COMPONENT_CATEGORY = {
    "fetch_weather_markets":  "API",
    "ensemble_forecast":      "FORECAST",
    "live_price":             "API",
    "claude_analysis":        "AI",
    "noaa_forecast":          "API",
    "openmeteo_forecast":     "API",
    "kalman_correction":      "COMPUTE",
    "btc_price":              "API",
    "portfolio_analysis":     "AI",
    "scan_cycle_total":       "CYCLE",
}

def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


class PerformanceMonitor:

    def __init__(self):
        self._lock      = threading.Lock()
        self._process   = psutil.Process(os.getpid()) if _PSUTIL_OK else None

        # Historial CPU/RAM
        self._cpu_history:    deque[float] = deque(maxlen=_HISTORY_LEN)
        self._ram_mb_history: deque[float] = deque(maxlen=_HISTORY_LEN)
        self._ram_pct_history:deque[float] = deque(maxlen=_HISTORY_LEN)
        self._ts_history:     deque[float] = deque(maxlen=_HISTORY_LEN)

        # Timings por componente
        self._timings:     dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=_TIMING_LEN))
        self._call_counts: dict[str, int]           = defaultdict(int)
        self._error_counts:dict[str, int]           = defaultdict(int)

        # Operaciones activas en este momento (nombre → timestamp inicio)
        self._active_ops: dict[str, float] = {}

        # Historial reciente de operaciones completadas (para mostrar "que paso antes")
        self._recent_ops: deque[dict] = deque(maxlen=10)

        # Metricas del ciclo del bot
        self._scan_count     = 0
        self._markets_total  = 0
        self._opps_found     = 0
        self._trades_eval    = 0
        self._last_scan_ms   = 0.0
        self._last_scan_ts   = 0.0

        # Resource log
        self._resource_log: deque[dict] = deque(maxlen=_RESLOG_LEN)

        self._last_ram_mb   = 0.0
        self._sample_count  = 0

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.start()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._sampler_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    # ── Snapshot del sistema ──────────────────────────────────────────────────

    def _capture_snapshot(self) -> dict:
        """
        Captura estado completo del proceso en este instante:
        memoria por modulo, threads, I/O, conexiones, ops activas.
        """
        snap = {}

        if _PSUTIL_OK and self._process:
            try:
                # CPU e I/O
                snap["cpu_pct"]  = self._process.cpu_percent(interval=None)
                snap["ram_mb"]   = round(self._process.memory_info().rss / 1024 / 1024, 1)
                snap["threads"]  = self._process.num_threads()

                io = self._process.io_counters()
                snap["io_read_mb"]  = round(io.read_bytes  / 1024 / 1024, 1)
                snap["io_write_mb"] = round(io.write_bytes / 1024 / 1024, 1)

                # Conexiones de red activas
                try:
                    conns = self._process.net_connections()
                    snap["net_connections"] = len(conns)
                    snap["net_established"] = sum(1 for c in conns if getattr(c.status, 'name', c.status) in ('ESTABLISHED', 'established'))
                except Exception:
                    snap["net_connections"] = 0
                    snap["net_established"] = 0

                # Sistema
                vm = psutil.virtual_memory()
                snap["sys_ram_pct"] = vm.percent
                snap["sys_cpu_pct"] = psutil.cpu_percent(interval=None)

            except Exception as e:
                snap["psutil_error"] = str(e)

        # Top de memoria por archivo (tracemalloc) — solo si está habilitado
        if _TRACEMALLOC_ENABLED:
            try:
                tm_snapshot = tracemalloc.take_snapshot()
                stats = tm_snapshot.statistics("filename")[:8]
                snap["top_memory"] = [
                    {
                        "file":  s.traceback[0].filename.replace("\\", "/").split("/")[-1] if s.traceback else "?",
                        "kb":    round(s.size / 1024, 1),
                        "count": s.count,
                    }
                    for s in stats
                ]
            except Exception:
                snap["top_memory"] = []
        else:
            snap["top_memory"] = []

        # Operaciones activas ahora mismo
        with self._lock:
            now = time.perf_counter()
            active = {
                name: round((now - t0) * 1000, 0)
                for name, t0 in self._active_ops.items()
            }
            recent = list(self._recent_ops)

        snap["active_ops"]  = active   # {nombre: ms_transcurridos}
        snap["recent_ops"]  = recent   # ultimas N completadas

        return snap

    # ── Resource log ─────────────────────────────────────────────────────────

    def _rlog(self, level: str, category: str, msg: str, detail: Optional[dict] = None):
        entry = {
            "time":     _now_str(),
            "level":    level,
            "category": category,
            "msg":      msg,
            "detail":   detail or {},
        }
        with self._lock:
            self._resource_log.append(entry)

    def _rlog_warn_with_snapshot(self, category: str, msg: str, base_detail: dict):
        """Emite un WARN enriquecido con snapshot completo del sistema."""
        snap = self._capture_snapshot()
        detail = {**base_detail, "snapshot": snap}
        self._rlog("WARN", category, msg, detail)

    def get_resource_log(self, limit: int = 200) -> list[dict]:
        with self._lock:
            entries = list(self._resource_log)
        return entries[-limit:]

    # ── Sampler (1/seg) ───────────────────────────────────────────────────────

    def _sampler_loop(self):
        while self._running:
            try:
                cpu_pct, ram_mb, ram_pct = self._read_resources()
                ts = time.time()

                prev_ram  = self._last_ram_mb
                ram_delta = ram_mb - prev_ram if prev_ram > 0 else 0.0

                with self._lock:
                    self._cpu_history.append(cpu_pct)
                    self._ram_mb_history.append(ram_mb)
                    self._ram_pct_history.append(ram_pct)
                    self._ts_history.append(ts)
                    self._last_ram_mb = ram_mb
                    self._sample_count += 1
                    cpu_hist_snap = list(self._cpu_history)
                    ram_hist_snap = list(self._ram_mb_history)

                if cpu_pct >= _CPU_SPIKE_PCT:
                    cpu_avg = round(sum(cpu_hist_snap) / len(cpu_hist_snap), 1) if cpu_hist_snap else 0.0
                    self._rlog_warn_with_snapshot("CPU",
                        f"CPU spike: {cpu_pct:.1f}% (proceso)",
                        {
                            "cpu_pct":    round(cpu_pct, 1),
                            "cpu_avg_1m": cpu_avg,
                            "ram_mb":     round(ram_mb, 1),
                        })

                if ram_delta >= _RAM_SPIKE_MB:
                    ram_avg = round(sum(ram_hist_snap) / len(ram_hist_snap), 1) if ram_hist_snap else 0.0
                    self._rlog_warn_with_snapshot("MEMORY",
                        f"RAM +{ram_delta:.1f} MB → {ram_mb:.1f} MB total",
                        {
                            "ram_mb":     round(ram_mb, 1),
                            "delta_mb":   round(ram_delta, 1),
                            "ram_avg_1m": ram_avg,
                        })

            except Exception:
                pass
            time.sleep(5.0)  # 5s en vez de 1s: reduce CPU overhead del monitor ~80%

    def _read_resources(self) -> tuple[float, float, float]:
        if not _PSUTIL_OK or self._process is None:
            return 0.0, 0.0, 0.0
        try:
            cpu    = self._process.cpu_percent(interval=None)
            mem    = self._process.memory_info()
            ram_mb = mem.rss / 1024 / 1024
            vm     = psutil.virtual_memory()
            return cpu, ram_mb, vm.percent
        except Exception:
            return 0.0, 0.0, 0.0

    # ── Timer ─────────────────────────────────────────────────────────────────

    class _Timer:
        def __init__(self, monitor: "PerformanceMonitor", name: str, extra: Optional[dict] = None):
            self._mon   = monitor
            self._name  = name
            self._extra = extra or {}
            self._t0    = 0.0

        def __enter__(self):
            self._t0 = time.perf_counter()
            with self._mon._lock:
                self._mon._active_ops[self._name] = self._t0
            return self

        def __exit__(self, exc_type, *_):
            elapsed_ms = (time.perf_counter() - self._t0) * 1000
            error      = exc_type is not None

            with self._mon._lock:
                self._mon._active_ops.pop(self._name, None)
                history = list(self._mon._timings[self._name])
                self._mon._timings[self._name].append(elapsed_ms)
                self._mon._call_counts[self._name] += 1
                if error:
                    self._mon._error_counts[self._name] += 1
                total_calls  = self._mon._call_counts[self._name]
                total_errors = self._mon._error_counts[self._name]

            # Guardar en recientes
            self._mon._recent_ops.append({
                "name":    self._name,
                "ms":      round(elapsed_ms, 1),
                "error":   error,
                "time":    _now_str(),
            })

            avg_ms       = round(sum(history) / len(history), 1) if history else None
            min_ms       = round(min(history), 1) if history else None
            max_ms       = round(max(history), 1) if history else None
            times_slower = round(elapsed_ms / avg_ms, 1) if avg_ms and avg_ms > 0 else None

            category = _COMPONENT_CATEGORY.get(self._name, "OTHER")
            base_detail = {
                "ms":           round(elapsed_ms, 1),
                "avg_ms":       avg_ms,
                "min_ms":       min_ms,
                "max_ms":       max_ms,
                "times_slower": times_slower,
                "total_calls":  total_calls,
                "total_errors": total_errors,
                "component":    self._name,
                **self._extra,
            }

            if error:
                self._mon._rlog_warn_with_snapshot(category,
                    f"{self._name} FALLÓ ({elapsed_ms:.0f} ms)", base_detail)
            elif elapsed_ms >= _SLOW_MS:
                self._mon._rlog_warn_with_snapshot(category,
                    f"{self._name} lento: {elapsed_ms:.0f} ms", base_detail)
            else:
                self._mon._rlog("INFO", category,
                    f"{self._name}: {elapsed_ms:.0f} ms", base_detail)

    def timer(self, name: str, **extra) -> "_Timer":
        return self._Timer(self, name, extra)

    def record_time(self, name: str, elapsed_ms: float, error: bool = False, **extra):
        with self._lock:
            history = list(self._timings[name])
            self._timings[name].append(elapsed_ms)
            self._call_counts[name] += 1
            if error:
                self._error_counts[name] += 1
            total_calls  = self._call_counts[name]
            total_errors = self._error_counts[name]

        avg_ms       = round(sum(history) / len(history), 1) if history else None
        times_slower = round(elapsed_ms / avg_ms, 1) if avg_ms and avg_ms > 0 else None
        category     = _COMPONENT_CATEGORY.get(name, "OTHER")
        base_detail  = {
            "ms": round(elapsed_ms, 1), "avg_ms": avg_ms,
            "times_slower": times_slower, "total_calls": total_calls,
            "total_errors": total_errors, "component": name, **extra,
        }

        if error or elapsed_ms >= _SLOW_MS:
            self._rlog_warn_with_snapshot(category,
                f"{name} {'FALLÓ' if error else 'lento'}: {elapsed_ms:.0f} ms", base_detail)
        else:
            self._rlog("INFO", category, f"{name}: {elapsed_ms:.0f} ms", base_detail)

    # ── Contadores del bot ────────────────────────────────────────────────────

    def record_scan(self, markets_analyzed: int, opps_found: int,
                    trades_evaluated: int, scan_ms: float):
        with self._lock:
            self._scan_count    += 1
            self._markets_total += markets_analyzed
            self._opps_found    += opps_found
            self._trades_eval   += trades_evaluated
            self._last_scan_ms   = scan_ms
            self._last_scan_ts   = time.time()

        _, ram_mb, _ = self._read_resources()
        level = "WARN" if scan_ms >= _SLOW_MS else "INFO"
        detail = {
            "scan_ms":  round(scan_ms, 1),
            "markets":  markets_analyzed,
            "opps":     opps_found,
            "ram_mb":   round(ram_mb, 1),
            "scan_n":   self._scan_count,
            "component":"scan_cycle_total",
        }
        if level == "WARN":
            self._rlog_warn_with_snapshot("CYCLE",
                f"Ciclo #{self._scan_count}: {scan_ms:.0f} ms | "
                f"{markets_analyzed} mkt | {opps_found} opps | RAM {ram_mb:.0f} MB",
                detail)
        else:
            self._rlog("INFO", "CYCLE",
                f"Ciclo #{self._scan_count}: {scan_ms:.0f} ms | "
                f"{markets_analyzed} mkt | {opps_found} opps | RAM {ram_mb:.0f} MB",
                detail)

    # ── Stats ─────────────────────────────────────────────────────────────────

    def _timing_stats(self, name: str) -> dict:
        data = list(self._timings.get(name, []))
        if not data:
            return {"avg_ms": None, "min_ms": None, "max_ms": None,
                    "last_ms": None, "calls": 0, "errors": 0, "p95_ms": None}
        return {
            "avg_ms":  round(sum(data) / len(data), 1),
            "min_ms":  round(min(data), 1),
            "max_ms":  round(max(data), 1),
            "last_ms": round(data[-1], 1),
            "calls":   self._call_counts.get(name, 0),
            "errors":  self._error_counts.get(name, 0),
            "p95_ms":  round(sorted(data)[int(len(data) * 0.95)], 1) if len(data) >= 5 else None,
        }

    def get_stats(self) -> dict:
        with self._lock:
            cpu_hist  = list(self._cpu_history)
            ram_hist  = list(self._ram_mb_history)
            ramp_hist = list(self._ram_pct_history)
            ts_hist   = list(self._ts_history)
            all_names = list(self._timings.keys())
            timings   = {n: self._timing_stats(n) for n in all_names}

        cpu_now  = cpu_hist[-1]  if cpu_hist  else 0.0
        ram_now  = ram_hist[-1]  if ram_hist  else 0.0
        cpu_avg  = round(sum(cpu_hist) / len(cpu_hist), 1) if cpu_hist else 0.0
        ram_avg  = round(sum(ram_hist) / len(ram_hist), 1) if ram_hist else 0.0

        sys_cpu = sys_ram_total = sys_ram_used = sys_ram_pct = 0.0
        if _PSUTIL_OK:
            try:
                sys_cpu = psutil.cpu_percent(interval=None)
                vm = psutil.virtual_memory()
                sys_ram_total = round(vm.total / 1024 / 1024, 1)
                sys_ram_used  = round(vm.used  / 1024 / 1024, 1)
                sys_ram_pct   = vm.percent
            except Exception:
                pass

        return {
            "process": {
                "cpu_pct":    round(cpu_now, 1),
                "cpu_avg_1m": cpu_avg,
                "ram_mb":     round(ram_now, 1),
                "ram_avg_1m": ram_avg,
                "psutil_ok":  _PSUTIL_OK,
            },
            "system": {
                "cpu_pct":      round(sys_cpu, 1),
                "ram_total_mb": sys_ram_total,
                "ram_used_mb":  sys_ram_used,
                "ram_pct":      sys_ram_pct,
            },
            "history": {
                "cpu":     [round(x, 1) for x in cpu_hist],
                "ram_mb":  [round(x, 1) for x in ram_hist],
                "ram_pct": [round(x, 1) for x in ramp_hist],
                "ts":      [round(x, 3) for x in ts_hist],
                "len":     len(cpu_hist),
            },
            "timings": timings,
            "bot": {
                "scan_count":       self._scan_count,
                "markets_total":    self._markets_total,
                "opps_found":       self._opps_found,
                "trades_eval":      self._trades_eval,
                "last_scan_ms":     round(self._last_scan_ms, 1),
                "last_scan_ts":     self._last_scan_ts,
                "markets_per_scan": round(self._markets_total / max(self._scan_count, 1), 1),
            },
            "sample_count": self._sample_count,
        }

    def get_current_resources(self) -> dict:
        cpu, ram_mb, _ = self._read_resources()
        return {"cpu_pct": round(cpu, 1), "ram_mb": round(ram_mb, 1)}


# Instancia global
perf = PerformanceMonitor()
