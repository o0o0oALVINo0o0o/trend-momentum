"""core.py 與 analyze.py 判定函式的合成資料測試（不碰網路、不碰檔案系統）。
執行（在專案目錄）：python -I -m unittest discover -s tests -v"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import analyze  # noqa: E402
import core  # noqa: E402
from core import D0, DAY, TOTAL  # noqa: E402

BIG = 1_000_000_000.0        # 日成交額 10 億（≥100M 層）


def mk(closes, qv=BIG, ph_from=None, drop_days=(), lows=None, highs=None, f_rate=0.0001, f_drop_days=(), name="AAA"):
    """產生 Sym。closes: 長度 ≤ TOTAL 的收盤價列表（開盤 = 收盤）；ph_from: 此日起為占位日（凍結價、成交量 0）。
    資金費：每日 3 筆（00:00:00.005 起每 8 小時），涵蓋整個期間。"""
    n = len(closes)
    rows = []
    for i in range(n):
        if i in drop_days:
            continue
        c = closes[i]
        v = qv(i) if callable(qv) else qv
        h = c * 1.01 if highs is None or highs.get(i) is None else highs[i]
        l = c * 0.99 if lows is None or lows.get(i) is None else lows[i]
        if ph_from is not None and i >= ph_from:
            c = closes[ph_from - 1]
            rows.append((D0 + i * DAY, c, c, c, c, 0.0))
        else:
            rows.append((D0 + i * DAY, c, h, l, c, v))
    f_rows = []
    for d in range(TOTAL):
        if d in f_drop_days:
            continue
        for k in range(3):
            f_rows.append((D0 + d * DAY + k * 8 * 3_600_000 + 5, 8, f_rate))
    return core.Sym(name, rows, f_rows)


def rising(n=300, flat=40, step=1.0):
    return [100.0 + max(0, i - flat + 1) * step for i in range(n)]


class TestSym(unittest.TestCase):
    def test_placeholder_and_derived(self):
        s = mk(rising(100), ph_from=70)
        self.assertFalse(s.ph[69])
        self.assertTrue(s.ph[70] and s.ph[99])
        self.assertTrue(s.tradable[69] and not s.tradable[70])
        self.assertEqual(s.first_trad, 0)
        self.assertEqual(s.next_ph[10], 70)
        self.assertEqual(s.next_ph[70], 71)
        self.assertGreater(s.next_ph[99], 10 ** 8 - 1)
        self.assertAlmostEqual(s.avg30[29], BIG)
        self.assertTrue(math.isnan(s.avg30[28]))

    def test_frozen_price_with_volume_is_not_placeholder(self):
        closes = [100.0] * 100
        rows = [(D0 + i * DAY, 100.0, 100.0, 100.0, 100.0, 5e6) for i in range(100)]
        s = core.Sym("X", rows, [])
        self.assertFalse(any(s.ph))

    def test_mid_zero_volume_nonfrozen_is_ordinary(self):
        rows = [(D0 + i * DAY, 100.0 + i, 101.0 + i, 99.0 + i, 100.0 + i, 0.0 if i == 10 else 1e9) for i in range(40)]
        s = core.Sym("X", rows, [])
        self.assertFalse(s.ph[10])
        self.assertTrue(math.isnan(s.avg30[39]) is False)

    def test_missing_day_breaks_avg30_and_data_ok(self):
        s = mk(rising(100), drop_days=(50,))
        self.assertTrue(math.isnan(s.avg30[60]))
        self.assertFalse(s.data_ok(45, 55))
        self.assertTrue(s.data_ok(51, 60))

    def test_fund_ok_is_interval_based(self):
        s = mk(rising(100))
        self.assertTrue(s.fund_ok(42, 60))
        gap = mk(rising(100), f_drop_days=(50,))
        self.assertFalse(gap.fund_ok(42, 60))                 # 區間內缺一整天
        self.assertTrue(gap.fund_ok(5, 20))                    # 區間外的缺口不影響
        self.assertTrue(gap.fund_ok(51, 60))
        self.assertTrue(gap.fund_ok(42, 50))                   # .005 偏移：出場時刻附近缺的那筆晚於出場，屬下一部位，不影響本交易
        self.assertTrue(gap.fund_ok(42, 49))
        # 週期轉換（8h -> 4h）不應被誤判成缺筆
        rows = []
        for d in range(0, 60):
            step = 8 if d < 30 else 4
            for k in range(24 // step):
                rows.append((D0 + d * DAY + k * step * 3_600_000 + 5, step, 0.0001))
        sw = core.Sym("SW", [(D0 + i * DAY, 100.0, 101.0, 99.0, 100.0, 1e9) for i in range(60)], rows)
        self.assertTrue(sw.fund_ok(20, 45))
        # 缺一筆（第 35 日 08:00）-> 間隔 8h > 1.5 × 4h -> 不完整
        rows2 = [r for r in rows if r[0] != D0 + 35 * DAY + 8 * 3_600_000 + 5]
        sw2 = core.Sym("SW2", [(D0 + i * DAY, 100.0, 101.0, 99.0, 100.0, 1e9) for i in range(60)], rows2)
        self.assertFalse(sw2.fund_ok(20, 45))
        self.assertTrue(sw2.fund_ok(20, 34))
        none = core.Sym("Y", [(D0, 1.0, 1.0, 1.0, 1.0, 1.0)], [])
        self.assertFalse(none.fund_ok(0, 1))

    def test_fund_ok_tail_and_midnight(self):
        rows_dropped_last = mk(rising(100))
        rows_dropped_last.set_funding_limit(D0 + 59 * DAY + 8 * 3_600_000 + 10)      # 可見資料止於第 59 日 08:00（含）
        self.assertFalse(rows_dropped_last.fund_ok(42, 60))                         # 少了 16:00 與 00:00 結算
        self.assertTrue(rows_dropped_last.fund_ok(42, 59))
        # 恰在 x 日 00:00 的結算由舊部位承擔：計入金額
        s = mk(rising(100), f_rate=0.0001)
        with_x = s.funding_sum(1, 42, 60, s.o[42])
        n_settle = round(with_x * s.o[42] / 0.0001 / 100.0)      # 粗略：每筆約 0.0001×(價格≈100~130)
        self.assertGreater(n_settle, 0)


class TestTargets(unittest.TestCase):
    def test_signal_timing_and_age(self):
        s = mk(rising(150))
        T = core.compute_targets(s, 20)
        self.assertEqual(T[39], 0)
        self.assertEqual(T[40], 1)
        self.assertEqual(T[100], 1)

    def test_volume_gates_and_hysteresis(self):
        q = lambda i: 25e6 if i < 80 else (15e6 if i < 160 else 5e6)
        s = mk(rising(260), qv=q)
        T = core.compute_targets(s, 20)
        self.assertEqual(T[60], 1)           # 新進場需 ≥ 20M
        self.assertEqual(T[140], 1)          # 15M：續抱（≥10M）
        self.assertEqual(T[250], 0)          # 5M：退出
        s2 = mk(rising(150), qv=15e6)
        self.assertEqual(max(core.compute_targets(s2, 20)), 0)   # 從未達 20M：不進場

    def test_placeholder_is_zero_and_resets_prev(self):
        s = mk(rising(150), ph_from=100)
        T = core.compute_targets(s, 20)
        self.assertEqual(T[99], 1)
        self.assertTrue(all(v == 0 for v in T[100:150]))


class TestNetReturn(unittest.TestCase):
    def test_hand_calc(self):
        closes = rising(300)
        s = mk(closes, f_rate=0.0001)
        e, x = 42, 60
        pe, px = closes[e], closes[x - 1]
        fund = 0.0
        for d in range(e, x):
            for k in range(3):
                fund += 1 * 0.0001 * closes[d - 1] / pe
        ratio = px / pe
        want = (ratio - 1) - (0.0005 + 0.0003) - (0.0005 + 0.0003) * ratio - fund
        got = core.net_return(s, 1, e, x, px)
        self.assertAlmostEqual(got, want, places=12)
        short = core.net_return(s, -1, e, x, px)
        want_s = -(ratio - 1) - (0.0005 + 0.0003) - (0.0005 + 0.0003) * ratio + fund
        self.assertAlmostEqual(short, want_s, places=12)

    def test_stress_slippage_x3(self):
        s = mk(rising(300))
        a = core.net_return(s, 1, 42, 60, s.c[59], 1.0)
        b = core.net_return(s, 1, 42, 60, s.c[59], 3.0)
        ratio = s.c[59] / s.o[42]
        self.assertAlmostEqual(a - b, 2 * 0.0003 * (1 + ratio), places=12)

    def test_unevaluable_cases(self):
        self.assertIsNone(core.net_return(mk(rising(300), drop_days=(50,)), 1, 42, 60, 130.0))
        self.assertIsNone(core.net_return(mk(rising(300), f_drop_days=(50,)), 1, 42, 60, 130.0))
        self.assertIsNone(core.net_return(mk(rising(300)), 1, 42, 60, float("nan")))

    def test_slippage_tiers(self):
        self.assertEqual(core.slip_bps(2e8), 3.0)
        self.assertEqual(core.slip_bps(5e7), 8.0)
        self.assertEqual(core.slip_bps(1.5e7), 25.0)
        self.assertEqual(core.slip_bps(5e6), 50.0)
        self.assertEqual(core.slip_bps(float("nan")), 50.0)


class TestSimulate(unittest.TestCase):
    def run_sim(self, s, N=20, ws=0, we=100):
        T = core.compute_targets(s, N)
        return core.simulate(s, N, T, ws, we)

    def test_entry_timing_and_censoring(self):
        s = mk(rising(300))
        trades, voided = self.run_sim(s, we=60)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t["entry"], t["exit"], t["reason"], t["dir"]), (42, 60, "cens", 1))
        self.assertEqual(t["entry_px"], s.o[42])
        self.assertEqual(t["exit_px"], s.c[59])
        self.assertAlmostEqual(t["nets"]["main"], core.net_return(s, 1, 42, 60, s.c[59]), places=12)
        self.assertEqual(voided, [])

    def test_termination_and_stress(self):
        s = mk(rising(300), ph_from=70)
        trades, voided = self.run_sim(s, we=200)
        t = trades[0]
        self.assertEqual((t["entry"], t["exit"], t["reason"]), (42, 70, "term"))
        self.assertEqual(t["exit_px"], s.c[69])
        n = t["nets"]
        self.assertAlmostEqual(n["S2"], n["main"] - 0.10, places=12)
        self.assertAlmostEqual(n["S3"], n["S1"] - 0.10, places=12)
        self.assertEqual(n["S5"], -0.25)
        self.assertEqual(len(trades), 1)
        self.assertEqual(voided, [])

    def test_termination_not_visible_beyond_window_end(self):
        s = mk(rising(300), ph_from=70)
        trades, _ = self.run_sim(s, we=60)               # 窗內看不到 70 日的占位 -> 行政截尾
        self.assertEqual(trades[0]["reason"], "cens")

    def test_entry_on_placeholder_day_is_voided(self):
        closes = [100.0] * 68 + [101.0] * 232            # t=68 才出現正報酬 -> 預定 execution 70
        s = mk(closes, ph_from=70)
        trades, voided = self.run_sim(s, we=200)
        self.assertEqual(trades, [])
        self.assertEqual([(v["entry"], v["dir"]) for v in voided], [(70, 1)])

    def test_liquidation(self):
        closes = rising(300)
        s = mk(closes, lows={50: closes[42] * 0.04})
        trades, _ = self.run_sim(s, we=100)
        t = trades[0]
        self.assertEqual((t["reason"], t["exit"]), ("liq", 51))
        self.assertTrue(all(v == -1.0 for v in t["nets"].values()))

    def test_short_liquidation(self):
        closes = [100.0] * 40 + [100.0 - (i + 1) * 0.5 for i in range(100)]
        s = mk(closes, highs={50: 100.0 * 2.0})
        T = core.compute_targets(s, 20)
        trades, _ = core.simulate(s, 20, T, 0, 100)
        shorts = [t for t in trades if t["dir"] < 0]
        self.assertEqual(shorts[0]["reason"], "liq")

    def test_reversal_creates_exit_and_new_entry_same_day(self):
        closes = [(100.0 + max(0, i - 39)) if i < 100 else 40.0 + (i - 100) * 0.1 for i in range(250)]      # 上升後在 100 日急跌：T 直接由 +1 翻成 −1
        s = mk(closes)
        T = core.compute_targets(s, 20)
        trades, _ = core.simulate(s, 20, T, 0, 200)
        longs = [t for t in trades if t["dir"] > 0]
        shorts = [t for t in trades if t["dir"] < 0]
        self.assertTrue(longs and shorts)
        self.assertEqual(longs[0]["exit"], shorts[0]["entry"])
        self.assertEqual(longs[0]["reason"], "sig")

    def test_flat_start_requires_target_change(self):
        s = mk(rising(300))
        T = core.compute_targets(s, 20)
        trades, voided = core.simulate(s, 20, T, 60, 120)    # T 在 60 之前早已為 +1 且未改變
        self.assertEqual((trades, voided), ([], []))

    def test_missing_data_makes_trade_unevaluable(self):
        s = mk(rising(300), drop_days=(50,))
        T = core.compute_targets(s, 20)
        trades, _ = core.simulate(s, 20, T, 0, 100)
        self.assertIsNone(trades[0]["nets"])                       # 持倉期間缺日 K：不可評估
        self.assertEqual(trades[0]["entry"], 42)

    def test_funding_gap_makes_trade_unevaluable(self):
        s = mk(rising(300), f_drop_days=(50,))
        T = core.compute_targets(s, 20)
        trades, _ = core.simulate(s, 20, T, 0, 100)
        self.assertTrue(trades and trades[0]["nets"] is None)

    def test_cooldown_dedup_counts_voided_positions(self):
        evs = [{"symbol": "A", "entry": 10}, {"symbol": "A", "entry": 14}, {"symbol": "A", "entry": 17},
               {"symbol": "B", "entry": 12}]
        f = core.dedup_flags(evs)
        self.assertEqual([f[id(e)] for e in evs], [True, False, True, True])


class TestBaseline(unittest.TestCase):
    def test_matches_manual_enumeration(self):
        closes = [100.0 + 3 * math.sin(i / 7.0) + i * 0.1 for i in range(300)]
        s = mk(closes)
        elig = core.eligible_days(s, 20, 0, 200)
        self.assertEqual(elig[0], 31)                                # 資訊日 t=e−2=29：30 日齊全、上市天數 30
        L = 5
        got = core.baseline_mean(s, 1, L, 200, elig)
        vals = [core.net_return(s, 1, e, min(e + L, 200), s.c[199] if e + L >= 200 else s.o[e + L]) for e in elig]      # 原定出場在窗末或之後：窗末行政截尾
        self.assertAlmostEqual(got[0], sum(vals) / len(vals), places=12)
        self.assertEqual(got[1], len(vals))

    def test_candidates_crossing_termination_are_kept_and_settled(self):
        s = mk(rising(300), ph_from=70)
        elig = [e for e in core.eligible_days(s, 20, 0, 200)]
        self.assertTrue(elig and max(elig) == 69)
        mean_, used, bad, cand = core.baseline_mean(s, 1, 10, 200, elig)
        self.assertEqual(cand, used + bad)
        crossing = [e for e in elig if e + 10 > 70]
        self.assertTrue(crossing)
        e = crossing[0]
        want = core.net_return(s, 1, e, 70, s.c[69])
        self.assertIsNotNone(want)
        self.assertGreaterEqual(used, len(crossing))

    def test_nominal_exit_beyond_window_is_censored_not_dropped(self):
        s = mk(rising(300))
        elig = core.eligible_days(s, 20, 0, 60)
        m, used, bad, cand = core.baseline_mean(s, 1, 30, 60, elig)
        self.assertEqual((cand, used, bad), (len(elig), len(elig), 0))
        e = elig[-1]
        want = core.net_return(s, 1, e, 60, s.c[59])
        first = core.baseline_mean(s, 1, 30, 60, [e])[0]
        self.assertAlmostEqual(first, want, places=12)

    def test_liquidation_candidate_counts_minus_one(self):
        closes = rising(300)
        s = mk(closes, lows={50: closes[45] * 0.04})
        m, used, bad, _ = core.baseline_mean(s, 1, 10, 200, [45])
        self.assertEqual((m, used), (-1.0, 1))


class TestPathsAndPortfolio(unittest.TestCase):
    def test_daily_path_sums_to_net(self):
        s = mk(rising(300))
        T = core.compute_targets(s, 20)
        trades, _ = core.simulate(s, 20, T, 0, 100)
        for t in trades:
            path = core.daily_path(s, t, 100)
            self.assertEqual(len(path), t["exit"] - t["entry"] + (1 if t["exit"] < 100 else 0))
            self.assertAlmostEqual(sum(path), t["nets"]["main"], places=12)

    def test_portfolio_capacity_and_pnl(self):
        s = mk(rising(300))
        mkt = lambda sym, e, x: {"symbol": sym, "entry": e, "exit": x, "dir": 1}
        items = [(mkt("B", 10, 13), [0.01, 0.02, 0.03], 1e9), (mkt("A", 10, 13), [0.1, 0.1, 0.1], 1e9),
                 (mkt("C", 11, 12), [0.5], 5e8)]
        pnl, acc, skp, eq = core.portfolio_daily(items, 0, 20, notional=0.05, max_pos=1)
        self.assertEqual((acc, skp), (1, 2))                         # 同額度時依代號：A 先取得；C 當日名額已滿
        self.assertAlmostEqual(pnl[10], 0.05 * 0.1)
        self.assertAlmostEqual(pnl[12], 0.05 * 0.1)
        self.assertAlmostEqual(pnl[13], 0.0)                          # 退出當日不再持有
        self.assertAlmostEqual(eq, 1.0 + 0.05 * 0.3)

    def test_portfolio_collateral_rule(self):
        items = [({"symbol": f"S{i}", "entry": 5, "exit": 8, "dir": 1}, [0.0, 0.0, 0.0], 1e9) for i in range(30)]
        _, acc, skp, _ = core.portfolio_daily(items, 0, 10)
        self.assertEqual((acc, skp), (20, 10))                        # 名額上限 20


class TestStats(unittest.TestCase):
    def test_quantile_matches_numpy_linear(self):
        v = [1.0, 2.0, 4.0, 8.0]
        self.assertAlmostEqual(core.quantile(v, 0.5), 3.0)
        self.assertAlmostEqual(core.quantile(v, 0.05 / 3), 1.0 + 0.05 / 3 * 3 * 1.0)
        self.assertTrue(math.isnan(core.quantile([], 0.5)))

    def test_cluster_bootstrap_deterministic_and_sensible(self):
        vals = [0.01 + (0.001 if i % 2 else -0.001) for i in range(200)]
        keys = [i // 4 for i in range(200)]
        c1, s1 = core.cluster_bootstrap(vals, keys, B=300)
        c2, s2 = core.cluster_bootstrap(vals, keys, B=300)
        self.assertEqual((c1, s1), (c2, s2))
        self.assertEqual(c1, 50)
        self.assertTrue(0.009 < core.lower_bound(s1) < 0.011)

    def test_stationary_bootstrap_constant_series(self):
        s = core.stationary_bootstrap([0.002] * 100, B=50)
        self.assertTrue(all(abs(x - 0.002) < 1e-12 for x in s))

    def test_iso_week(self):
        self.assertEqual(core.iso_week(0), "2020-01")


def fake_trade(entry, reason="sig", main=0.01, sym="A", nets=True):
    n = None if not nets else {"main": main, "S1": main, "S2": main, "S3": main, "S5": main}
    return {"symbol": sym, "entry": entry, "exit": entry + 5, "dir": 1, "reason": reason, "nets": n}


class TestJudge(unittest.TestCase):
    def stats(self, **kw):
        base = dict(natural_dedup=300, weeks=150, mean_main=0.01, mean_S1=0.01, mean_S2=0.01, mean_S3=0.01, mean_S4=0.01,
                    mean_S5=0.01, uneval_share=0.0, voided_share=0.0)
        base.update(kw)
        return base

    def test_summarize_counts_natural_only_and_s4(self):
        trs = [fake_trade(0), fake_trade(10), fake_trade(20, reason="cens"), fake_trade(30, nets=False)]
        s = analyze.summarize(trs, [])
        self.assertEqual(s["natural_dedup"], 2)       # 0、10 為可評估的 sig 且去重成立；30 不可評估不計入（較嚴）
        self.assertEqual(s["n_uneval"], 1)
        self.assertAlmostEqual(s["mean_S4"], (0.01 * 3 - 1) / 4)

    def test_voided_event_occupies_cooldown(self):
        v = [{"symbol": "A", "entry": 12}]
        trs = [fake_trade(10)]
        s = analyze.summarize(trs, v)
        self.assertEqual(s["natural_dedup"], 1)
        trs2 = [fake_trade(14)]
        s2 = analyze.summarize(trs2, [{"symbol": "A", "entry": 10}])
        self.assertEqual(s2["natural_dedup"], 0)                                   # 14 在作廢事件 10 的冷卻內

    def test_pass_and_fail_and_unknown(self):
        boot = {"net_a": (100, 0.002), "net_b": (40, 0.002), "net_c": (900, 0.002), "ex_a": (100, 0.001), "ex_b": (40, 0.001)}
        ex = {"mean": 0.01, "mean_s4": 0.01, "oos_mean": 0.005, "unevaluable": False, "oos_unevaluable": False, "s4_unevaluable": False}
        st, _ = analyze.judge_qualification(self.stats(), self.stats(natural_dedup=80, weeks=40), boot, ex)
        self.assertEqual(st, "PASS")
        boot_bad = dict(boot, net_a=(100, -0.001))
        st, _ = analyze.judge_qualification(self.stats(), self.stats(natural_dedup=80, weeks=40), boot_bad, ex)
        self.assertEqual(st, "FAIL")
        st, _ = analyze.judge_qualification(self.stats(natural_dedup=100), self.stats(natural_dedup=80, weeks=40), boot, ex)
        self.assertEqual(st, "UNKNOWN")
        st, _ = analyze.judge_qualification(self.stats(uneval_share=0.02), self.stats(natural_dedup=80, weeks=40), boot, ex)
        self.assertEqual(st, "UNKNOWN")
        st, _ = analyze.judge_qualification(self.stats(voided_share=0.06), self.stats(natural_dedup=80, weeks=40), boot, ex)
        self.assertEqual(st, "UNKNOWN")
        st, _ = analyze.judge_qualification(self.stats(mean_S5=-0.001), self.stats(natural_dedup=80, weeks=40), boot, ex)
        self.assertEqual(st, "FAIL")
        st, _ = analyze.judge_qualification(self.stats(), self.stats(natural_dedup=80, weeks=40), dict(boot, ex_b=None), ex)
        self.assertEqual(st, "UNKNOWN")                                            # 不可評估不得變 FAIL

    def test_unknown_beats_fail(self):
        boot = {"net_a": (100, -0.5), "net_b": (40, 0.002), "net_c": (900, 0.002), "ex_a": (100, 0.001), "ex_b": (40, 0.001)}
        ex = {"mean": 0.01, "mean_s4": 0.01, "oos_mean": 0.005, "unevaluable": False, "oos_unevaluable": False, "s4_unevaluable": False}
        st, _ = analyze.judge_qualification(self.stats(natural_dedup=100), self.stats(natural_dedup=80, weeks=40), boot, ex)
        self.assertEqual(st, "UNKNOWN")                                            # 樣本不足優先於 FAIL

    def test_nan_oos_mean_is_unknown_not_fail(self):
        boot = {"net_a": (100, 0.002), "net_b": (40, 0.002), "net_c": (900, 0.002), "ex_a": (100, 0.001), "ex_b": (40, 0.001)}
        ex = {"mean": 0.01, "mean_s4": 0.01, "oos_mean": 0.005, "unevaluable": False, "oos_unevaluable": False, "s4_unevaluable": False}
        st, _ = analyze.judge_qualification(self.stats(), self.stats(natural_dedup=80, weeks=40, mean_main=float("nan")), boot, ex)
        self.assertEqual(st, "UNKNOWN")

    def test_target_mapping(self):
        self.assertEqual(analyze.judge_target("FAIL", 20, 0.2, 0.0)[0], "FAIL")
        self.assertEqual(analyze.judge_target("UNKNOWN", 20, 0.2, 0.0)[0], "UNKNOWN")
        self.assertEqual(analyze.judge_target("PASS", 20, 0.05, 0.0)[0], "FAIL")       # ≥12 月且中位數 <10%
        self.assertEqual(analyze.judge_target("PASS", 20, 0.12, -0.2)[0], "FAIL")      # 最差月 < −10%
        self.assertEqual(analyze.judge_target("PASS", 20, 0.12, 0.0)[0], "UNKNOWN")    # 月數 < 36 且未見失敗
        self.assertEqual(analyze.judge_target("PASS", 6, 0.01, -0.5)[0], "UNKNOWN")     # 月數不足 12：不能判 FAIL
        self.assertEqual(analyze.judge_target("PASS", 40, 0.12, 0.0)[0], "PASS")

    def test_monthly_returns_only_full_months(self):
        ws = (__import__("datetime").date(2025, 2, 15) - __import__("datetime").date(2020, 1, 1)).days
        we = ws + 60
        pnl = [0.001] * 60
        months, excluded = analyze.monthly_returns(pnl, ws, we)
        keys = [k for k, _ in months]
        self.assertEqual(excluded.get("2025-02"), "部分月")
        self.assertNotIn("2025-02", keys)                                           # 部分月不算
        self.assertIn("2025-03", keys)
        self.assertEqual(len(months), 1)
        self.assertAlmostEqual(months[0][1], (1.0 + 0.001 * 45 - (1.0 + 0.001 * 14)) / (1.0 + 0.001 * 14), places=9)


class TestCausality(unittest.TestCase):
    def test_changing_data_after_cut_does_not_change_in_sample(self):
        base = rising(300)
        a = mk(base)
        b = mk(base, ph_from=155, f_drop_days=(160, 161, 162))          # 切點（150）之後出現占位日與資金費缺口
        for sym in (a, b):
            sym.set_funding_limit(D0 + 150 * DAY)
        Ta, Tb = core.compute_targets(a, 20), core.compute_targets(b, 20)
        self.assertEqual(Ta[:150], Tb[:150])
        ta, va = core.simulate(a, 20, Ta, 0, 150)
        tb, vb = core.simulate(b, 20, Tb, 0, 150)
        strip = lambda ts: [(t["entry"], t["exit"], t["reason"], t["entry_px"], t["exit_px"], t["nets"]) for t in ts]
        self.assertEqual(strip(ta), strip(tb))
        self.assertEqual(va, vb)
        self.assertEqual(ta[0]["reason"], "cens")                         # 切點後的占位日不可見：行政截尾
        el_a = core.eligible_days(a, 20, 0, 150)
        el_b = core.eligible_days(b, 20, 0, 150)
        self.assertEqual(el_a, el_b)
        self.assertEqual(core.baseline_mean(a, 1, 10, 150, el_a), core.baseline_mean(b, 1, 10, 150, el_b))


class TestMissAndWarmup(unittest.TestCase):
    def test_missing_day_exit_is_miss_not_natural(self):
        s = mk(rising(300), drop_days=(50,))
        T = core.compute_targets(s, 20)
        trades, _ = core.simulate(s, 20, T, 0, 100)
        self.assertEqual(trades[0]["reason"], "miss")
        self.assertIsNone(trades[0]["nets"])

    def test_warmup_requires_complete_closes(self):
        s = mk(rising(200), drop_days=(10,))
        T = core.compute_targets(s, 60)
        self.assertEqual(T[70], 0)                 # t−60 = 10 缺資料
        self.assertEqual(T[100], 1)
        self.assertFalse(core.signal_ok(s, 60, 70))
        self.assertTrue(core.signal_ok(s, 60, 100))

    def test_baseline_liquidation_precedes_missing_data(self):
        closes = rising(300)
        s = mk(closes, lows={50: closes[45] * 0.04}, drop_days=(52,))
        m, used, bad, _ = core.baseline_mean(s, 1, 10, 200, [45])
        self.assertEqual((m, used, bad), (-1.0, 1, 0))                       # 強平發生在缺資料之前
        s2 = mk(closes, lows={55: closes[45] * 0.04}, drop_days=(52,))
        m2, used2, bad2, _ = core.baseline_mean(s2, 1, 10, 200, [49])
        self.assertEqual((used2, bad2), (0, 1))                               # 缺資料(52)在強平(55)之前 -> 不可評估，且被回報


class TestDefenses(unittest.TestCase):
    def stats(self, **kw):
        base = dict(natural_dedup=300, weeks=150, mean_main=0.01, mean_S1=0.01, mean_S2=0.01, mean_S3=0.01, mean_S4=0.01,
                    mean_S5=0.01, uneval_share=0.0, voided_share=0.0)
        base.update(kw)
        return base

    BOOT = {"net_a": (100, 0.002), "net_b": (40, 0.002), "net_c": (900, 0.002), "ex_a": (100, 0.001), "ex_b": (40, 0.001)}
    EX = {"mean": 0.01, "mean_s4": 0.01, "oos_mean": 0.005, "unevaluable": False, "oos_unevaluable": False, "s4_unevaluable": False}

    def judge(self, is_s=None, oos_s=None, boot=None, ex=None, s4=None):
        return analyze.judge_qualification(is_s or self.stats(), oos_s or self.stats(natural_dedup=80, weeks=40),
                                           boot or self.BOOT, ex or self.EX, s4)[0]

    def test_oos_quality_gates(self):
        self.assertEqual(self.judge(), "PASS")
        self.assertEqual(self.judge(oos_s=self.stats(natural_dedup=80, weeks=40, uneval_share=0.02)), "UNKNOWN")
        self.assertEqual(self.judge(oos_s=self.stats(natural_dedup=80, weeks=40, voided_share=0.06)), "UNKNOWN")
        self.assertEqual(self.judge(ex=dict(self.EX, oos_unevaluable=True)), "UNKNOWN")
        self.assertEqual(self.judge(ex=dict(self.EX, unevaluable=True)), "UNKNOWN")

    def test_s4_inference_is_required_when_present(self):
        good = dict(self.BOOT)
        self.assertEqual(self.judge(s4=good), "PASS")
        self.assertEqual(self.judge(s4=dict(good, net_a=(100, -0.001))), "FAIL")
        self.assertEqual(self.judge(s4=dict(good, ex_b=None)), "UNKNOWN")

    def test_nan_everywhere_is_unknown_never_fail_or_pass(self):
        nan = float("nan")
        self.assertEqual(self.judge(is_s=self.stats(mean_main=nan)), "UNKNOWN")
        self.assertEqual(self.judge(is_s=self.stats(mean_S2=nan)), "UNKNOWN")
        self.assertEqual(self.judge(boot=dict(self.BOOT, net_c=(900, nan))), "UNKNOWN")
        self.assertEqual(self.judge(ex=dict(self.EX, mean=nan)), "UNKNOWN")
        self.assertEqual(self.judge(ex=dict(self.EX, oos_mean=nan)), "UNKNOWN")
        self.assertEqual(analyze.judge_target("PASS", 36, nan, nan)[0], "UNKNOWN")

    def test_empty_bootstrap_inputs(self):
        self.assertEqual(core.cluster_bootstrap([], []), (0, []))
        self.assertEqual(core.stationary_bootstrap([]), [])
        boot = analyze.run_boot([0.1] * 5, list(range(5)), list(range(5)), [0.1] * 10, [0.1] * 5, list(range(5)), list(range(5)))
        self.assertTrue(all(v is None for v in boot.values()))                  # 群集 <30、天數 <360 -> 抽樣前即不可評估

    def test_invalid_month_exclusion(self):
        ws = (__import__("datetime").date(2025, 2, 15) - __import__("datetime").date(2020, 1, 1)).days
        months, excluded = analyze.monthly_returns([0.001] * 60, ws, ws + 60, invalid_months={"2025-03"})
        self.assertEqual(months, [])
        self.assertEqual(excluded["2025-03"], "有不可評估交易（資料缺漏）")


class TestDailyPathBuckets(unittest.TestCase):
    def test_exit_cost_on_exit_day_and_clamped_at_window_end(self):
        s = mk(rising(300), f_rate=0.0)
        T = core.compute_targets(s, 20)
        trades, _ = core.simulate(s, 20, T, 0, 60)
        t = trades[0]
        p_inside = core.daily_path(s, t, 100)               # 窗末之後仍有一天：出場日有一格
        p_clamped = core.daily_path(s, t, 60)               # x == we：併入最後一日
        self.assertEqual(len(p_inside), t["exit"] - t["entry"] + 1)
        self.assertEqual(len(p_clamped), t["exit"] - t["entry"])
        self.assertAlmostEqual(sum(p_inside), sum(p_clamped), places=12)
        ratio = t["exit_px"] / t["entry_px"]
        self.assertAlmostEqual(p_inside[-1], -(0.0005 + 0.0003) * ratio, places=12)       # 出場日只有出場成本


class TestSeriesAndMidnight(unittest.TestCase):
    def test_daily_series_presence_and_s4_spread(self):
        s = mk(rising(300), f_rate=0.0)
        T = core.compute_targets(s, 20)
        trades, _ = core.simulate(s, 20, T, 0, 60)
        t = trades[0]                                            # 進場 42、窗末截尾 60
        syms = {"AAA": s}
        ser = analyze.daily_series(syms, [t], [], 60)
        self.assertEqual(len(ser), t["exit"] - t["entry"])       # 每個持倉日一格
        self.assertAlmostEqual(sum(ser), t["nets"]["main"], places=12)
        bad = {"symbol": "AAA", "entry": 10, "exit": 14}
        ser4 = analyze.daily_series(syms, [], [bad], 60)
        self.assertEqual(ser4, [-0.25] * 4)                      # 沿整個持倉區間均攤，每日各計一個持倉
        # 出場日的成本併入前一個持倉日，不新增一天
        t2 = dict(t, exit=50, exit_px=s.o[50], reason="sig")
        t2["nets"] = {"main": core.net_return(s, 1, 42, 50, s.o[50])}
        ser2 = analyze.daily_series(syms, [t2], [], 60)
        self.assertEqual(len(ser2), 50 - 42)
        self.assertAlmostEqual(sum(ser2), t2["nets"]["main"], places=12)

    def test_daily_path_parts_midnight_components(self):
        s = mk(rising(300), f_rate=0.0001)
        t = {"dir": 1, "entry": 42, "exit": 50, "entry_px": s.o[42], "exit_px": s.o[50], "reason": "sig"}
        t["nets"] = {"main": core.net_return(s, 1, 42, 50, s.o[50])}
        vals, mid = core.daily_path_parts(s, t, 100)
        self.assertEqual(len(vals), len(mid))
        self.assertAlmostEqual(sum(vals), t["nets"]["main"], places=12)
        ratio = t["exit_px"] / t["entry_px"]
        self.assertAlmostEqual(mid[-1], -(0.0005 + 0.0003) * ratio, places=12)       # 出場日 00:00：出場成本
        self.assertAlmostEqual(mid[0], -0.0001 * s.c[41] / s.o[42], places=12)           # 進場日 00:00:00.005 的資金費由新部位承擔

    def test_portfolio_snapshot_includes_midnight_items(self):
        def mkitem(sym, entry, exit_, path, mid):
            return ({"symbol": sym, "entry": entry, "exit": exit_, "dir": 1}, path, 1e9, mid)
        holders = [mkitem(f"H{i:02d}", 1, 20, [0.0] * 19, [0.0] * 4 + [-1.0] + [0.0] * 14) for i in range(19)]   # 第 5 日午夜各虧 100%
        newcomer = mkitem("Z", 5, 8, [0.0] * 3, None)
        _, acc, skp, _ = core.portfolio_daily(holders + [newcomer], 0, 30)
        self.assertEqual((acc, skp), (19, 1))                    # 午夜損失先入快照：權益 1 − 0.95 = 0.05，不足 19 倉＋0.05
        holders2 = [mkitem(f"H{i:02d}", 1, 20, [0.0] * 19, [0.0] * 19) for i in range(19)]
        _, acc2, skp2, _ = core.portfolio_daily(holders2 + [newcomer], 0, 30)
        self.assertEqual((acc2, skp2), (20, 0))


class TestRound3(unittest.TestCase):
    def test_interval_transition_missing_midnight_row_is_detected(self):
        def build(skip_midnight):
            rows = []
            for d in range(0, 60):
                if d < 30:
                    times = [0, 8, 16]
                    iv = 8
                else:
                    times = list(range(24))
                    iv = 1
                for h in times:
                    if skip_midnight and d == 30 and h == 0:
                        continue
                    rows.append((D0 + d * DAY + h * 3_600_000 + 5, iv, 0.0001))
            return core.Sym("TR", [(D0 + i * DAY, 100.0, 101.0, 99.0, 100.0, 1e9) for i in range(60)], rows)
        ok = build(False)
        self.assertTrue(ok.fund_ok(20, 45))              # 8h -> 1h 的正常轉換（間隔 8h 或更短）不誤判
        self.assertFalse(build(True).fund_ok(20, 45))    # 轉換處午夜那筆遺失：9h 間隔 > 8h + 60s

    def test_liquidation_loss_is_booked_on_liquidation_day(self):
        closes = rising(300)
        s = mk(closes, lows={50: closes[42] * 0.04})
        T = core.compute_targets(s, 20)
        trades, _ = core.simulate(s, 20, T, 0, 100)
        t = trades[0]
        vals, mid = core.daily_path_parts(s, t, 100)
        self.assertEqual(t["reason"], "liq")
        self.assertEqual(len(vals), t["exit"] - t["entry"])          # 沒有一般出場日
        self.assertAlmostEqual(sum(vals), -1.0, places=12)
        self.assertAlmostEqual(mid[-1], -0.0001 * s.c[49] / t["entry_px"], places=12)     # 強平日 00:00 的資金費仍在 mid；沒有出場成本
        # 強平損失在強平日入帳 -> 次日（exit 日）新倉的快照已承認完整損失
        def pack(n_others):
            loser = (t, vals, 1e9, mid)
            others = [({"symbol": f"H{i:02d}", "entry": 42, "exit": 90, "dir": 1}, [0.0] * 48, 1e9, [0.0] * 48) for i in range(n_others)]
            newcomer = ({"symbol": "Z", "entry": t["exit"], "exit": t["exit"] + 5, "dir": 1}, [0.0] * 5, 1e9, [0.0] * 5)
            return core.portfolio_daily([loser] + others + [newcomer], 0, 100)
        _, acc, skp, eq = pack(18)
        self.assertEqual((acc, skp), (20, 0))                          # 權益 0.95、已用 0.90 -> 可用 0.05，剛好可開
        self.assertAlmostEqual(eq, 0.95, places=9)
        _, acc2, skp2, _ = pack(19)
        self.assertEqual((acc2, skp2), (20, 1))                        # 已用 0.95、權益 0.95 -> 可用 0：新倉被拒（若未承認損失會誤接受）

    def test_main_flow_smoke_on_synthetic_universe(self):
        import random as _r
        syms, names = {}, []
        for i in range(36):
            rng = _r.Random(100 + i)
            c, closes = 100.0, []
            for _ in range(TOTAL):
                c *= 1.0 + rng.gauss(0.0005, 0.03)
                closes.append(max(c, 1.0))
            kw = {}
            if i % 9 == 0:
                kw["ph_from"] = 2000                         # 下市占位
            if i % 11 == 1:
                kw["drop_days"] = (1500, 1501)               # 缺資料日
            nm = f"SYN{i:02d}USDT"
            syms[nm] = mk(closes, name=nm, **kw)
            names.append(nm)
        text = analyze.main_run(names, syms, save_results=False, B=30)
        self.assertIn("合格通過：", text)
        self.assertIn("目標通過：", text)
        self.assertNotIn("Traceback", text)


class TestFundingOwnership(unittest.TestCase):
    def build(self, offset_ms, drop_day=None):
        rows = []
        for d in range(0, 80):
            for k in range(3):
                if drop_day is not None and d == drop_day and k == 0:
                    continue                                  # 只缺該日 00:00 那一筆
                rows.append((D0 + d * DAY + k * 8 * 3_600_000 + offset_ms, 8, 0.0001))
        return core.Sym("OW", [(D0 + i * DAY, 100.0, 101.0, 99.0, 100.0, 1e9) for i in range(80)], rows)

    def test_missing_settlement_at_exit_instant_exact_offset_is_ours(self):
        self.assertTrue(self.build(0).fund_ok(42, 60))
        self.assertFalse(self.build(0, drop_day=60).fund_ok(42, 60))      # .000 的出場時刻結算屬於本交易：遺失 -> 不完整

    def test_missing_settlement_just_after_exit_is_not_ours(self):
        self.assertTrue(self.build(5, drop_day=60).fund_ok(42, 60))        # .005：該筆屬下一部位
        self.assertFalse(self.build(5, drop_day=60).fund_ok(42, 61))       # 但它若落在持倉內（出場延後一天）就是缺筆
        self.assertFalse(self.build(5, drop_day=50).fund_ok(42, 60))

    def test_smoke_empty_valid_early_return(self):
        syms, names = {}, []
        for i in range(3):
            nm = f"E{i}USDT"
            syms[nm] = mk(rising(TOTAL), name=nm)
            names.append(nm)
        text = analyze.main_run(names, syms, save_results=False, B=20, header_hashes=False)
        self.assertIn("整體 UNKNOWN", text)


if __name__ == "__main__":
    unittest.main()
