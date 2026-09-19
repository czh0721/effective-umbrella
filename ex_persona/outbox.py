"""出站队列：所有主动发送的统一入口，带指数退避与每账号速率限制。

消息先入库（``outbox`` 表）再由后台工作线程发送，因此进程重启不会丢消息，
失败会自动退避重试，超过上限标记为 ``failed`` 供排查。
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import observability, store

log = logging.getLogger("nian.outbox")

# 每账号每分钟最多发送条数，避免触发微信风控。
PER_MINUTE = 20
# 同账号两条消息之间的最小间隔（秒），让节奏更接近真人。
MIN_INTERVAL = 1.0
MAX_ATTEMPTS = 5
BASE_BACKOFF = 5.0
MAX_BACKOFF = 600.0
POLL_SECONDS = 2.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OutboxWorker:
    def __init__(
        self,
        send_func,
        *,
        per_minute: int = PER_MINUTE,
        min_interval: float = MIN_INTERVAL,
        max_attempts: int = MAX_ATTEMPTS,
        poll_seconds: float = POLL_SECONDS,
        base_backoff: float = BASE_BACKOFF,
        max_backoff: float = MAX_BACKOFF,
        on_fail=None,
    ) -> None:
        self._send = send_func
        self._per_minute = max(int(per_minute), 1)
        self._min_interval = max(float(min_interval), 0.0)
        self._max_attempts = max(int(max_attempts), 1)
        self._poll = max(float(poll_seconds), 0.2)
        self._base_backoff = max(float(base_backoff), 0.0)
        self._max_backoff = max(float(max_backoff), 0.0)
        self._on_fail = on_fail
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sent: dict[int, float] = {}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="nian-outbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def enqueue(
        self,
        user_id: int,
        recipient: str,
        text: str = "",
        media: str = "",
        delay_seconds: float = 0,
        charge_ref: str = "",
        charge_amount: int = 0,
    ) -> int:
        if not recipient or (not text and not media):
            return 0
        outbox_id = store.add_outbox(
            user_id,
            recipient,
            text,
            media,
            delay_seconds=delay_seconds,
            charge_ref=charge_ref,
            charge_amount=charge_amount,
        )
        observability.METRICS.inc("outbox.enqueued")
        return outbox_id

    def _loop(self) -> None:
        while not self._stop.wait(self._poll):
            try:
                self.drain_once()
            except Exception:  # noqa: BLE001 - 工作线程永不退出
                log.exception("outbox drain failed", extra={"event": "outbox.drain_error"})

    def drain_once(self) -> int:
        """处理一批到期消息，返回成功发送条数。可被测试直接调用。"""
        sent = 0
        now = time.time()
        for row in store.list_due_outbox(_now_iso(), limit=20):
            if self._stop.is_set():
                break
            if not self._allowed(row["user_id"], now):
                continue
            error = ""
            try:
                ok = bool(self._send(row["user_id"], row["recipient"], row["text"], row["media"]))
            except Exception as exc:  # noqa: BLE001
                ok = False
                error = str(exc)
            if ok:
                store.mark_outbox_sent(row["id"])
                self._last_sent[row["user_id"]] = time.time()
                observability.METRICS.inc("outbox.sent")
                sent += 1
            else:
                self._fail(row, error or "send returned failure")
        return sent

    def _allowed(self, user_id: int, now: float) -> bool:
        last = self._last_sent.get(user_id, 0.0)
        if last and now - last < self._min_interval:
            return False
        since = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        return store.count_outbox_sent_since(user_id, since) < self._per_minute

    def _fail(self, row: dict, error: str) -> None:
        attempts = int(row.get("attempts") or 0) + 1
        if attempts >= self._max_attempts:
            store.mark_outbox_failed(row["id"], error)
            observability.METRICS.inc("outbox.failed")
            log.warning(
                "outbox give up",
                extra={"event": "outbox.gave_up", "user_id": row["user_id"], "error": error},
            )
            if self._on_fail is not None:
                try:
                    self._on_fail(row, error)
                except Exception:  # noqa: BLE001 - 回调失败不影响队列
                    log.exception("outbox on_fail failed", extra={"event": "outbox.on_fail_error"})
            return
        delay = min(self._base_backoff * (2 ** (attempts - 1)), self._max_backoff)
        next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        store.mark_outbox_retry(row["id"], error, next_at, attempts)
        observability.METRICS.inc("outbox.retry")
