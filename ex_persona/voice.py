"""MiniMax 语音合成与音色克隆，以及本地音频转码。

微信语音样本可能来自不同编码容器，而 MiniMax 音色克隆只接受 mp3 / m4a / wav
且要求单个文件 10 秒–5 分钟。这里统一用 ffmpeg 把多条样本解码、拼接并转成单条
mp3，再交给 MiniMax。TTS 直接产出 mp3 音频字节。
"""

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"
FFMPEG = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.getenv("FFPROBE_BIN", "ffprobe")
# 微信语音下发编码为 silk（#!SILK_V3），ffmpeg 不支持，需要独立解码器。
SILK_DECODER_CANDIDATES = (
    "/opt/nian/bin/silk_v3_decoder",
    "/opt/nian/tools/silk-v3-decoder/silk/decoder",
)
SILK_SAMPLE_RATE = 24000

# 克隆输入约束（与 MiniMax 文档一致）。
CLONE_MIN_SECONDS = 10
CLONE_MAX_SECONDS = 300
CLONE_MAX_BYTES = 20 * 1024 * 1024
# T2A v2 单次合成文本上限。
TTS_MAX_CHARS = 500
# 音频统一转码参数：单声道 24kHz，兼容语音克隆要求。
TARGET_SAMPLE_RATE = 24000

# 前端预设音色映射到 MiniMax 系统音色 id。
PRESET_VOICES = {
    "female-1": "female-shaonv",
    "female-2": "female-yujie",
    "male-1": "male-qn-qingse",
    "male-2": "male-qn-jingying",
}
DEFAULT_PRESET = "female-1"

_RETRYABLE = (urllib.error.URLError, TimeoutError, ConnectionError)


class VoiceError(RuntimeError):
    """语音服务调用失败。``auth`` 为真表示 Key 无效或没有权限。"""

    def __init__(self, message: str, *, auth: bool = False) -> None:
        super().__init__(message)
        self.auth = auth


def system_voice_id(preset: str) -> str:
    return PRESET_VOICES.get(preset or "", PRESET_VOICES[DEFAULT_PRESET])


def active_voice_id(settings: dict) -> tuple[str, bool]:
    """返回 (voice_id, 是否为复刻音色)。"""
    voice = (settings or {}).get("voice") or {}
    clone_id = str(voice.get("clone_voice_id") or "").strip()
    if voice.get("clone_status") == "ready" and clone_id:
        return clone_id, True
    return system_voice_id(str(voice.get("preset") or voice.get("voice") or "")), False


def ffmpeg_available() -> bool:
    return bool(shutil.which(FFMPEG) or Path(FFMPEG).exists())


