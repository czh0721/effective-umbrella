import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ex_persona import voice


def _make_wav(path: Path, seconds: float) -> None:
    subprocess.run(
        [
            voice.FFMPEG, "-y", "-v", "error",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-ac", "1", "-ar", "24000", str(path),
        ],
        check=True,
    )


@unittest.skipUnless(voice.ffmpeg_available(), "ffmpeg 不可用")
class VoiceTranscodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.short = cls.root / "short.wav"
        cls.first = cls.root / "a.wav"
        cls.second = cls.root / "b.wav"
        _make_wav(cls.short, 3.0)
        _make_wav(cls.first, 6.0)
        _make_wav(cls.second, 6.0)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_probe_duration(self):
        self.assertAlmostEqual(voice.probe_duration_seconds(self.short), 3.0, delta=0.3)

    def test_build_clone_audio_concatenates(self):
        dest = self.root / "merged.mp3"
        duration = voice.build_clone_audio([str(self.first), str(self.second)], dest)
        self.assertTrue(dest.exists() and dest.stat().st_size > 0)
        self.assertAlmostEqual(duration, 12.0, delta=1.0)

    def test_clone_rejects_short_samples(self):
        with self.assertRaises(voice.VoiceError):
            voice.minimax_clone(
                [str(self.short)], api_key="k", voice_id="v-test"
            )

    def test_is_silk_detects_wechat_header(self):
        silk = self.root / "sample.silk"
        silk.write_bytes(b"\x02#!SILK_V3" + b"\x00" * 32)
        self.assertTrue(voice.is_silk(silk))
        self.assertFalse(voice.is_silk(self.first))

    def test_silk_sample_decoded_via_decoder(self):
        source_pcm = self.root / "src.pcm"
        subprocess.run(
            [
                voice.FFMPEG, "-y", "-v", "error", "-f", "lavfi",
                "-i", "sine=frequency=440:duration=3", "-ac", "1",
                "-ar", "24000", "-f", "s16le", str(source_pcm),
            ],
            check=True,
        )
        fake = self.root / "fake-decoder"
        fake.write_text(f'#!/bin/sh\ncp "{source_pcm}" "$2"\n')
        fake.chmod(0o755)
        silk = self.root / "silk-sample.silk"
        silk.write_bytes(b"\x02#!SILK_V3" + b"\x00" * 32)
        with mock.patch.dict(os.environ, {"SILK_DECODER_BIN": str(fake)}):
            dest = self.root / "from-silk.mp3"
            duration = voice.build_clone_audio([str(silk)], dest)
        self.assertTrue(dest.exists() and dest.stat().st_size > 0)
        self.assertAlmostEqual(duration, 3.0, delta=0.5)


class VoiceClientTest(unittest.TestCase):
    def test_preset_mapping(self):
        self.assertEqual(voice.system_voice_id("male-1"), "male-qn-qingse")
        self.assertEqual(voice.system_voice_id(""), "female-shaonv")

    def test_active_voice_id_prefers_clone(self):
        voice_id, cloned = voice.active_voice_id(
            {"voice": {"preset": "male-1", "clone_status": "ready", "clone_voice_id": "my-voice"}}
        )
        self.assertEqual(voice_id, "my-voice")
        self.assertTrue(cloned)

    def test_active_voice_id_falls_back_to_preset(self):
        voice_id, cloned = voice.active_voice_id({"voice": {"preset": "male-2"}})
        self.assertEqual(voice_id, "male-qn-jingying")
        self.assertFalse(cloned)

    def test_tts_returns_audio_bytes(self):
        payload = {"data": {"audio": "494433"}, "base_resp": {"status_code": 0}}
        with mock.patch.object(voice, "_request", return_value=payload) as mocked:
            audio = voice.minimax_tts("你好", "female-shaonv", api_key="k")
        self.assertEqual(audio, b"ID3")
        body = json.loads(mocked.call_args.args[1].decode())
        self.assertEqual(body["audio_setting"]["format"], "mp3")

    def test_tts_empty_text(self):
        with self.assertRaises(voice.VoiceError):
            voice.minimax_tts("   ", "female-shaonv", api_key="k")

    def test_tts_auth_error(self):
        with mock.patch.object(
            voice, "_request", side_effect=voice.VoiceError("bad", auth=True)
        ):
            with self.assertRaises(voice.VoiceError) as ctx:
                voice.minimax_tts("你好", "female-shaonv", api_key="k")
        self.assertTrue(ctx.exception.auth)

    def test_clone_flow(self):
        sample = Path(tempfile.mkdtemp()) / "sample.wav"
        if not voice.ffmpeg_available():
            self.skipTest("ffmpeg 不可用")
        _make_wav(sample, 11.0)
        with mock.patch.object(
            voice, "_post_file", return_value={"file": {"file_id": "f-1"}, "base_resp": {"status_code": 0}}
        ) as upload, mock.patch.object(
            voice, "_request", return_value={"base_resp": {"status_code": 0}}
        ) as request:
            result = voice.minimax_clone([str(sample)], api_key="k", voice_id="v-1")
        self.assertEqual(result["voice_id"], "v-1")
        self.assertTrue(upload.called and request.called)


