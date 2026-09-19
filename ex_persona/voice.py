"""语音合成与音色克隆（MiniMax / 豆包），以及本地音频转码。

微信语音样本可能来自不同编码容器，而语音克隆服务只接受 mp3 / m4a / wav 等常见
格式且对时长有要求。这里统一用 ffmpeg 把多条样本解码、拼接并转成单条 mp3，再交给
当前提供商（MiniMax 或豆包）。TTS 直接产出 mp3 音频字节。

提供商由平台配置选择，:func:`synthesize` 与 :func:`clone` 负责分派。
"""

import base64
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
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

# 豆包（火山引擎）语音接口。
DOUBAO_DEFAULT_BASE_URL = "https://openspeech.bytedance.com"
DOUBAO_TTS_PATH = "/api/v3/tts/unidirectional"
DOUBAO_CLONE_PATH = "/api/v3/tts/voice_clone"
# 资源 ID：复刻音色、语音合成 2.0 / 1.0。
DOUBAO_RESOURCE_ICL = "seed-icl-2.0"
DOUBAO_RESOURCE_TTS_2 = "seed-tts-2.0"
DOUBAO_RESOURCE_TTS_1 = "seed-tts-1.0"

# 前端预设音色映射到各提供商的系统音色 id。
PRESET_VOICES_MINIMAX = {
    "female-1": "female-shaonv",
    "female-2": "female-yujie",
    "male-1": "male-qn-qingse",
    "male-2": "male-qn-jingying",
}
PRESET_VOICES_DOUBAO = {
    "female-1": "zh_female_shuangkuaisisi_uranus_bigtts",
    "female-2": "zh_female_gaolengyujie_uranus_bigtts",
    "male-1": "zh_male_taocheng_uranus_bigtts",
    "male-2": "zh_male_ruyayichen_uranus_bigtts",
}
PRESET_VOICES = PRESET_VOICES_MINIMAX
PRESET_VOICES_BY_PROVIDER = {
    "minimax": PRESET_VOICES_MINIMAX,
    "doubao": PRESET_VOICES_DOUBAO,
}
PROVIDERS = ("minimax", "doubao")
DEFAULT_PROVIDER = "minimax"
DEFAULT_PRESET = "female-1"

_RETRYABLE = (urllib.error.URLError, TimeoutError, ConnectionError)


class VoiceError(RuntimeError):
    """语音服务调用失败。``auth`` 为真表示 Key 无效或没有权限。"""

    def __init__(self, message: str, *, auth: bool = False) -> None:
        super().__init__(message)
        self.auth = auth


def preset_table(provider: str = DEFAULT_PROVIDER) -> dict[str, str]:
    return PRESET_VOICES_BY_PROVIDER.get(provider or DEFAULT_PROVIDER, PRESET_VOICES_MINIMAX)


def preset_voice_ids(provider: str = DEFAULT_PROVIDER) -> set[str]:
    return set(preset_table(provider).values())


def system_voice_id(preset: str, provider: str = DEFAULT_PROVIDER) -> str:
    table = preset_table(provider)
    return table.get(preset or "", table[DEFAULT_PRESET])


def active_voice_id(settings: dict, provider: str = DEFAULT_PROVIDER) -> tuple[str, bool]:
    """返回 (voice_id, 是否为复刻音色)，按当前提供商解析预设音色。"""
    voice = (settings or {}).get("voice") or {}
    clone_id = str(voice.get("clone_voice_id") or "").strip()
    if voice.get("clone_status") == "ready" and clone_id:
        return clone_id, True
    return system_voice_id(str(voice.get("preset") or voice.get("voice") or ""), provider), False


@dataclass
class VoiceCredentials:
    """当前提供商的一组语音凭据与端点配置。"""

    provider: str = DEFAULT_PROVIDER
    minimax_api_key: str = ""
    minimax_base_url: str = DEFAULT_BASE_URL
    minimax_group_id: str = ""
    doubao_api_key: str = ""
    doubao_app_id: str = ""
    doubao_access_token: str = ""
    doubao_base_url: str = DOUBAO_DEFAULT_BASE_URL
    doubao_resource_id: str = ""

    @property
    def ready(self) -> bool:
        if self.provider == "doubao":
            return bool(self.doubao_api_key) or bool(
                self.doubao_app_id and self.doubao_access_token
            )
        return bool(self.minimax_api_key)


