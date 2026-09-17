"""结构化日志与轻量指标：让线上问题可追踪、可度量。

日志统一输出为单行 JSON，便于采集；指标是进程内计数器与耗时统计，通过
``/api/metrics`` 暴露，适合单进程部署与排障，不依赖外部监控系统。
"""

import json
import logging
import threading
import time

_EXTRA_KEYS = (
    "event",
    "user_id",
    "persona_id",
    "contact",
    "status",
    "duration_ms",
    "error",
    "recipient",
    "task_id",
    "kind",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in _EXTRA_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if getattr(root, "_nian_json", False):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    root._nian_json = True  # type: ignore[attr-defined]


def log_event(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields) -> None:
    logger.log(level, msg, extra=fields)


class Metrics:
    """进程内计数器：计数与耗时。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = {}
        self._timings: dict[str, list[float]] = {}
        self._started = time.time()

    def inc(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + value

    def observe(self, name: str, seconds: float) -> None:
        with self._lock:
            bucket = self._timings.setdefault(name, [])
            bucket.append(seconds)
            if len(bucket) > 200:
                del bucket[: len(bucket) - 200]

    def snapshot(self) -> dict:
        with self._lock:
            counters = dict(self._counters)
            timings = {
                name: {
                    "count": len(values),
                    "avg_ms": round(sum(values) / len(values) * 1000, 2) if values else 0.0,
                    "max_ms": round(max(values) * 1000, 2) if values else 0.0,
                }
                for name, values in self._timings.items()
            }
        return {"uptime_seconds": round(time.time() - self._started, 1), "counters": counters, "timings": timings}

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._timings.clear()


METRICS = Metrics()


def snapshot() -> dict:
    return METRICS.snapshot()
