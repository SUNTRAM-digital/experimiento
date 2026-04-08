"""
Performance Monitor — monitoreo de recursos del sistema y tiempos de ejecucion.

Trackea en tiempo real:
  - CPU: porcentaje de uso del proceso y del sistema
  - Memoria: RSS, VMS, porcentaje del sistema
  - Tiempos de ejecucion: por componente (scan, forecast, Claude, Kalman, etc.)
  - Throughput: mercados analizados/minuto, trades evaluados/ciclo
  - Latencia de APIs externas: NOAA, OpenMeteo, Polymarket Gamma
  - Resource log: eventos estructurados sobre llamadas, spikes, ciclos

Uso:
  from performance_monitor import perf
  with perf.timer("noaa_forecast"):
      result = await get_forecast(...)
  stats = perf.get_stats()
  log   = perf.get_resource_log()
"""
import time
import threading
from collections import deque, defaultdict
from datetime import datetime, timezone
from typing import Optional
import os

try:
    import psutil
    _PSUTIL_OK = True
except ImportError:
    _PSUTIL_OK = False


# Numero de muestras historicas a mantener para graficas
_HISTORY_LEN   = 60    # ultimos 60 puntos (1 por segundo → 1 minuto)
_TIMING_LEN    = 100   # ultimas 100 ejecuciones por componente
_RESLOG_LEN    = 500   # entradas del resource log

# Umbrales para alertas automaticas
_SLOW_MS       = 3000  # llamada lenta si supera 3s
_CPU_SPIKE_PCT = 75.0  # alerta si CPU del proceso supera este %
_RAM_SPIKE_MB  = 20.0  # alerta si RAM sube mas de 20MB en un segundo

