import tempfile
import unittest
from pathlib import Path

from ex_persona.ingest import normalize, parse_txt, select_target

SAMPLE = """2023-05-01 21:03:00 阿哲
今晚一起吃饭吗
2023-05-01 21:04:12 小鹿
不吃啦 减肥呢
2023-05-01 21:05:00 小鹿: 那随你
2023-05-01 21:06:00 小鹿: 好
"""


class IngestTest(unittest.TestCase):
    def test_parse_txt_separate_and_inline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.txt"
            path.write_text(SAMPLE, encoding="utf-8")
            items = parse_txt(path)
        self.assertEqual(len(items), 4)
        self.assertEqual(items[0]["sender"], "阿哲")
        self.assertEqual(items[0]["content"], "今晚一起吃饭吗")
        self.assertEqual(items[2]["sender"], "小鹿")
        self.assertEqual(items[2]["content"], "那随你")

    def test_select_target_by_frequency(self) -> None:
        items = parse_txt_from_text(SAMPLE)
        messages = normalize(items)
        target = select_target(messages, None)
        self.assertEqual(target, "小鹿")
        self.assertEqual(sum(1 for m in messages if m.is_target), 3)

    def test_normalize_csv_like_row(self) -> None:
        rows = [{"时间": "2023-01-01 10:00", "昵称": "小美", "内容": "在吗"},
                {"time": "2023-01-01 10:01", "sender": "我", "content": "在"}]
        messages = normalize(rows)
        self.assertEqual(messages[0].sender, "小美")
        self.assertEqual(messages[0].text, "在吗")
        self.assertEqual(messages[1].sender, "我")

    def test_normalize_is_sender(self) -> None:
        rows = [
            {"time": "t1", "IsSender": "1", "content": "hi"},
            {"time": "t2", "IsSender": "0", "content": "hello"},
        ]
        messages = normalize(rows)
        self.assertEqual(messages[0].sender, "我")
        self.assertEqual(messages[1].sender, "对方")


def parse_txt_from_text(text: str) -> list:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "chat.txt"
        path.write_text(text, encoding="utf-8")
        return parse_txt(path)


if __name__ == "__main__":
    unittest.main()
