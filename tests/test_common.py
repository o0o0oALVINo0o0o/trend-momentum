"""common.py 的純函式測試（不碰網路、不碰檔案系統）。執行：python -I -m unittest discover -s tests -v（在專案目錄）"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common  # noqa: E402

K = "data/futures/um/monthly/klines/"
F = "data/futures/um/monthly/fundingRate/"
D0 = 1577836800000          # 2020-01-01 00:00 UTC
DAY = 86_400_000
HEADER = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore"


def krow(t, o=10.0, h=12.0, l=9.0, c=11.0, qv=1000.0):
    return f"{t},{o},{h},{l},{c},5,{t + DAY - 1},{qv},10,1,1,0"


class TestSymbol(unittest.TestCase):
    def test_keep(self):
        for s in ("BTCUSDT", "1000PEPEUSDT", "LUNAUSDT", "币安人生USDT", "哈基米USDT"):
            self.assertTrue(common.keep_symbol(s), s)

    def test_drop(self):
        for s in ("BTCUSDC", "BTCUSDT_250627", "USDCUSDT", "FDUSDUSDT", "USDEUSDT", "EURUSDT", "AEURUSDT",
                  "BTCBUSD", "1000LUNCBUSD", "BT,CUSDT", "BT CUSDT", "BT/CUSDT", "USDT"):
            self.assertFalse(common.keep_symbol(s), s)


class TestKeys(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(common.parse_key(K + "BTCUSDT/1d/BTCUSDT-1d-2026-10.zip"),
                         {"kind": "kline", "symbol": "BTCUSDT", "month": "2026-10", "checksum": False})
        self.assertEqual(common.parse_key(F + "BTCUSDT/BTCUSDT-fundingRate-2020-01.zip.CHECKSUM"),
                         {"kind": "fund", "symbol": "BTCUSDT", "month": "2020-01", "checksum": True})

    def test_strict_rejections(self):
        bad = [K + "BTCUSDT/1d/BTCUSDT-1d-2021-13.zip", K + "BTCUSDT/1d/BTCUSDT-1d-2021-00.zip",
               K + "BTCUSDT/1h/BTCUSDT-1h-2021-01.zip", K + "BTCUSDT/1d/ETHUSDT-1d-2021-01.zip",
               K + "BTCUSDT/1d/extra/BTCUSDT-1d-2021-01.zip", K + "BTCUSDT/1d/BTCUSDT-1d-2021-01.csv",
               K + "BTCUSDT/1d/BTCUSDT-1d-2021-1.zip", F + "BTCUSDT/1d/BTCUSDT-fundingRate-2021-01.zip",
               F + "BTCUSDT/BTCUSDT-1d-2021-01.zip", "other/BTCUSDT-1d-2021-01.zip"]
        for k in bad:
            self.assertIsNone(common.parse_key(k), k)

    def test_range(self):
        self.assertTrue(common.in_range("2020-01"))
        self.assertTrue(common.in_range("2026-10"))
        self.assertFalse(common.in_range("2019-12"))
        self.assertFalse(common.in_range("2026-11"))
        self.assertFalse(common.in_range("2021-13"))
        self.assertFalse(common.in_range(None))


class TestCoverage(unittest.TestCase):
    def test_clean(self):
        ms = ["2025-09", "2025-10", "2025-11", "2025-12"] + ["2026-%02d" % i for i in range(1, 11)]
        self.assertEqual(common.coverage(ms, ms, ms, ms), [])

    def test_flags(self):
        full = ["2026-%02d" % i for i in range(1, 11)]
        f = common.coverage(["2026-01", "2026-03"], ["2026-01", "2026-03", "2026-05"], [], [])
        self.assertIn("kline:gap:2026-02", f)
        self.assertIn("kline:orphan_checksum:2026-05", f)
        self.assertIn("kline:ends_early:2026-03", f)
        self.assertIn("fund:empty", f)
        f2 = common.coverage(full, full[:-1], full, full)
        self.assertIn("kline:zip_without_checksum:2026-10", f2)
        f3 = common.coverage(full, full, [], ["2026-01"])
        self.assertIn("fund:empty", f3)
        self.assertIn("fund:orphan_checksum_only", f3)


class TestKlineParse(unittest.TestCase):
    def test_ok_with_header(self):
        text = HEADER + "\n" + krow(D0) + "\n" + krow(D0 + DAY)
        rows, errs, n_raw = common.parse_kline_csv(text, "2020-01")
        self.assertEqual(errs, [])
        self.assertEqual(n_raw, 2)
        self.assertEqual([r[0] for r in rows], [D0, D0 + DAY])
        self.assertEqual(rows[0][5], 1000.0)

    def test_ok_without_header_blank_lines_and_bom(self):
        text = "﻿\n\n" + krow(D0) + "\n\n" + krow(D0 + DAY) + "\n"
        rows, errs, n_raw = common.parse_kline_csv(text, "2020-01")
        self.assertEqual((len(rows), errs, n_raw), (2, [], 2))
        text = "\n\n" + HEADER + "\n" + krow(D0)               # 標頭前有空行
        rows, errs, _ = common.parse_kline_csv(text, "2020-01")
        self.assertEqual((len(rows), errs), (1, []))

    def test_broken_first_row_is_error_not_header(self):
        text = "BROKEN,1,2,3,4,5,6,7,8,9,10,0\n" + krow(D0)
        rows, errs, n_raw = common.parse_kline_csv(text, "2020-01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(n_raw, 2)
        self.assertTrue(any("time" in e for e in errs), errs)

    def test_errors(self):
        cases = {
            "duplicate": krow(D0) + "\n" + krow(D0),
            "ohlc": krow(D0, h=9.5),
            "nonpositive": krow(D0, l=0.0, h=12.0),
            "nonfinite": krow(D0, c="nan"),
            "time_range": krow(D0 - DAY),
            "columns": "1,2,3",
            "order": krow(D0 + DAY) + "\n" + krow(D0),
        }
        for why, text in cases.items():
            _, errs, _ = common.parse_kline_csv(text, "2020-01")
            self.assertTrue(any(why in e for e in errs), (why, errs))

    def test_eleven_columns_rejected(self):
        _, errs, _ = common.parse_kline_csv(",".join(krow(D0).split(",")[:11]), "2020-01")
        self.assertTrue(any("columns" in e for e in errs))

    def test_misaligned_time_and_negative_volume(self):
        _, errs, _ = common.parse_kline_csv(krow(D0 + 1), "2020-01")
        self.assertTrue(any("time_range" in e for e in errs))
        _, errs, _ = common.parse_kline_csv(krow(D0, qv=-1.0), "2020-01")
        self.assertTrue(any("nonpositive" in e for e in errs))

    def test_bad_rows_are_not_returned(self):
        rows, errs, n_raw = common.parse_kline_csv(krow(D0) + "\n" + krow(D0 + DAY, h=1.0), "2020-01")
        self.assertEqual((len(rows), len(errs), n_raw), (1, 1, 2))


class TestFundingParse(unittest.TestCase):
    def test_ok(self):
        text = "calc_time,funding_interval_hours,last_funding_rate\n" + f"{D0 + 5},8,0.0001\n" + f"{D0 + 28800000},8,-0.0002\n"
        rows, errs, n_raw = common.parse_funding_csv(text, "2020-01")
        self.assertEqual((errs, n_raw), ([], 2))
        self.assertEqual(rows[0][0], D0 + 5)                     # 原始毫秒保留，不四捨五入
        self.assertEqual(rows[1][2], -0.0002)

    def test_errors(self):
        rows, errs, n_raw = common.parse_funding_csv(
            f"{D0},8,0.1\n{D0},8,0.2\n{D0 + 1},8,inf\n{D0 - 1},8,0.1\n{D0 + 2},0,0.1\n{D0 + 3},8\n", "2020-01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(errs), 5)
        self.assertEqual(n_raw, 6)

    def test_order_and_broken_first_row(self):
        _, errs, _ = common.parse_funding_csv(f"{D0 + 10},8,0.1\n{D0 + 5},8,0.1\n", "2020-01")
        self.assertTrue(any("order" in e for e in errs))
        rows, errs, _ = common.parse_funding_csv(f"BROKEN,8,0.1\n{D0},8,0.1\n", "2020-01")
        self.assertEqual(len(rows), 1)
        self.assertTrue(any("time" in e for e in errs))


if __name__ == "__main__":
    unittest.main()
