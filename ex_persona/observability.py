"""结构化日志与轻量指标：让线上问题可追踪、可度量。

日志统一输出为单行 JSON，便于采集；指标是进程内计数器与耗时统计，通过
``/api/metrics`` 暴露，适合单进程部署与排障，不依赖外部监控系统。
"""

import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

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


LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "nian.log"


def log_dir() -> Path:
    return Path(os.getenv("PERSONA_DATA_DIR", "data")).expanduser() / LOG_DIR_NAME


def log_file_path() -> Path:
    return log_dir() / LOG_FILE_NAME


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if getattr(root, "_nian_json", False):
        return
    formatter = JsonFormatter()
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root.addHandler(handler)
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file_path(), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:  # 目录不可写时仍保留 stdout 日志
        pass
    root.setLevel(level)
    root._nian_json = True  # type: ignore[attr-defined]


_LEVEL_ORDER = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


def _iter_log_lines() -> Iterator[tuple[str, str]]:
    path = log_file_path()
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                text = line.rstrip("\n")
                if text:
                    yield path.name, text
    except OSError:
        return


def read_log_tail(level: str = "", keyword: str = "", limit: int = 200) -> list[dict]:
    """读取本地滚动日志尾部，支持最低级别与关键词过滤。"""
    threshold = _LEVEL_ORDER.get((level or "").upper(), 0)
    needle = (keyword or "").strip().lower()
    size = max(1, min(int(limit), 1000))
    collected: list[dict] = []
    for source, line in _iter_log_lines():
        try:
            payload = json.loads(line)
        except ValueError:
            payload = {"msg": line}
        line_level = str(payload.get("level") or "").upper()
        if threshold and _LEVEL_ORDER.get(line_level, 0) < threshold:
            continue
        if needle and needle not in line.lower():
            continue
        collected.append(
            {
                "ts": payload.get("ts", ""),
                "level": line_level or "INFO",
                "logger": payload.get("logger", ""),
                "msg": payload.get("msg", ""),
                "source": source,
            }
        )
    collected.reverse()
    return collected[:size]



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