def credentials(config: object) -> VoiceCredentials:
    """从平台配置对象（或其等价的字典/命名空间）构建凭据。"""
    def field(name: str, default: str = "") -> str:
        value = getattr(config, name, None)
        if value is None and isinstance(config, dict):
            value = config.get(name)
        return str(value if value is not None else default).strip()

    provider = field("voice_provider", DEFAULT_PROVIDER).lower() or DEFAULT_PROVIDER
    return VoiceCredentials(
        provider=provider,
        minimax_api_key=field("minimax_api_key"),
        minimax_base_url=(os.getenv("PERSONA_MINIMAX_BASE_URL") or DEFAULT_BASE_URL).strip(),
        minimax_group_id=(os.getenv("PERSONA_MINIMAX_GROUP_ID") or "").strip(),
        doubao_api_key=field("doubao_api_key"),
        doubao_app_id=field("doubao_app_id"),
        doubao_access_token=field("doubao_access_token"),
        doubao_base_url=(os.getenv("PERSONA_DOUBAO_BASE_URL") or DOUBAO_DEFAULT_BASE_URL).strip(),
        doubao_resource_id=field("doubao_resource_id"),
    )


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


# --------------------------------------------------------------------------- #
# 豆包（火山引擎）
# --------------------------------------------------------------------------- #

def doubao_resource_id(voice_id: str, *, is_clone: bool = False, override: str = "") -> str:
    """按音色推断豆包资源 ID，显式覆盖优先。"""
    if override:
        return override
    if is_clone:
        return DOUBAO_RESOURCE_ICL
    if voice_id.startswith("S_"):
        return DOUBAO_RESOURCE_ICL
    if "_uranus_" in voice_id or voice_id.startswith("saturn_"):
        return DOUBAO_RESOURCE_TTS_2
    return DOUBAO_RESOURCE_TTS_1


def _doubao_headers(
    *, api_key: str = "", app_id: str = "", access_token: str = ""
) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    elif app_id and access_token:
        headers["X-Api-App-Key"] = app_id
        headers["X-Api-Access-Key"] = access_token
    else:
        raise VoiceError("平台未配置语音服务")
    headers["X-Api-Request-Id"] = uuid.uuid4().hex
    return headers


def _doubao_error(payload: dict, context: str) -> None:
    code = payload.get("code")
    try:
        code_int = int(code) if code is not None else 0
    except (TypeError, ValueError):
        code_int = 0
    if code_int in (0, 20000000):
        return
    message = str(payload.get("message") or payload.get("msg") or "").strip()
    auth = code_int in (401, 403, 100004, 100005)
    raise VoiceError(f"{context}：{message or code}", auth=auth)


def _doubao_post(
    url: str, payload: dict, headers: dict[str, str], timeout: int, retries: int = 2
) -> str:
    """POST JSON 并返回原始响应文本（V3 为分块拼接的多个 JSON）。"""
    body = json.dumps(payload).encode("utf-8")
    last: Exception | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = error.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                detail = ""
            if error.code in (401, 403):
                raise VoiceError("豆包语音鉴权失败，请检查 AppID 与 Access Token", auth=True) from error
            if error.code >= 500 and attempt < retries:
                last = error
                continue
            raise VoiceError(f"豆包语音返回 HTTP {error.code}：{detail}") from error
        except _RETRYABLE as error:  # noqa: PERF203
            if attempt < retries:
                last = error
                continue
            raise VoiceError("豆包语音暂时不可用") from error
    raise VoiceError(f"豆包语音调用失败：{last}")


def _iter_json_objects(text: str):
    decoder = json.JSONDecoder()
    index, length = 0, len(text)
    while index < length:
        while index < length and text[index] in " \r\n\t":
            index += 1
        if index < length and text.startswith("data:", index):
            index += 5
            continue
        if index >= length:
            break
        try:
            obj, end = decoder.raw_decode(text, index)
        except ValueError:
            break
        index = end
        if isinstance(obj, dict):
            yield obj


def _doubao_audio(raw: str) -> bytes:
    audio = bytearray()
    last_error: dict | None = None
    for obj in _iter_json_objects(raw):
        if obj.get("code") not in (None, 0, "0", 20000000, "20000000"):
            last_error = obj
            continue
        data = obj.get("data")
        if isinstance(data, str) and data:
            try:
                audio.extend(base64.b64decode(data))
            except (ValueError, TypeError):
                continue
    if last_error is not None and not audio:
        _doubao_error(last_error, "豆包语音合成失败")
    if not audio:
        raise VoiceError("豆包语音合成未返回音频")
    return bytes(audio)