def silk_decoder_path() -> str:
    """定位 silk 解码器可执行文件，找不到返回空串。"""
    env = (os.getenv("SILK_DECODER_BIN") or "").strip()
    if env and Path(env).exists():
        return env
    for name in ("silk_v3_decoder", "silk_decoder", "decoder"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in SILK_DECODER_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return ""


def silk_available() -> bool:
    return bool(silk_decoder_path())


def is_silk(path: str | Path) -> bool:
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return False
    return b"SILK" in head


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as error:
        raise VoiceError("未安装 ffmpeg，无法处理音频") from error
    except subprocess.TimeoutExpired as error:
        raise VoiceError("音频处理超时") from error


def probe_duration_seconds(path: str | Path) -> float:
    """用 ffprobe 读取音频时长（秒）；失败返回 0。"""
    proc = _run([
        FFPROBE, "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(path),
    ], timeout=30)
    if proc.returncode != 0:
        return 0.0
    try:
        payload = json.loads(proc.stdout or "{}")
        return float(payload.get("format", {}).get("duration") or 0.0)
    except (ValueError, TypeError):
        return 0.0


def _to_wav(source: str | Path, dest: str | Path) -> None:
    if is_silk(source):
        _decode_silk(source, dest)
        return
    proc = _run([
        FFMPEG, "-y", "-v", "error", "-i", str(source),
        "-ac", "1", "-ar", str(TARGET_SAMPLE_RATE), "-f", "wav", str(dest),
    ])
    if proc.returncode == 0:
        return
    # 有些样本没有明显的扩展名/魔数，ffmpeg 失败后再尝试 silk 解码。
    if silk_available():
        try:
            _decode_silk(source, dest)
            return
        except VoiceError:
            pass
    raise VoiceError(f"音频转码失败：{proc.stderr.strip()[:200]}")


def _decode_silk(source: str | Path, dest: str | Path) -> None:
    decoder = silk_decoder_path()
    if not decoder:
        raise VoiceError("未安装 silk 解码器，无法处理微信语音")
    with tempfile.TemporaryDirectory() as tmp:
        pcm = Path(tmp) / "decoded.pcm"
        proc = _run([decoder, str(source), str(pcm)], timeout=60)
        if proc.returncode != 0 or not pcm.exists():
            raise VoiceError(f"silk 解码失败：{proc.stderr.strip()[:200]}")
        convert = _run([
            FFMPEG, "-y", "-v", "error", "-f", "s16le",
            "-ar", str(SILK_SAMPLE_RATE), "-ac", "1", "-i", str(pcm), str(dest),
        ])
        if convert.returncode != 0:
            raise VoiceError(f"silk 转码失败：{convert.stderr.strip()[:200]}")



def build_clone_audio(sample_paths: list[str], dest: str | Path) -> float:
    """把多条样本解码拼接为一条 mp3，返回拼接后时长（秒）。

    先逐条转成统一参数的 wav，再用 concat demuxer 合并，避免不同编码直接拼接
    失败（微信语音常见 amr / silk / mp3 混用）。
    """
    paths = [str(item) for item in sample_paths if item and Path(item).exists()]
    if not paths:
        raise VoiceError("没有可用的语音样本")
    if not ffmpeg_available():
        raise VoiceError("未安装 ffmpeg，无法处理音频")
    with tempfile.TemporaryDirectory() as tmp:
        wavs: list[str] = []
        for index, source in enumerate(paths):
            wav = Path(tmp) / f"{index:03d}.wav"
            _to_wav(source, wav)
            wavs.append(str(wav))
        list_file = Path(tmp) / "list.txt"
        list_file.write_text(
            "\n".join(f"file '{wav}'" for wav in wavs) + "\n", encoding="utf-8"
        )
        proc = _run([
            FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0",
            "-i", str(list_file), "-ac", "1", "-ar", str(TARGET_SAMPLE_RATE),
            "-b:a", "64k", "-t", str(CLONE_MAX_SECONDS), str(dest),
        ])
        if proc.returncode != 0:
            raise VoiceError(f"音频拼接失败：{proc.stderr.strip()[:200]}")
    return probe_duration_seconds(dest)


def _request(
    url: str,
    data: bytes,
    headers: dict[str, str],
    timeout: int,
    retries: int = 2,
) -> dict:
    last: Exception | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = ""
            try:
                body = error.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                body = ""
            if error.code in (401, 403):
                raise VoiceError("语音服务鉴权失败，请检查 MiniMax Key", auth=True) from error
            if error.code >= 500 and attempt < retries:
                last = error
                continue
            raise VoiceError(f"语音服务返回 HTTP {error.code}：{body}") from error
        except _RETRYABLE as error:  # noqa: PERF203
            if attempt < retries:
                last = error
                continue
            raise VoiceError("语音服务暂时不可用") from error
    raise VoiceError(f"语音服务调用失败：{last}")


def _check_base_resp(payload: dict) -> None:
    base = payload.get("base_resp") or {}
    code = int(base.get("status_code") or 0)
    if code == 0:
        return
    message = str(base.get("status_msg") or "语音服务调用失败")
    if code in (1004, 1002):
        raise VoiceError(f"语音服务鉴权/限流失败：{message}", auth=code == 1004)
    raise VoiceError(message)


def minimax_tts(
    text: str,
    voice_id: str,
    *,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = "speech-02-turbo",
    group_id: str = "",
    timeout: int = 30,
) -> bytes:
    """调用 T2A v2 合成 mp3 音频字节。"""
    clean = (text or "").strip()
    if not clean:
        raise VoiceError("待合成文本为空")
    if not api_key:
        raise VoiceError("平台未配置语音服务")
    url = f"{(base_url or DEFAULT_BASE_URL).rstrip('/')}/t2a_v2"
    if group_id:
        url += "?" + urllib.parse.urlencode({"GroupId": group_id})
    payload = {
        "model": model or "speech-02-turbo",
        "text": clean[:TTS_MAX_CHARS],
        "stream": False,
        "voice_setting": {"voice_id": voice_id, "speed": 1.0, "vol": 1.0, "pitch": 0},
        "audio_setting": {
            "sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1,
        },
    }
    body = json.dumps(payload).encode("utf-8")
    result = _request(
        url,
        body,
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout,
    )
    _check_base_resp(result)
    audio = (result.get("data") or {}).get("audio") or ""
    if not audio:
        raise VoiceError("语音合成未返回音频")
    if isinstance(audio, str) and audio.startswith("http"):
        with urllib.request.urlopen(audio, timeout=timeout) as response:  # noqa: S310
            return response.read()
    try:
        return bytes.fromhex(audio)
    except ValueError as error:
        raise VoiceError("语音合成返回的音频格式无法解析") from error


def _post_file(
    url: str,
    field_name: str,
    filename: str,
    content: bytes,
    api_key: str,
    timeout: int,
) -> dict:
    boundary = "----nianvoice" + os.urandom(8).hex()
    parts: list[bytes] = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        b'Content-Disposition: form-data; name="purpose"\r\n\r\nvoice_clone\r\n'
    )
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        "Content-Type: audio/mpeg\r\n\r\n".encode()
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    return _request(
        url,
        body,
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        timeout,
    )


