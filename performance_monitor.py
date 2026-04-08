"""
Performance Monitor — monitoreo de recursos del sistema y tiempos de ejecucion.

Trackea en tiempo real:
  - CPU: porcentaje de uso del proceso y del sistema
  - Memoria: RSS, VMS, porcentaje del sistema
  - Tiempos de ejecucion: por componente (scan, forecast, Claude, Kalman, etc.)
  - Throughput: mercados analizados/minuto, trades evaluados/ciclo
  - Latencia de APIs externas: NOAA, OpenMeteo, Polymarket Gamma
  - Historial de las ultimas N mediciones para graficas

Uso:
  from performance_monitor import perf
  with perf.timer("scan_cycle"):
      ...do work...
  stats = perf.get_stats()
"""
import time
import threading
from collections import deque, defaultdict
from typing import Optional
import os

try:
    import psutil
    _PSUTIL_OK = True
except ImportError:
    _PSUTIL_OK = False


# Numero de muestras historicas a mantener para graficas
_HISTORY_LEN = 60   # ultimos 60 puntos (1 por segundo → 1 minuto)
_TIMING_LEN  = 100  # ultimas 100 ejecuciones por componente


class PerformanceMonitor:
    """
    Monitor centralizado de recursos y tiempos.
    Thread-safe. Instancia global: `perf`.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._process = psutil.Process(os.getpid()) if _PSUTIL_OK else None

        # Historial de muestras de sistema (CPU, RAM) — 1/seg
        self._cpu_history:    deque[float] = deque(maxlen=_HISTORY_LEN)
        self._ram_mb_history: deque[float] = deque(maxlen=_HISTORY_LEN)
        self._ram_pct_history:deque[float] = deque(maxlen=_HISTORY_LEN)
        self._ts_history:     deque[float] = deque(maxlen=_HISTORY_LEN)

        # Tiempos de ejecucion por componente
        self._timings: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=_TIMING_LEN))
        self._call_counts: dict[str, int] = defaultdict(int)
        self._error_counts: dict[str, int] = defaultdict(int)

        # Metricas del ciclo del bot
        self._scan_count     = 0
        self._markets_total  = 0
        self._opps_found     = 0
        self._trades_eval    = 0
        self._last_scan_ms   = 0.0
        self._last_scan_ts   = 0.0

        # Muestras acumuladas para CPU/RAM
        self._sample_count = 0

        # Arrancar el sampler en background
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

    # ── Sampler interno (1 muestra/seg) ───────────────────────────────────────

    def _sampler_loop(self):
        """Toma muestras de CPU y RAM cada segundo en background."""
        while self._running:
            try:
                cpu_pct, ram_mb, ram_pct = self._read_resources()
                ts = time.time()
                with self._lock:
                    self._cpu_history.append(cpu_pct)
                    self._ram_mb_history.append(ram_mb)
                    self._ram_pct_history.append(ram_pct)
                    self._ts_history.append(ts)
                    self._sample_count += 1
            except Exception:
                pass
            time.sleep(1.0)

    def _read_resources(self) -> tuple[float, float, float]:
        """Lee CPU%, RAM MB del proceso, RAM% del sistema."""
        if not _PSUTIL_OK or self._process is None:
            return 0.0, 0.0, 0.0
        try:
            cpu   = self._process.cpu_percent(interval=None)
            mem   = self._process.memory_info()
            ram_mb  = mem.rss / 1024 / 1024
            sys_mem = psutil.virtual_memory()
            ram_pct = sys_mem.percent
            return cpu, ram_mb, ram_pct
        except Exception:
            return 0.0, 0.0, 0.0

    # ── Context manager para medir tiempos ────────────────────────────────────

    class _Timer:
        """Context manager que registra el tiempo de ejecucion."""
        def __init__(self, monitor: "PerformanceMonitor", name: str):
            self._mon  = monitor
            self._name = name
            self._t0   = 0.0

        def __enter__(self):
            self._t0 = time.perf_counter()
            return self

        def __exit__(self, exc_type, *_):
            elapsed_ms = (time.perf_counter() - self._t0) * 1000
            with self._mon._lock:
                self._mon._timings[self._name].append(elapsed_ms)
                self._mon._call_counts[self._name] += 1
                if exc_type is not None:
                    self._mon._error_counts[self._name] += 1

    def timer(self, name: str) -> "_Timer":
        """
        Uso:
            with perf.timer("noaa_forecast"):
                result = await get_forecast(...)
        """
        return self._Timer(self, name)

    def record_time(self, name: str, elapsed_ms: float, error: bool = False):
        """Registra un tiempo ya medido externamente."""
        with self._lock:
            self._timings[name].append(elapsed_ms)
            self._call_counts[name] += 1
            if error:
                self._error_counts[name] += 1

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

    # ── Estadisticas ─────────────────────────────────────────────────────────

    def _timing_stats(self, name: str) -> dict:
        data = list(self._timings.get(name, []))
        if not data:
            return {"avg_ms": None, "min_ms": None, "max_ms": None,
                    "last_ms": None, "calls": 0, "errors": 0}
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
        """Retorna todas las metricas para el endpoint /api/performance."""
        with self._lock:
            cpu_hist  = list(self._cpu_history)
            ram_hist  = list(self._ram_mb_history)
            ramp_hist = list(self._ram_pct_history)
            ts_hist   = list(self._ts_history)

            cpu_now  = cpu_hist[-1]  if cpu_hist  else 0.0
            ram_now  = ram_hist[-1]  if ram_hist  else 0.0
            ramp_now = ramp_hist[-1] if ramp_hist else 0.0
            cpu_avg  = round(sum(cpu_hist)  / len(cpu_hist),  1) if cpu_hist  else 0.0
            ram_avg  = round(sum(ram_hist)  / len(ram_hist),  1) if ram_hist  else 0.0

            # Todos los timings conocidos
            all_names = list(self._timings.keys())
            timings = {n: self._timing_stats(n) for n in all_names}

            # Sistema global (si psutil disponible)
            sys_cpu = 0.0
            sys_ram_total_mb = 0.0
            sys_ram_used_mb  = 0.0
            sys_ram_pct      = 0.0
            if _PSUTIL_OK:
                try:
                    sys_cpu = psutil.cpu_percent(interval=None)
                    vm = psutil.virtual_memory()
                    sys_ram_total_mb = round(vm.total  / 1024 / 1024, 1)
                    sys_ram_used_mb  = round(vm.used   / 1024 / 1024, 1)
                    sys_ram_pct      = vm.percent
                except Exception:
                    pass

            return {
                # Proceso del bot
                "process": {
                    "cpu_pct":      round(cpu_now, 1),
                    "cpu_avg_1m":   cpu_avg,
                    "ram_mb":       round(ram_now, 1),
                    "ram_avg_1m":   ram_avg,
                    "psutil_ok":    _PSUTIL_OK,
                },
                # Sistema completo
                "system": {
                    "cpu_pct":      round(sys_cpu, 1),
                    "ram_total_mb": sys_ram_total_mb,
                    "ram_used_mb":  sys_ram_used_mb,
                    "ram_pct":      sys_ram_pct,
                },
                # Historial para graficas (ultimos _HISTORY_LEN puntos)
                "history": {
                    "cpu":     [round(x, 1) for x in cpu_hist],
                    "ram_mb":  [round(x, 1) for x in ram_hist],
                    "ram_pct": [round(x, 1) for x in ramp_hist],
                    "ts":      [round(x, 3) for x in ts_hist],
                    "len":     len(cpu_hist),
                },
                # Tiempos por componente
                "timings": timings,
                # Metricas del ciclo de trading
                "bot": {
                    "scan_count":    self._scan_count,
                    "markets_total": self._markets_total,
                    "opps_found":    self._opps_found,
                    "trades_eval":   self._trades_eval,
                    "last_scan_ms":  round(self._last_scan_ms, 1),
                    "last_scan_ts":  self._last_scan_ts,
                    "markets_per_scan": round(self._markets_total / max(self._scan_count, 1), 1),
                },
                "sample_count": self._sample_count,
            }

    def get_current_resources(self) -> dict:
        """Version ligera — solo CPU y RAM ahora mismo (para /api/status)."""
        cpu, ram_mb, _ = self._read_resources()
        return {"cpu_pct": round(cpu, 1), "ram_mb": round(ram_mb, 1)}


# Instancia global
perf = PerformanceMonitor()