def doubao_tts(
    text: str,
    voice_id: str,
    *,
    api_key: str = "",
    app_id: str = "",
    access_token: str = "",
    base_url: str = DOUBAO_DEFAULT_BASE_URL,
    resource_id: str = "",
    is_clone: bool = False,
    timeout: int = 60,
) -> bytes:
    """调用豆包 V3 非流式合成接口，返回 mp3 音频字节。"""
    clean = (text or "").strip()
    if not clean:
        raise VoiceError("待合成文本为空")
    headers = _doubao_headers(api_key=api_key, app_id=app_id, access_token=access_token)
    headers["X-Api-Resource-Id"] = doubao_resource_id(
        voice_id, is_clone=is_clone, override=resource_id
    )
    payload = {
        "req_params": {
            "text": clean[:TTS_MAX_CHARS],
            "speaker": voice_id,
            "audio_params": {"format": "mp3", "sample_rate": 24000},
        }
    }
    url = (base_url or DOUBAO_DEFAULT_BASE_URL).rstrip("/") + DOUBAO_TTS_PATH
    raw = _doubao_post(url, payload, headers, timeout)
    return _doubao_audio(raw)


def doubao_clone(
    sample_paths: list[str],
    *,
    voice_id: str,
    api_key: str = "",
    app_id: str = "",
    access_token: str = "",
    base_url: str = DOUBAO_DEFAULT_BASE_URL,
    resource_id: str = "",
    timeout: int = 120,
) -> dict:
    """用多条样本在豆包注册复刻音色，返回包含 voice_id 的结果。"""
    headers = _doubao_headers(api_key=api_key, app_id=app_id, access_token=access_token)
    headers["X-Api-Resource-Id"] = resource_id or DOUBAO_RESOURCE_ICL
    with tempfile.TemporaryDirectory() as tmp:
        merged = Path(tmp) / "clone.mp3"
        duration = build_clone_audio(sample_paths, merged)
        if duration < CLONE_MIN_SECONDS:
            raise VoiceError(f"语音样本不足，至少需要 {CLONE_MIN_SECONDS} 秒")
        data = merged.read_bytes()
        if len(data) > CLONE_MAX_BYTES:
            raise VoiceError("语音样本文件过大")
        payload = {
            "speaker_id": "custom_speaker_id",
            "custom_speaker_id": voice_id,
            "audio": {"data": base64.b64encode(data).decode("ascii"), "format": "mp3"},
        }
        url = (base_url or DOUBAO_DEFAULT_BASE_URL).rstrip("/") + DOUBAO_CLONE_PATH
        raw = _doubao_post(url, payload, headers, timeout)
    result: dict = {}
    for obj in _iter_json_objects(raw):
        result = obj
        break
    _doubao_error(result, "豆包音色复刻失败")
    return {"voice_id": voice_id, "duration": duration, "provider": "doubao"}


# --------------------------------------------------------------------------- #
# 提供商分派
# --------------------------------------------------------------------------- #

def synthesize(
    text: str,
    voice_id: str,
    creds: VoiceCredentials,
    *,
    model: str = "",
    is_clone: bool = False,
    timeout: int = 60,
) -> bytes:
    """按凭据里的提供商合成语音。"""
    if not creds.ready:
        raise VoiceError("平台未配置语音服务")
    if creds.provider == "doubao":
        return doubao_tts(
            text,
            voice_id,
            api_key=creds.doubao_api_key,
            app_id=creds.doubao_app_id,
            access_token=creds.doubao_access_token,
            base_url=creds.doubao_base_url,
            resource_id=creds.doubao_resource_id,
            is_clone=is_clone,
            timeout=timeout,
        )
    return minimax_tts(
        text,
        voice_id,
        api_key=creds.minimax_api_key,
        base_url=creds.minimax_base_url,
        model=model or "speech-02-turbo",
        group_id=creds.minimax_group_id,
        timeout=timeout,
    )


def clone(
    sample_paths: list[str],
    voice_id: str,
    creds: VoiceCredentials,
    *,
    timeout: int = 120,
) -> dict:
    """按凭据里的提供商克隆音色。"""
    if not creds.ready:
        raise VoiceError("平台未配置语音服务")
    if creds.provider == "doubao":
        return doubao_clone(
            sample_paths,
            voice_id=voice_id,
            api_key=creds.doubao_api_key,
            app_id=creds.doubao_app_id,
            access_token=creds.doubao_access_token,
            base_url=creds.doubao_base_url,
            resource_id=creds.doubao_resource_id,
            timeout=timeout,
        )
    return minimax_clone(
        sample_paths,
        api_key=creds.minimax_api_key,
        voice_id=voice_id,
        base_url=creds.minimax_base_url,
        group_id=creds.minimax_group_id,
        timeout=timeout,
    )