def minimax_clone(
    sample_paths: list[str],
    *,
    api_key: str,
    voice_id: str,
    base_url: str = DEFAULT_BASE_URL,
    group_id: str = "",
    timeout: int = 120,
) -> dict:
    """用多条样本克隆音色，返回包含 voice_id 的结果。"""
    if not api_key:
        raise VoiceError("平台未配置语音服务")
    with tempfile.TemporaryDirectory() as tmp:
        merged = Path(tmp) / "clone.mp3"
        duration = build_clone_audio(sample_paths, merged)
        if duration < CLONE_MIN_SECONDS:
            raise VoiceError(f"语音样本不足，至少需要 {CLONE_MIN_SECONDS} 秒")
        data = merged.read_bytes()
        if len(data) > CLONE_MAX_BYTES:
            raise VoiceError("语音样本文件过大")
        base = (base_url or DEFAULT_BASE_URL).rstrip("/")
        upload_url = f"{base}/files/upload"
        if group_id:
            upload_url += "?" + urllib.parse.urlencode({"GroupId": group_id})
        upload = _post_file(upload_url, "file", "clone.mp3", data, api_key, timeout)
        _check_base_resp(upload)
        file_id = (upload.get("file") or {}).get("file_id")
        if not file_id:
            raise VoiceError("语音样本上传失败")
        clone_url = f"{base}/voice_clone"
        if group_id:
            clone_url += "?" + urllib.parse.urlencode({"GroupId": group_id})
        body = json.dumps({"file_id": file_id, "voice_id": voice_id}).encode("utf-8")
        result = _request(
            clone_url,
            body,
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout,
        )
        _check_base_resp(result)
    return {"voice_id": voice_id, "duration": duration}
