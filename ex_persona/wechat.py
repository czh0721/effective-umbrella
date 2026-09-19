"""微信桥接：为每个用户拉起独立 HOME 环境下的 weclaw 进程。

每个用户拥有独立的 ``home/.weclaw`` 配置与账号目录，桥接令牌决定微信消息
被路由到哪个用户的人格。平台按 user_id 管理桥接的登录、启停与状态。
"""

import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

WECLAW_BIN = os.getenv("WECLAW_BIN", "weclaw")
AGENT_NAME = "persona"


def bound_accounts(home_dir: str | Path) -> bool:
    """判断该 HOME 下是否已有登录成功的微信账号（weclaw 账号文件）。"""
    accounts = Path(home_dir).expanduser().resolve() / ".weclaw" / "accounts"
    return accounts.is_dir() and any(accounts.glob("*-im-bot.json"))


class WeChatBridge:
    """单个用户的 weclaw 桥接。"""

    def __init__(
        self,
        user_id: int,
        home_dir: Path,
        port: int,
        on_event: Callable[[int, str], None] | None = None,
    ) -> None:
        self.user_id = user_id
        # HOME 必须是绝对路径：weclaw 用 $HOME/.weclaw 定位配置与账号，
        # 相对路径会被它按自己的 cwd 再拼一次，导致读不到配置而回退到 echo。
        self.home = Path(home_dir).expanduser().resolve()
        self.port = port
        self._lock = threading.Lock()
        self._login: subprocess.Popen | None = None
        self._bridge: subprocess.Popen | None = None
        self._qr_url: str | None = None
        self._log = ""
        self._bridge_log = ""
        self._phase = "idle"
        self._autostart = True
        self._last_try = 0.0
        self._last_prune = 0.0
        self._expired = False
        # 启停串行化：避免并发 status / watchdog 同时 stop+start 互相踩踏。
        self._op_lock = threading.Lock()
        # 配置写入串行化：status 轮询会频繁 ensure_config，避免并发写坏文件。
        self._cfg_lock = threading.Lock()
        self._on_event = on_event

    # -- 基础 ------------------------------------------------------------- #

    @property
    def config_path(self) -> Path:
        return self.home / ".weclaw" / "config.json"

    def _env(self) -> dict:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env.setdefault("USERPROFILE", str(self.home))
        return env

    def available(self) -> bool:
        return bool(shutil.which(WECLAW_BIN)) or Path(WECLAW_BIN).exists()

    # -- 进程探测 --------------------------------------------------------- #
    # weclaw 的 ``stop`` 会按可执行文件路径 pkill 掉机器上所有 weclaw 进程，
    # 多用户环境下会互相误杀。这里改为只按 HOME 精确识别属于自己的实例。

    def _iter_home_bridge_pids(self) -> list[int]:
        proc_root = Path("/proc")
        if not proc_root.is_dir():
            return []
        target_home = str(self.home)
        pids: list[int] = []
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == os.getpid():
                continue
            try:
                argv = [
                    part.decode("utf-8", "replace")
                    for part in (entry / "cmdline").read_bytes().split(b"\x00")
                    if part
                ]
            except OSError:
                continue
            if not argv or "start" not in argv or not any("weclaw" in a for a in argv):
                continue
            try:
                environ = (entry / "environ").read_bytes().split(b"\x00")
            except OSError:
                continue
            home = ""
            for item in environ:
                if item.startswith(b"HOME="):
                    home = item[5:].decode("utf-8", "replace")
                    break
            if home == target_home:
                pids.append(pid)
        return pids

    def _home_bridge_alive(self) -> bool:
        return bool(self._iter_home_bridge_pids())

    # -- 账号文件 --------------------------------------------------------- #
    # 每次扫码 weclaw 都会在 ``$HOME/.weclaw/accounts`` 新增一个账号文件（按 bot id
    # 命名），旧的不会自动清理。weclaw ``start`` 会为目录下每个账号各起一个 monitor，
    # 于是历史失效账号的 "session expired" 会持续刷日志，既拖慢启动，又会让新登录
    # 的有效账号被误判为整体失效。这里只保留最新的一次登录。

    def account_files(self) -> list[Path]:
        accounts = self.home / ".weclaw" / "accounts"
        if not accounts.is_dir():
            return []
        return sorted(
            accounts.glob("*-im-bot.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    def prune_stale_accounts(self) -> int:
        """只保留最新登录的账号，其余移到 ``_stale/`` 备份（不删除）。"""
        files = self.account_files()
        if len(files) <= 1:
            return 0
        keep = files[0]
        keep_prefix = keep.name[: -len(".json")]
        accounts = keep.parent
        backup = accounts / "_stale"
        moved = 0
        for path in list(accounts.glob("*-im-bot*.json")):
            if path.name.startswith(keep_prefix):
                continue
            try:
                backup.mkdir(parents=True, exist_ok=True)
                path.rename(backup / path.name)
                moved += 1
            except OSError:
                continue
        return moved

    def _emit(self, event: str) -> None:
        """把桥接状态变化通知给上层（webapp 落库 phase / 发提醒）。"""
        if self._on_event is None:
            return
        try:
            self._on_event(self.user_id, event)
        except Exception:  # noqa: BLE001 - 回调失败不影响桥接本身
            pass

    def _terminate_home_processes(self) -> None:
        pids = self._iter_home_bridge_pids()
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                continue
        if pids:
            time.sleep(0.5)

    def ensure_config(self, bridge_token: str) -> None:
        config: dict = {}
        if self.config_path.exists():
            try:
                config = json.loads(self.config_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                config = {}
        agents = config.setdefault("agents", {})
        agents[AGENT_NAME] = {
            "type": "http",
            "endpoint": (
                f"http://127.0.0.1:{self.port}/v1/chat/completions/{bridge_token}"
            ),
            "api_key": "local",
            "model": AGENT_NAME,
        }
        config["default_agent"] = AGENT_NAME
        # 收到对方发来的图片/表情时，由 weclaw 回调本端点保存为该人格的表情包。
        config["media_endpoint"] = (
            f"http://127.0.0.1:{self.port}/api/wechat/media/{bridge_token}"
        )
        # 收到语音时额外把原始音频回调本端点存档，用于音色克隆。
        config["voice_endpoint"] = (
            f"http://127.0.0.1:{self.port}/api/wechat/voice/{bridge_token}"
        )
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
        with self._cfg_lock:
            # 原子替换：status 轮询与 watchdog 会并发调用，避免 weclaw 读到半截 JSON
            # 后退回自带 agent 探测（例如 openclaw）而报连接失败。
            tmp = self.config_path.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.config_path)

    def _run(self, *args: str, timeout: int = 15) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                [WECLAW_BIN, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._env(),
                cwd=str(self.home),
            )
        except FileNotFoundError:
            return 127, "未找到 weclaw 可执行文件"
        except subprocess.TimeoutExpired:
            return 124, "weclaw 命令超时"
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    # -- 登录 ------------------------------------------------------------- #

    def start_login(self, bridge_token: str) -> dict:
        with self._lock:
            alive = self._login is not None and self._login.poll() is None
        if alive:
            return self.status()

        # 重新扫码意味着旧会话已不可用：先清掉旧桥接进程，避免它在后台继续
        # 刷「session expired」，也避免新的登录凭据落盘后仍被旧进程占用。
        with self._op_lock:
            with self._lock:
                old = self._bridge
                self._bridge = None
            self._terminate_home_processes()
            if old is not None and old.poll() is None:
                old.terminate()

        self.ensure_config(bridge_token)
        with self._lock:
            self._autostart = True
            self._expired = False
            self._qr_url = None
            self._log = ""
            self._phase = "starting"
            try:
                self._login = subprocess.Popen(
                    [WECLAW_BIN, "login"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=self._env(),
                    cwd=str(self.home),
                )
            except FileNotFoundError:
                self._login = None
                self._phase = "failed"
                self._log = "未找到 weclaw 可执行文件，请确认已安装并在 PATH 中"
                return self.status()
            threading.Thread(target=self._pump_login, args=(self._login,), daemon=True).start()

        for _ in range(150):
            with self._lock:
                if self._qr_url or self._phase == "failed":
                    break
            time.sleep(0.2)
        return self.status()

    def _pump_login(self, proc: subprocess.Popen) -> None:
        if proc.stdout is not None:
            for line in proc.stdout:
                with self._lock:
                    self._log = (self._log + line)[-8000:]
                    if "QR URL:" in line:
                        self._qr_url = line.split("QR URL:", 1)[1].strip()
                        self._phase = "waiting-scan"
        code = proc.wait()
        with self._lock:
            if self._phase != "failed":
                self._phase = "logged-in" if code == 0 else "failed"
        if code == 0:
            # 绑定成功即自动开启转发，无需用户手动启动。必须强制重启：否则旧进程
            # 句柄还在时 start_bridge 会直接返回，新登录的凭据不会被加载。
            self._expired = False
            self.start_bridge(force=True)
        else:
            self._emit("failed")

    # -- 桥接 ------------------------------------------------------------- #

    def is_bound(self) -> bool:
        return bound_accounts(self.home)

    def maybe_autostart(self) -> None:
        if not self._autostart or self._expired or self.is_running() or not self.is_bound():
            return
        now = time.time()
        if now - self._last_try < 12:
            return
        self._last_try = now
        self.start_bridge()

    def recover(self) -> dict:
        """清理历史扫码残留账号后尝试恢复转发。

        典型场景：用户反复扫码导致 accounts 目录堆积失效账号，新账号的有效登录被
        整体误判为过期。清理只保留最新账号并强制重启，即可恢复收发。
        """
        self._expired = False
        self._autostart = True
        self._last_prune = time.time()
        pruned = self.prune_stale_accounts()
        result = self.start_bridge(force=True)
        result["pruned"] = pruned
        return result

    def start_bridge(self, force: bool = False) -> dict:
        with self._op_lock:
            with self._lock:
                if not force and self._bridge is not None and self._bridge.poll() is None:
                    return {"code": 0, "output": "已在运行"}
            # 只保留最新账号，避免历史失效账号拖慢启动/污染日志。
            self.prune_stale_accounts()
            # 清理本用户遗留的旧实例（例如服务重启后失去 Popen 句柄的孤儿进程）。
            self._terminate_home_processes()
            with self._lock:
                self._bridge_log = ""
                try:
                    self._bridge = subprocess.Popen(
                        [WECLAW_BIN, "start", "-f"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        env=self._env(),
                        cwd=str(self.home),
                    )
                except FileNotFoundError:
                    self._bridge = None
                    return {"code": 127, "output": "未找到 weclaw 可执行文件"}
                threading.Thread(target=self._pump_bridge, args=(self._bridge,), daemon=True).start()
            time.sleep(1.5)
            with self._lock:
                alive = self._bridge is not None and self._bridge.poll() is None
                self._phase = "running" if alive else "failed"
            if alive:
                self._expired = False
            self._emit("running" if alive else "failed")
            return {"code": 0 if alive else 1, "output": "微信转发已启动" if alive else "启动失败，请查看日志"}

    def _pump_bridge(self, proc: subprocess.Popen) -> None:
        if proc.stdout is not None:
            for line in proc.stdout:
                with self._lock:
                    self._bridge_log = (self._bridge_log + line)[-8000:]

    def stop_bridge(self, explicit: bool = True) -> dict:
        if explicit:
            with self._lock:
                self._autostart = False
        with self._op_lock:
            with self._lock:
                proc = self._bridge
                self._bridge = None
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
            # 连同失去句柄的孤儿实例一起清理，且不影响其他用户。
            self._terminate_home_processes()
        with self._lock:
            if self._phase != "logged-in":
                self._phase = "idle"
        self._expired = False
        if explicit:
            self._emit("logged-in")
        return {"code": 0, "output": "已停止"}

    def is_running(self) -> bool:
        if self._expired:
            return False
        with self._lock:
            if self._bridge is not None and self._bridge.poll() is None:
                return True
        # 服务重启后 in-memory 句柄会丢失，但 weclaw 可能仍在运行；按 HOME 精确判定。
        return self._home_bridge_alive()

    def send(self, to: str, text: str = "", media: str = "") -> dict:
        if not to:
            return {"code": 1, "output": "缺少接收方"}
        args = ["send", "--to", to]
        if text:
            args += ["--text", text]
        if media:
            args += ["--media", media]
        code, output = self._run(*args, timeout=30)
        return {"code": code, "output": output.strip()}

    def status(self, with_svg: bool = True) -> dict:
        with self._lock:
            login_alive = self._login is not None and self._login.poll() is None
            bridge_alive = self._bridge is not None and self._bridge.poll() is None
            phase = self._phase
            qr_url = self._qr_url
            log = self._log
            bridge_log = self._bridge_log

        code, output = self._run("status", timeout=10)
        low = output.lower()
        combined = (output + "\n" + bridge_log).lower()
        # 账号目录里有多个登录时，无法判断是哪一条失效；历史扫码残留会让有效的新账号
        # 被误判为整体失效。此时清理旧账号并重启桥接，绝不直接判死。
        if len(self.account_files()) > 1:
            if time.time() - self._last_prune >= 15:
                self._last_prune = time.time()
                # 先清掉旧日志，避免清理与重启之间的轮询又看到残留的失效告警。
                with self._lock:
                    self._bridge_log = ""
                if self.prune_stale_accounts():
                    threading.Thread(
                        target=self.start_bridge, args=(True,), daemon=True
                    ).start()
            expired = False
        else:
            # weclaw 会话过期后进程可能还活着（monitor 仍在刷日志，但 pidfile 已失效），
            # 此时不能报告「在线」，否则用户会以为在转发实际却收不到消息。
            expired = "session expired" in combined or "re-authenticate" in combined
        if expired and not self._expired:
            self._expired = True
            self._autostart = False
            self._emit("expired")
            threading.Thread(target=self._terminate_home_processes, daemon=True).start()
        running = False if (expired or self._expired) else (
            bridge_alive
            or (code == 0 and "not running" not in low)
            or self._home_bridge_alive()
        )
        payload = {
            "available": self.available(),
            "phase": "expired" if self._expired else phase,
            "expired": self._expired,
            "login_alive": login_alive,
            "running": running,
            "qr_seq": hashlib.md5(qr_url.encode("utf-8")).hexdigest()[:12] if qr_url else "",
            "weclaw": output.strip(),
            "log": log[-1500:],
            "bridge_log": bridge_log[-3000:],
            "config_path": str(self.config_path),
            "home_dir": str(self.home),
        }
        if with_svg and qr_url:
            try:
                payload["qr_svg"] = qr_svg(qr_url)
            except Exception as error:  # noqa: BLE001
                payload["qr_error"] = str(error)
        return payload


class WeChatManager:
    """按 user_id 管理多个用户的桥接实例。"""

    def __init__(self, port: int, on_event: Callable[[int, str], None] | None = None) -> None:
        self.port = port
        self._on_event = on_event
        self._lock = threading.Lock()
        self._bridges: dict[int, WeChatBridge] = {}

    def get(self, user_id: int, home_dir: Path, bridge_token: str) -> WeChatBridge:
        with self._lock:
            bridge = self._bridges.get(user_id)
            if bridge is None:
                bridge = WeChatBridge(user_id, home_dir, self.port, on_event=self._on_event)
                self._bridges[user_id] = bridge
            bridge.ensure_config(bridge_token)
            return bridge

    def stop(self, user_id: int, explicit: bool = True) -> dict:
        with self._lock:
            bridge = self._bridges.get(user_id)
        if bridge is None:
            return {"code": 0, "output": "未运行"}
        return bridge.stop_bridge(explicit=explicit)

    def is_running(self, user_id: int) -> bool:
        with self._lock:
            bridge = self._bridges.get(user_id)
        return bool(bridge and bridge.is_running())

    def stop_all(self) -> None:
        with self._lock:
            bridges = list(self._bridges.values())
        for bridge in bridges:
            try:
                # 进程退出时的清理不代表用户意图：不要改写 phase，否则重启后无法
                # 区分「用户主动关闭」和「服务正常停机」。
                bridge.stop_bridge(explicit=False)
            except Exception:  # noqa: BLE001
                pass


def qr_svg(data: str) -> str:
    import qrcode
    import qrcode.image.svg

    image = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode("utf-8")