# Categorias de los componentes para el log
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
        self._timings:     dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=_TIMING_LEN))
        self._call_counts: dict[str, int]           = defaultdict(int)
        self._error_counts:dict[str, int]           = defaultdict(int)

        # Metricas del ciclo del bot
        self._scan_count     = 0
        self._markets_total  = 0
        self._opps_found     = 0
        self._trades_eval    = 0
        self._last_scan_ms   = 0.0
        self._last_scan_ts   = 0.0

        # Resource log — eventos estructurados
        self._resource_log: deque[dict] = deque(maxlen=_RESLOG_LEN)

        # Ultimo valor de RAM para detectar spikes
        self._last_ram_mb = 0.0
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

    # ── Resource log ─────────────────────────────────────────────────────────

    def _rlog(self, level: str, category: str, msg: str, detail: Optional[dict] = None):
        """Añade una entrada al resource log (thread-safe)."""
        entry = {
            "time":     _now_str(),
            "level":    level,       # INFO | WARN | ERROR
            "category": category,    # API | FORECAST | AI | COMPUTE | CYCLE | MEMORY | CPU
            "msg":      msg,
            "detail":   detail or {},
        }
        with self._lock:
            self._resource_log.append(entry)

    def get_resource_log(self, limit: int = 200) -> list[dict]:
        with self._lock:
            entries = list(self._resource_log)
        return entries[-limit:]

    # ── Sampler interno (1 muestra/seg) ───────────────────────────────────────

    def _sampler_loop(self):
        """Toma muestras de CPU y RAM cada segundo. Emite alertas al resource log."""
        while self._running:
            try:
                cpu_pct, ram_mb, ram_pct = self._read_resources()
                ts = time.time()

                # Detectar spikes antes de guardar
                prev_ram = self._last_ram_mb
                ram_delta = ram_mb - prev_ram if prev_ram > 0 else 0.0

                with self._lock:
                    self._cpu_history.append(cpu_pct)
                    self._ram_mb_history.append(ram_mb)
                    self._ram_pct_history.append(ram_pct)
                    self._ts_history.append(ts)
                    self._last_ram_mb = ram_mb
                    self._sample_count += 1

                # Alertas fuera del lock para no bloquear
                if cpu_pct >= _CPU_SPIKE_PCT:
                    self._rlog("WARN", "CPU",
                        f"CPU spike: {cpu_pct:.1f}% (proceso)",
                        {"cpu_pct": cpu_pct, "ram_mb": round(ram_mb, 1)})

                if ram_delta >= _RAM_SPIKE_MB:
                    self._rlog("WARN", "MEMORY",
                        f"RAM subio +{ram_delta:.1f} MB → {ram_mb:.1f} MB total",
                        {"ram_mb": round(ram_mb, 1), "delta_mb": round(ram_delta, 1)})

            except Exception:
                pass
            time.sleep(1.0)

    def _read_resources(self) -> tuple[float, float, float]:
        """Lee CPU%, RAM MB del proceso, RAM% del sistema."""
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

    # ── Context manager para medir tiempos ────────────────────────────────────

    class _Timer:
        """Context manager que registra el tiempo y emite entrada al resource log."""
        def __init__(self, monitor: "PerformanceMonitor", name: str, extra: Optional[dict] = None):
            self._mon   = monitor
            self._name  = name
            self._extra = extra or {}
            self._t0    = 0.0

        def __enter__(self):
            self._t0 = time.perf_counter()
            return self

        def __exit__(self, exc_type, *_):
            elapsed_ms = (time.perf_counter() - self._t0) * 1000
            error = exc_type is not None

            with self._mon._lock:
                self._mon._timings[self._name].append(elapsed_ms)
                self._mon._call_counts[self._name] += 1
                if error:
                    self._mon._error_counts[self._name] += 1

            # Emitir al resource log
            category = _COMPONENT_CATEGORY.get(self._name, "OTHER")
            detail   = {"ms": round(elapsed_ms, 1), **self._extra}

            if error:
                self._mon._rlog("ERROR", category,
                    f"{self._name} FALLÓ ({elapsed_ms:.0f} ms)",
                    detail)
            elif elapsed_ms >= _SLOW_MS:
                self._mon._rlog("WARN", category,
                    f"{self._name} lento: {elapsed_ms:.0f} ms",
                    detail)
            else:
                self._mon._rlog("INFO", category,
                    f"{self._name}: {elapsed_ms:.0f} ms",
                    detail)

    def timer(self, name: str, **extra) -> "_Timer":
        """
        Uso:
            with perf.timer("noaa_forecast", station="KLGA"):
                result = await get_forecast(...)
        """
        return self._Timer(self, name, extra)

    def record_time(self, name: str, elapsed_ms: float, error: bool = False, **extra):
        """Registra un tiempo ya medido externamente y emite al resource log."""
        with self._lock:
            self._timings[name].append(elapsed_ms)
            self._call_counts[name] += 1
            if error:
                self._error_counts[name] += 1

        category = _COMPONENT_CATEGORY.get(name, "OTHER")
        detail   = {"ms": round(elapsed_ms, 1), **extra}

        if error:
            self._rlog("ERROR", category, f"{name} FALLÓ ({elapsed_ms:.0f} ms)", detail)
        elif elapsed_ms >= _SLOW_MS:
            self._rlog("WARN",  category, f"{name} lento: {elapsed_ms:.0f} ms",  detail)
        else:
            self._rlog("INFO",  category, f"{name}: {elapsed_ms:.0f} ms",        detail)

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

        # Resumen del ciclo al resource log
        _, ram_mb, _ = self._read_resources()
        level = "WARN" if scan_ms >= _SLOW_MS else "INFO"
        self._rlog(level, "CYCLE",
            f"Ciclo #{self._scan_count} completado: {scan_ms:.0f} ms | "
            f"{markets_analyzed} mercados | {opps_found} opps | RAM {ram_mb:.0f} MB",
            {"scan_ms": round(scan_ms, 1), "markets": markets_analyzed,
             "opps": opps_found, "ram_mb": round(ram_mb, 1),
             "scan_n": self._scan_count})

    # ── Estadisticas ─────────────────────────────────────────────────────────

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
        """Retorna todas las metricas para el endpoint /api/performance."""
        with self._lock:
            cpu_hist  = list(self._cpu_history)
            ram_hist  = list(self._ram_mb_history)
            ramp_hist = list(self._ram_pct_history)
            ts_hist   = list(self._ts_history)

            cpu_now  = cpu_hist[-1]  if cpu_hist  else 0.0
            ram_now  = ram_hist[-1]  if ram_hist  else 0.0
            cpu_avg  = round(sum(cpu_hist) / len(cpu_hist), 1) if cpu_hist else 0.0
            ram_avg  = round(sum(ram_hist) / len(ram_hist), 1) if ram_hist else 0.0

            all_names = list(self._timings.keys())
            timings   = {n: self._timing_stats(n) for n in all_names}

        sys_cpu = 0.0
        sys_ram_total_mb = sys_ram_used_mb = sys_ram_pct = 0.0
        if _PSUTIL_OK:
            try:
                sys_cpu = psutil.cpu_percent(interval=None)
                vm = psutil.virtual_memory()
                sys_ram_total_mb = round(vm.total / 1024 / 1024, 1)
                sys_ram_used_mb  = round(vm.used  / 1024 / 1024, 1)
                sys_ram_pct      = vm.percent
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
                "ram_total_mb": sys_ram_total_mb,
                "ram_used_mb":  sys_ram_used_mb,
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
        """Version ligera — solo CPU y RAM ahora mismo."""
        cpu, ram_mb, _ = self._read_resources()
        return {"cpu_pct": round(cpu, 1), "ram_mb": round(ram_mb, 1)}


# Instancia global
perf = PerformanceMonitor()
