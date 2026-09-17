import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ex_persona import ingest, sources


def apple_ns(text: str) -> int:
    stamp = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int((stamp.timestamp() - 978307200) * 1e9)


class SmsXmlTest(unittest.TestCase):
    def test_parse_received_and_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sms.xml"
            path.write_text(
                '<?xml version="1.0"?>'
                '<smses count="2">'
                '<sms address="+8613800000000" date="1714557600000" type="1" body="在干嘛"/>'
                '<sms address="+8613800000000" date="1714557660000" type="2" body="在忙"/>'
                "</smses>",
                encoding="utf-8",
            )
            items = sources.parse_sms_xml(path)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["sender"], "+8613800000000")
        self.assertEqual(items[0]["content"], "在干嘛")
        self.assertEqual(items[1]["sender"], "我")


class ImessageTest(unittest.TestCase):
    def test_parse_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "chat.db"
            con = sqlite3.connect(db)
            con.executescript(
                "CREATE TABLE handle (id TEXT, ROWID INTEGER PRIMARY KEY);"
                "CREATE TABLE message (date INTEGER, is_from_me INTEGER, text TEXT, handle_id INTEGER);"
            )
            con.execute("INSERT INTO handle (id) VALUES ('+8613800000000')")
            con.execute(
                "INSERT INTO message VALUES (?, 0, '你好', 1)",
                (apple_ns("2024-05-01 10:00:00"),),
            )
            con.execute(
                "INSERT INTO message VALUES (?, 1, '在呢', 1)",
                (apple_ns("2024-05-01 10:01:00"),),
            )
            con.commit()
            con.close()
            items = sources.parse_imessage_sqlite(db)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["sender"], "+8613800000000")
        self.assertEqual(items[0]["content"], "你好")
        self.assertEqual(items[1]["sender"], "我")
        self.assertTrue(items[0]["time"].startswith("2024-05-01"))

    def test_reject_non_imessage_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "other.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE foo (a TEXT)")
            con.commit()
            con.close()
            with self.assertRaises(ValueError):
                sources.parse_imessage_sqlite(db)


class SocialJsonTest(unittest.TestCase):
    def test_collect_posts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "weibo.json"
            path.write_text(
                '[{"text":"今天天气真好 <a>链接</a>","created_at":"2024-05-01",'
                '"user":{"screen_name":"小鹿"}},'
                '{"text":"今天天气真好 <a>链接</a>","created_at":"2024-05-01"}]',
                encoding="utf-8",
            )
            items = sources.parse_social_json(path)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["subject"])
        self.assertEqual(items[0]["kind"], "post")
        self.assertNotIn("<a>", items[0]["content"])


class ImageTest(unittest.TestCase):
    def test_sidecar_caption(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("pillow 未安装")
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "photo.jpg"
            Image.new("RGB", (6, 6), (200, 180, 160)).save(photo)
            Path(tmp, "photo.txt").write_text("那天在海边拍的", encoding="utf-8")
            items = sources.parse_image(photo)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["subject"])
        self.assertEqual(items[0]["kind"], "photo")
        self.assertIn("海边", items[0]["content"])

    def test_plain_image_skipped(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("pillow 未安装")
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "bare.png"
            Image.new("RGB", (6, 6)).save(photo)
            self.assertEqual(sources.parse_image(photo), [])


class TextBlobTest(unittest.TestCase):
    def test_chat_blob(self):
        blob = "2024-05-01 20:00:00 小鹿\n在干嘛\n2024-05-01 20:01:00 我\n在想你"
        items = ingest.parse_text_blob(blob)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["sender"], "小鹿")

    def test_plain_prose_becomes_post(self):
        items = ingest.parse_text_blob("她喜欢在句尾加波浪号～")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["subject"])


class EndToEndTest(unittest.TestCase):
    def test_run_with_social_and_sms(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "raw"
            root.mkdir()
            (root / "weibo.json").write_text(
                '[{"text":"喜欢夜里散步","created_at":"2024-06-01"}]', encoding="utf-8"
            )
            (root / "sms.xml").write_text(
                '<smses><sms address="小鹿" date="1714557600000" type="1" body="晚安"/>'
                '<sms address="小鹿" date="1714557660000" type="2" body="晚安"/>'
                "</smses>",
                encoding="utf-8",
            )
            out = Path(tmp) / "profile"
            result = ingest.run(root, out, "小鹿")
            self.assertEqual(result.target, "小鹿")
            messages = ingest.read_messages(out / "messages.jsonl")
            subject = [m for m in messages if m.subject]
            self.assertEqual(len(subject), 1)
            self.assertTrue(subject[0].is_target)
            meta = __import__("json").loads((out / "ingest_meta.json").read_text(encoding="utf-8"))
            self.assertIn("post", meta["kinds"])

    def test_run_with_pasted_text_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "profile"
            result = ingest.run(None, out, "小鹿", extra_text="2024-05-01 20:00:00 小鹿\n晚安")
            self.assertEqual(result.target, "小鹿")
            self.assertEqual(len(result.messages), 1)
            self.assertTrue(result.messages[0].is_target)


if __name__ == "__main__":
    unittest.main()
