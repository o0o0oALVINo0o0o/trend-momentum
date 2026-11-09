"""fetch.py 的純函式與分頁測試（不碰網路、不碰檔案系統）。執行：python -I -m unittest discover -s tests -v（在專案目錄）"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fetch  # noqa: E402

NS = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'


def xml(contents=(), prefixes=(), truncated=False, next_marker=None, flag=True):
    c = "".join(f"<Contents><Key>{k}</Key><Size>{s}</Size></Contents>" for k, s in contents)
    p = "".join(f"<CommonPrefixes><Prefix>{x}</Prefix></CommonPrefixes>" for x in prefixes)
    nm = f"<NextMarker>{next_marker}</NextMarker>" if next_marker else ""
    t = f'<IsTruncated>{"true" if truncated else "false"}</IsTruncated>' if flag else ""
    return f"<ListBucketResult {NS}>{t}{nm}{c}{p}</ListBucketResult>".encode()


class TestParse(unittest.TestCase):
    def test_listing(self):
        e, p, t, nm = fetch.parse_listing(xml([("k1", 5), ("k2", 7)], ["pre/"], True, "k2"))
        self.assertEqual(e, [("k1", 5), ("k2", 7)])
        self.assertEqual(p, ["pre/"])
        self.assertTrue(t)
        self.assertEqual(nm, "k2")

    def test_missing_truncated_flag_is_error(self):
        with self.assertRaises(ValueError):
            fetch.parse_listing(xml([("k", 1)], flag=False))

    def test_checksum(self):
        h = "a" * 64
        self.assertEqual(fetch.parse_checksum(f"{h}  X-1d-2026-09.zip\n", "X-1d-2026-09.zip"), h)
        self.assertEqual(fetch.parse_checksum(f"{h.upper()} *X.zip", "X.zip"), h)
        self.assertIsNone(fetch.parse_checksum(f"{h}  Y.zip", "X.zip"))
        self.assertIsNone(fetch.parse_checksum("abc  X.zip", "X.zip"))
        self.assertIsNone(fetch.parse_checksum("g" * 64 + "  X.zip", "X.zip"))
        self.assertIsNone(fetch.parse_checksum("", "X.zip"))


class TestPagination(unittest.TestCase):
    def setUp(self):
        self._orig = fetch.http_get

    def tearDown(self):
        fetch.http_get = self._orig

    def test_follows_pages_until_complete(self):
        pages = [xml([("a", 1)], truncated=True, next_marker="a"), xml([("b", 2)], truncated=True), xml([("c", 3)])]
        calls = []

        def fake(url, tries=5, timeout=60):
            calls.append(url)
            return pages[len(calls) - 1]

        fetch.http_get = fake
        entries, _, log = fetch.list_prefix("p/")
        self.assertEqual([k for k, _ in entries], ["a", "b", "c"])
        self.assertEqual(len(log), 3)
        self.assertFalse(log[-1]["truncated"])
        self.assertEqual(log[0]["marker_out"], "a")
        self.assertEqual((log[1]["first_key"], log[1]["last_key"]), ("b", "b"))
        self.assertIn("marker=a", calls[1])
        self.assertIn("marker=b", calls[2])

    def test_truncated_without_marker_raises(self):
        fetch.http_get = lambda url, tries=5, timeout=60: xml([], truncated=True)
        with self.assertRaises(RuntimeError):
            fetch.list_prefix("p/")

    def test_marker_not_advancing_raises(self):
        fetch.http_get = lambda url, tries=5, timeout=60: xml([("a", 1)], truncated=True, next_marker="a")
        with self.assertRaises(RuntimeError):
            fetch.list_prefix("p/")


class TestManifest(unittest.TestCase):
    def line(self, key, status="downloaded"):
        return json.dumps({"key": key, "status": status}) + "\n"

    def test_latest_wins_and_partial_tail(self):
        lines = [self.line("a", "http_500"), self.line("a"), self.line("b"), '{"key": "c", "sta']
        latest, bad, partial = fetch.load_manifest_lines(lines)
        self.assertEqual(latest["a"]["status"], "downloaded")
        self.assertNotIn("c", latest)
        self.assertTrue(partial)
        self.assertEqual(bad, 0)

    def test_bad_middle_line_counted(self):
        latest, bad, partial = fetch.load_manifest_lines([self.line("a"), "garbage\n", "[1]\n", self.line("b")])
        self.assertEqual(bad, 2)
        self.assertEqual(set(latest), {"a", "b"})
        self.assertFalse(partial)


class TestItemBad(unittest.TestCase):
    ITEM = {"key": "x/A-1d-2020-01.zip", "size": 10}

    def test_cases(self):
        f = fetch.item_bad
        ok = {"status": "downloaded", "sha256": "h"}
        self.assertFalse(f(self.ITEM, ok, 10, "h"))
        self.assertTrue(f(self.ITEM, ok, None, None))               # 本機沒有
        self.assertTrue(f(self.ITEM, ok, 9, "h"))                   # 截短
        self.assertTrue(f(self.ITEM, None, 10, "h"))                # manifest 沒紀錄
        self.assertTrue(f(self.ITEM, {"status": "http_500"}, 10, "h"))
        self.assertTrue(f(self.ITEM, ok, 10, "other"))              # 同大小但 SHA 與 manifest 不符（上一輪重現的案例）
        self.assertFalse(f(self.ITEM, {"status": "downloaded"}, 10, "h"))   # 舊紀錄沒有 sha 欄位時只看大小


class TestPair(unittest.TestCase):
    H = "a" * 64

    def test_cases(self):
        d = fetch.pair_decision
        good = f"{self.H}  A.zip\n"
        self.assertEqual(d(False, False, self.H, good, "A.zip"), (False, False))
        self.assertEqual(d(False, False, self.H, f"{'b' * 64}  A.zip\n", "A.zip"), (True, True))    # CHECKSUM 內容壞掉：成對重抓
        self.assertEqual(d(False, False, self.H, "garbage", "A.zip"), (True, True))                  # CHECKSUM 無法解析
        self.assertEqual(d(False, False, self.H, f"{self.H}  OTHER.zip\n", "A.zip"), (True, True))   # 檔名不符
        self.assertEqual(d(True, False, self.H, None, "A.zip"), (True, False))
        self.assertEqual(d(False, True, self.H, None, "A.zip"), (False, True))


class TestPartialTail(unittest.TestCase):
    def test_partial_then_repair_then_clean(self):
        lines = [json.dumps({"key": "a", "status": "downloaded"}) + "\n", '{"key": "b", "sta']
        latest, bad, partial = fetch.load_manifest_lines(lines)
        self.assertTrue(partial)
        complete, text = fetch.split_partial(lines)
        self.assertEqual(text, '{"key": "b", "sta')
        repaired = complete + [json.dumps({"key": "b", "status": "downloaded"}) + "\n"]     # 重建後再追加新紀錄
        latest, bad, partial = fetch.load_manifest_lines(repaired)
        self.assertEqual((bad, partial), (0, False))                 # verify 不會因舊殘行永久失敗
        self.assertEqual(set(latest), {"a", "b"})

    def test_no_partial(self):
        complete, text = fetch.split_partial(['{"key": "a"}\n'])
        self.assertIsNone(text)
        self.assertEqual(complete, ['{"key": "a"}\n'])
        self.assertEqual(fetch.split_partial([]), ([], None))


class TestMisc(unittest.TestCase):
    def test_unique(self):
        self.assertEqual(fetch.check_unique(["a", "b", "a", "a"]), ["a", "a"])
        self.assertEqual(fetch.check_unique(["a", "b"]), [])

    def test_retryable(self):
        self.assertTrue(fetch.retryable(429))
        self.assertTrue(fetch.retryable(503))
        self.assertFalse(fetch.retryable(404))
        self.assertFalse(fetch.retryable(403))

    def test_retry_after(self):
        self.assertEqual(fetch.parse_retry_after("30"), 30.0)
        self.assertEqual(fetch.parse_retry_after("300"), 300.0)              # 不截短
        self.assertIsNone(fetch.parse_retry_after(None))
        self.assertIsNone(fetch.parse_retry_after("soon"))
        # HTTP-date：Sun, 08 Nov 2026 00:00:00 GMT = epoch 1794096000
        self.assertAlmostEqual(fetch.parse_retry_after("Sun, 08 Nov 2026 00:00:00 GMT", now=1794096000 - 60), 60.0, delta=1.0)
        self.assertEqual(fetch.parse_retry_after("Sun, 08 Nov 2026 00:00:00 GMT", now=1794096000 + 100), 0.0)

    def test_backoff(self):
        for _ in range(20):
            self.assertGreaterEqual(fetch.backoff_seconds(0, retry_after=300), 300)
            self.assertLessEqual(fetch.backoff_seconds(10), 61.0)


if __name__ == "__main__":
    unittest.main()