class DoubaoProviderTest(unittest.TestCase):
    def test_preset_mapping_is_provider_specific(self):
        self.assertEqual(
            voice.system_voice_id("female-2", "doubao"),
            "zh_female_gaolengyujie_uranus_bigtts",
        )
        self.assertEqual(voice.system_voice_id("male-1", "minimax"), "male-qn-qingse")
        self.assertNotEqual(
            voice.system_voice_id("female-1", "doubao"),
            voice.system_voice_id("female-1", "minimax"),
        )

    def test_resource_id_derivation(self):
        self.assertEqual(voice.doubao_resource_id("custom_voice", is_clone=True), "seed-icl-2.0")
        self.assertEqual(voice.doubao_resource_id("S_abc"), "seed-icl-2.0")
        self.assertEqual(
            voice.doubao_resource_id("zh_female_vv_uranus_bigtts"), "seed-tts-2.0"
        )
        self.assertEqual(voice.doubao_resource_id("zh_female_cancan_mars_bigtts"), "seed-tts-1.0")
        self.assertEqual(
            voice.doubao_resource_id("x", override="custom.resource"), "custom.resource"
        )

    def test_tts_concatenates_streamed_chunks(self):
        import base64 as b64

        raw = "\n".join([
            json.dumps({"code": 0, "data": b64.b64encode(b"ID3").decode()}),
            json.dumps({"code": 0, "data": b64.b64encode(b"tail").decode()}),
        ])
        with mock.patch.object(voice, "_doubao_post", return_value=raw) as posted:
            audio = voice.doubao_tts(
                "你好", "zh_female_vv_uranus_bigtts", app_id="a", access_token="t"
            )
        self.assertEqual(audio, b"ID3tail")
        headers = posted.call_args.args[2]
        self.assertEqual(headers["X-Api-Resource-Id"], "seed-tts-2.0")
        self.assertEqual(headers["X-Api-App-Key"], "a")
        self.assertNotIn("X-Api-Key", headers)
        payload = posted.call_args.args[1]
        self.assertEqual(payload["req_params"]["speaker"], "zh_female_vv_uranus_bigtts")

    def test_tts_prefers_api_key_header(self):
        raw = json.dumps({"code": 0, "data": base64_id3()})
        with mock.patch.object(voice, "_doubao_post", return_value=raw) as posted:
            voice.doubao_tts("hi", "S_x", api_key="k", is_clone=True)
        headers = posted.call_args.args[2]
        self.assertEqual(headers["X-Api-Key"], "k")
        self.assertEqual(headers["X-Api-Resource-Id"], "seed-icl-2.0")

    def test_tts_reports_error_code(self):
        raw = json.dumps({"code": 55000000, "message": "resource ID is mismatched"})
        with mock.patch.object(voice, "_doubao_post", return_value=raw):
            with self.assertRaises(voice.VoiceError):
                voice.doubao_tts("hi", "x", app_id="a", access_token="t")

    def test_tts_requires_credentials(self):
        with self.assertRaises(voice.VoiceError):
            voice.doubao_tts("hi", "x")

    @unittest.skipUnless(voice.ffmpeg_available(), "ffmpeg 不可用")
    def test_clone_posts_custom_speaker(self):
        sample = Path(tempfile.mkdtemp()) / "sample.wav"
        _make_wav(sample, 11.0)
        with mock.patch.object(voice, "_doubao_post", return_value='{"code":0}') as posted:
            result = voice.doubao_clone(
                [str(sample)], voice_id="nianabc123", app_id="a", access_token="t"
            )
        self.assertEqual(result["voice_id"], "nianabc123")
        payload = posted.call_args.args[1]
        self.assertEqual(payload["speaker_id"], "custom_speaker_id")
        self.assertEqual(payload["custom_speaker_id"], "nianabc123")
        self.assertTrue(payload["audio"]["data"])

    def test_synthesize_dispatches_by_provider(self):
        creds = voice.VoiceCredentials(provider="minimax", minimax_api_key="mm")
        with mock.patch.object(
            voice, "minimax_tts", return_value=b"ID3"
        ) as minimax:
            audio = voice.synthesize("hi", "female-shaonv", creds)
        self.assertEqual(audio, b"ID3")
        self.assertEqual(minimax.call_args.args[1], "female-shaonv")

    def test_synthesize_requires_ready_credentials(self):
        creds = voice.VoiceCredentials(provider="doubao")
        with self.assertRaises(voice.VoiceError):
            voice.synthesize("hi", "x", creds)

    def test_credentials_from_config_object(self):
        class Cfg:
            voice_provider = "doubao"
            doubao_api_key = "dk"
            doubao_app_id = "app"
            minimax_api_key = ""

        creds = voice.credentials(Cfg())
        self.assertEqual(creds.provider, "doubao")
        self.assertTrue(creds.ready)


def base64_id3() -> str:
    import base64 as b64

    return b64.b64encode(b"ID3").decode()


if __name__ == "__main__":
    unittest.main()
