"""积分到期巡检：定期清零到期批次，并在到期前发送提醒。"""

import logging
import threading

from . import store

log = logging.getLogger("ex_persona.credits")

DEFAULT_INTERVAL_SECONDS = 600
REMIND_WINDOW_HOURS = 72


class CreditExpiryWorker:
    """后台轮询：清零已到期积分批次，并对 72 小时内到期的批次提醒一次。

    ``interval`` 取 600 秒，满足「巡检间隔不超过 1 小时」的要求。测试可注入
    ``now_func`` 直接调用 :meth:`tick`，无需启动线程。
    """

    def __init__(self, interval: int = DEFAULT_INTERVAL_SECONDS, now_func=None) -> None:
        self._interval = max(int(interval), 10)
        self._now = now_func or store.utcnow
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001 - 单次巡检失败不影响后续
                log.warning(
                    "credit expiry tick failed",
                    extra={"event": "credit.expire.tick_error", "error": str(error)},
                )

    def tick(self, now: str | None = None) -> dict:
        moment = now or self._now()
        expired = store.expire_credit_batches(moment)
        reminded = store.remind_expiring_batches(moment, REMIND_WINDOW_HOURS)
        result = {**expired, "reminded": reminded["sent"]}
        if expired["expired_batches"] or reminded["sent"]:
            log.info(
                "credit expiry tick",
                extra={"event": "credit.expire", **result},
            )
        return result
