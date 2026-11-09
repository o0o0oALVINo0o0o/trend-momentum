"""trend-momentum Stage 0 分析（SPEC.md v3.2；純標準函式庫）。

兩個階段（結果前登記紀律）：
  python -I analyze.py prep   載入資料、偵測占位日、寫 data/termination.csv 並印出其 SHA-256（不計算任何報酬）。
                              把該雜湊追加到 FREEZE.txt（一行：termination.csv SHA-256：<hash>）後才能 run。
  python -I analyze.py run    重新產生 termination.csv 並核對 FREEZE.txt 的雜湊；通過才計算並寫 results.txt。
前置條件：fetch.py verify 已通過（data/verify_failures.jsonl 為空）。結果前不得為了結果調整規則。
"""
import csv
import datetime as dt
import glob
import hashlib
import io
import json
import math
import os
import sys
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import core  # noqa: E402
from core import CUT, D0, DAY, TOTAL, isnan  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "data", "raw", "data", "futures", "um", "monthly")
NS = (20, 60, 120)
SCEN = ("main", "S1", "S2", "S3", "S5")
FIN = math.isfinite


def fin(*xs):
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) and FIN(x) for x in xs)


def utf8_stdout():
    """Windows 預設主控台編碼（cp950）無法輸出 ≥ 等符號；改用 UTF-8（無法重設時忽略）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def file_hash(path):
    try:
        return hashlib.sha256(open(path, "rb").read()).hexdigest()
    except OSError:
        return "n/a"


# ---------------------------------------------------------------- 純判定函式（可測試）
def mean(v):
    return sum(v) / len(v) if v else core.NAN


def summarize(trades, voided):
    """trades 含 nets（None = 不可評估）。去重在任何排除之前執行；只有 reason == 'sig' 的算自然完成。"""
    events = trades + voided
    flags = core.dedup_flags(events)
    natural = [t for t in trades if t["reason"] == "sig" and flags[id(t)] and t["nets"] is not None]
    ev = [t for t in trades if t["nets"] is not None]
    n_un = len(trades) - len(ev)
    s = {
        "n_trades": len(trades), "n_eval": len(ev), "n_uneval": n_un,
        "n_voided": len(voided), "n_events": len(events),
        "natural_dedup": len(natural),
        "weeks": len({core.iso_week(t["entry"]) for t in natural}),
        "reasons": {r: sum(1 for t in trades if t["reason"] == r) for r in ("sig", "miss", "liq", "term", "cens")},
    }
    for sc in SCEN:
        s["mean_" + sc] = mean([t["nets"][sc] for t in ev])
    s["mean_S4"] = (sum(t["nets"]["main"] for t in ev) - n_un) / len(trades) if trades else core.NAN
    s["voided_share"] = len(voided) / len(events) if events else 0.0
    s["uneval_share"] = n_un / len(trades) if trades else 0.0
    return s


BOOT_KEYS = ("net_a", "net_b", "net_c", "ex_a", "ex_b")


MIN_UNITS = {"net_a": 30, "net_b": 30, "ex_a": 30, "ex_b": 30, "net_c": 360}


def _lb(boot, key):
    v = None if boot is None else boot.get(key)
    if (not isinstance(v, (tuple, list)) or len(v) != 2 or not fin(v[1]) or not isinstance(v[0], int)
            or isinstance(v[0], bool) or v[0] < MIN_UNITS[key]):
        return None, "不可評估（群集／區塊／天數不足或統計量非有限）"
    return v[1], f"單位 {v[0]}，下界 {v[1]:.6f}"


def judge_qualification(is_s, oos_s, boot, excess_info, s4_boot=None):
    """依 SPEC §5／§9 判定「合格通過」。回傳 (status, [(條件, 狀態, 說明)])。
    boot／s4_boot: {'net_a','net_b','net_c','ex_a','ex_b'} 各為 (n_units, lower)|None。s4_boot=None 表示沒有不可評估交易（S4 = 主要情境）。
    excess_info: dict(mean, oos_mean, unevaluable, oos_unevaluable)。任何必要數值非有限 -> UNKNOWN，不得變 FAIL。"""
    rows = []

    def add(name, st, why):
        rows.append((name, st, why))

    need = ("natural_dedup", "weeks", "mean_main", "mean_S1", "mean_S2", "mean_S3", "mean_S4", "mean_S5", "uneval_share", "voided_share")
    for lab, st_ in (("樣本內", is_s), ("樣本外", oos_s)):
        miss = [k for k in need if not isinstance(st_, dict) or k not in st_]
        if not miss:
            miss = [k for k in ("natural_dedup", "weeks") if not isinstance(st_[k], int) or isinstance(st_[k], bool)]
            miss += [k for k in ("uneval_share", "voided_share") if not fin(st_[k])]
        if miss:
            add(f"輸入檢查：{lab}統計欄位", "UNKNOWN", f"缺 {miss}")
            return "UNKNOWN", rows
    ok1 = is_s["natural_dedup"] >= 200 and is_s["weeks"] >= 100 and oos_s["natural_dedup"] >= 50 and oos_s["weeks"] >= 25
    add("1 樣本量", "PASS" if ok1 else "UNKNOWN",
        f"IS 自然完成去重 {is_s['natural_dedup']}（≥200）週 {is_s['weeks']}（≥100）；OOS {oos_s['natural_dedup']}（≥50）週 {oos_s['weeks']}（≥25）")
    for lab, s in (("IS", is_s), ("OOS", oos_s)):
        add(f"資料品質：{lab} 不可評估占比 ≤1%", "PASS" if s["uneval_share"] <= 0.01 else "UNKNOWN", f"{s['uneval_share']:.4%}")
        add(f"資料品質：{lab} 作廢進場占比 ≤5%", "PASS" if s["voided_share"] <= 0.05 else "UNKNOWN", f"{s['voided_share']:.4%}")

    def net_cond(name, st, bt):
        parts = [_lb(bt, k) for k in ("net_a", "net_b", "net_c")]
        if not fin(st) or any(p[0] is None for p in parts):
            add(name, "UNKNOWN", f"平均 {st}；" + "；".join(p[1] for p in parts))
        else:
            add(name, "PASS" if st > 0 and all(p[0] > 0 for p in parts) else "FAIL",
                f"平均 {st:.6f}；" + "；".join(p[1] for p in parts))

    def ex_cond(name, mu, bt, flag="unevaluable"):
        parts = [_lb(bt, k) for k in ("ex_a", "ex_b")]
        if excess_info is None or excess_info.get(flag, True) or not fin(mu) or any(p[0] is None for p in parts):
            add(name, "UNKNOWN", f"基準或統計量不可評估（平均 {mu}）；" + "；".join(p[1] for p in parts))
        else:
            add(name, "PASS" if mu > 0 and all(p[0] > 0 for p in parts) else "FAIL",
                f"平均超額 {mu:.6f}；" + "；".join(p[1] for p in parts))

    net_cond("2 樣本內淨報酬 >0（三種 bootstrap）", is_s["mean_main"], boot)
    ex_cond("3 樣本內超額 >0（兩種 bootstrap）", None if excess_info is None else excess_info.get("mean"), boot)
    # 4 樣本外
    if (excess_info is None or excess_info.get("oos_unevaluable", True) or not fin(oos_s["mean_main"], oos_s.get("mean_S4", core.NAN))
            or not fin(excess_info.get("oos_mean", core.NAN))):
        add("4 樣本外淨報酬與超額 >0（含不可評估交易記 −100% 的 S4 平均）", "UNKNOWN", "樣本外不可評估（平均、基準或剔除比例）")
    else:
        add("4 樣本外淨報酬與超額 >0（含不可評估交易記 −100% 的 S4 平均）",
            "PASS" if oos_s["mean_main"] > 0 and oos_s["mean_S4"] > 0 and excess_info["oos_mean"] > 0 else "FAIL",
            f"OOS 平均 {oos_s['mean_main']:.6f}；OOS S4 平均 {oos_s['mean_S4']:.6f}；OOS 超額 {excess_info['oos_mean']:.6f}")
    # 5 壓力情境（含 S4 全套推論）
    vals = {k: is_s["mean_" + k] for k in ("S1", "S2", "S3", "S4", "S5")}
    if not fin(*vals.values()):
        add("5 壓力情境 S1–S5 平均 >0", "UNKNOWN", "；".join(f"{k}={v}" for k, v in vals.items()))
    else:
        add("5 壓力情境 S1–S5 平均 >0", "PASS" if all(v > 0 for v in vals.values()) else "FAIL",
            "；".join(f"{k}={v:.6f}" for k, v in vals.items()))
    if s4_boot is not None:
        net_cond("5b S4 最不利界限：淨報酬推論", is_s["mean_S4"], s4_boot)
        ex_cond("5c S4 最不利界限：超額推論", None if excess_info is None else excess_info.get("mean_s4"), s4_boot, "s4_unevaluable")
    sts = [r[1] for r in rows]
    status = "UNKNOWN" if "UNKNOWN" in sts else ("FAIL" if "FAIL" in sts else "PASS")
    return status, rows


def judge_target(qual_status, n_months, median, worst):
    """SPEC §5 目標通過映射。"""
    if qual_status == "FAIL":
        return "FAIL", "合格 FAIL"
    if qual_status == "UNKNOWN":
        return "UNKNOWN", "合格 UNKNOWN"
    if not fin(median, worst):
        return "UNKNOWN", "月報酬不可評估"
    if n_months >= 12 and (median < 0.10 or worst < -0.10):
        return "FAIL", f"已觀察到必要條件未達（月數 {n_months}、中位數 {median:.4f}、最差 {worst:.4f}）"
    if n_months < 36:
        return "UNKNOWN", f"樣本外完整有效月數 {n_months} < 36，且未觀察到失敗"
    return "PASS", "目標條件成立"


def month_key(day):
    d = (dt.datetime(2020, 1, 1) + dt.timedelta(days=day)).date()
    return f"{d.year}-{d.month:02d}"


def monthly_returns(pnl, ws, we, invalid_months=()):
    """每日損益 -> ([(月份, 報酬)], {被排除月份: 原因})；只含完全落在 [ws, we) 內且無不可評估交易的曆月。"""
    equity = 1.0
    months, order, eq_start = {}, [], {}
    for i, p in enumerate(pnl):
        key = month_key(ws + i)
        if key not in months:
            months[key] = {"days": 0}
            order.append(key)
            eq_start[key] = equity
        months[key]["days"] += 1
        equity += p
        months[key]["end"] = equity
    out, excluded = [], {}
    for key in order:
        y, m = int(key[:4]), int(key[5:])
        nd = (dt.date(y + (m == 12), 1 if m == 12 else m + 1, 1) - dt.date(y, m, 1)).days
        if months[key]["days"] != nd:
            excluded[key] = "部分月"
        elif key in invalid_months:
            excluded[key] = "有不可評估交易（資料缺漏）"
        elif eq_start[key] <= 0:
            excluded[key] = "權益 ≤ 0"
        else:
            out.append((key, (months[key]["end"] - eq_start[key]) / eq_start[key]))
    return out, excluded


# ---------------------------------------------------------------- 資料載入
def kline_zip_paths(name):
    return sorted(glob.glob(os.path.join(RAW, "klines", name, "1d", "*.zip")))


def load_symbol(name):
    k_rows, f_rows = [], []
    for zp in kline_zip_paths(name):
        month = os.path.basename(zp)[:-4][-7:]
        with zipfile.ZipFile(zp) as z:
            text = z.read(z.namelist()[0]).decode("utf-8")
        rows, _e, _n = common.parse_kline_csv(text, month)
        k_rows += rows
    for zp in sorted(glob.glob(os.path.join(RAW, "fundingRate", name, "*.zip"))):
        month = os.path.basename(zp)[:-4][-7:]
        with zipfile.ZipFile(zp) as z:
            text = z.read(z.namelist()[0]).decode("utf-8")
        rows, _e, _n = common.parse_funding_csv(text, month)
        f_rows += rows
    return core.Sym(name, k_rows, f_rows)


def kline_digest(name):
    h = hashlib.sha256()
    for zp in kline_zip_paths(name):
        h.update(os.path.basename(zp).encode() + b":" + hashlib.sha256(open(zp, "rb").read()).hexdigest().encode() + b"\n")
    return h.hexdigest()


def termination_rows(syms, names):
    out = []
    for nm in names:
        s = syms[nm]
        runs, i = [], 0
        while i < TOTAL:
            if s.ph[i]:
                j = i
                while j < TOTAL and s.ph[j]:
                    j += 1
                runs.append(f"D={i};L={i - 1 if i >= 1 else ''};end_exclusive={j}")
                i = j
            else:
                i += 1
        out.append([nm, "" if s.first_trad is None else s.first_trad, "|".join(runs), kline_digest(nm), "SPEC v3.2 B0"])
    return out


def write_termination(syms, names):
    path = os.path.join(HERE, "data", "termination.csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["symbol", "first_tradable_day", "placeholder_runs(D=first placeholder day;L=last tradable day before D)",
                    "kline_zip_digest", "rule"])
        w.writerows(termination_rows(syms, names))
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def fmt(x, p=6):
    return "nan" if (not isinstance(x, (int, float)) or isnan(x)) else f"{x:.{p}f}"


def load_all():
    fails = os.path.join(HERE, "data", "verify_failures.jsonl")
    if not os.path.exists(fails) or os.path.getsize(fails) != 0:
        sys.exit("verify_failures.jsonl 不存在或非空：請先讓 fetch.py verify 通過")
    with open(os.path.join(HERE, "data", "population.csv"), encoding="utf-8", newline="") as f:
        names = [r["symbol"] for r in csv.DictReader(f)]
    syms = {}
    t0 = time.time()
    for i, nm in enumerate(names):
        syms[nm] = load_symbol(nm)
        if (i + 1) % 200 == 0:
            print(f"  已載入 {i + 1}/{len(names)}（{time.time() - t0:.0f}s）", flush=True)
    return names, syms


# ---------------------------------------------------------------- 主流程
def daily_series(syms, trs, extra_unevaluable, we):
    """(c) 日序列：每日所有『持倉中』交易的平均損益。持倉日 = entry..exit−1；出場日只有出場成本，併入前一個持倉日（不增加分母）。
    S4：不可評估交易沿整個持倉區間 entry..exit−1 均攤 −100%，每日各計一個持倉。"""
    day_sum, day_cnt = [0.0] * we, [0] * we
    for t in trs:
        path = core.daily_path(syms[t["symbol"]], t, we)
        held = t["exit"] - t["entry"]
        if len(path) > held:
            path = path[:held - 1] + [path[held - 1] + path[held]]
        for k, v in enumerate(path):
            d = t["entry"] + k
            if 0 <= d < we:
                day_sum[d] += v
                day_cnt[d] += 1
    for t in extra_unevaluable:
        held = t["exit"] - t["entry"] if fin(t["exit"]) and t["exit"] > t["entry"] else 1
        for k in range(held):
            d = t["entry"] + k
            if 0 <= d < we:
                day_sum[d] += -1.0 / held
                day_cnt[d] += 1
    return [day_sum[d] / day_cnt[d] for d in range(we) if day_cnt[d] > 0]


def run_boot(vals, kw, kb, series, ex_vals, ex_kw, ex_kb, B=10_000):
    """對一組（淨報酬、超額、日序列）執行所有必要 bootstrap。群集數 <30 或天數 <360 -> None（抽樣前檢查）。"""
    boot = {}

    def cb(name, v, k):
        if len(set(k)) < 30:
            boot[name] = None
            return
        C, stats = core.cluster_bootstrap(v, k, B=B)
        boot[name] = (C, core.lower_bound(stats))

    cb("net_a", vals, kw)
    cb("net_b", vals, kb)
    cb("ex_a", ex_vals, ex_kw)
    cb("ex_b", ex_vals, ex_kb)
    boot["net_c"] = (len(series), core.lower_bound(core.stationary_bootstrap(series, B=B))) if len(series) >= 360 else None
    return boot


def main_run(names, syms, save_results=True, B=10_000, header_hashes=True):
    t_start = time.time()
    out = io.StringIO()

    def P(*a):
        line = " ".join(str(x) for x in a)
        print(line, flush=True)
        out.write(line + "\n")

    def save():
        if save_results:
            open(os.path.join(HERE, "results.txt"), "w", encoding="utf-8", newline="\n").write(out.getvalue())
        return out.getvalue()

    P("trend-momentum Stage 0 結果（純標準函式庫實作）")
    utf8_stdout()
    P("規格版本：SPEC v3.2（§9）＋§10 實作登記（v3.3／v3.4）")
    P("SPEC.md SHA-256:", file_hash(os.path.join(HERE, "SPEC.md")) if header_hashes else "(略)")
    P("population.csv SHA-256:", file_hash(os.path.join(HERE, "data", "population.csv")) if header_hashes else "(略)")
    P("偏離凍結規格的實作說明：亂數用 Python random.Random（Mersenne Twister，種子 20261107），非 SPEC 的 numpy PCG64（環境沒有 numpy）；分位數為線性內插。")
    P("未實作的描述項目（不影響判定）：流動性門檻 {10M,50M} 敏感度、樂觀對照（t+1 開盤）、各年度拆分；組合權益快照 = 前一日累計 + 當日 00:00 時刻的項目（出場成本、午夜資金費、強平於強平日入帳）；獨立終止證據僅用占位日（月檔缺失者視為資料缺漏、不可評估）。")
    P(f"標的 {len(syms)}")

    # ============ 樣本內：資金費只用切點以前（含切點時刻）的紀錄 ============
    for s in syms.values():
        s.set_funding_limit(D0 + CUT * DAY)
    targets = {N: {nm: core.compute_targets(syms[nm], N) for nm in names} for N in NS}
    is_res = {}
    for N in NS:
        trades, voided = [], []
        for nm in names:
            t, v = core.simulate(syms[nm], N, targets[N][nm], 0, CUT)
            trades += t
            voided += v
        is_res[N] = (trades, voided, summarize(trades, voided))
        s = is_res[N][2]
        P(f"[樣本內 N={N}] 交易 {s['n_trades']}（可評估 {s['n_eval']}、不可評估 {s['n_uneval']}）、作廢進場 {s['n_voided']}；"
          f"自然完成去重 {s['natural_dedup']}、進場週 {s['weeks']}；出場原因 {s['reasons']}；"
          f"平均淨報酬 main={fmt(s['mean_main'])} S1={fmt(s['mean_S1'])} S2={fmt(s['mean_S2'])} S3={fmt(s['mean_S3'])} S4={fmt(s['mean_S4'])} S5={fmt(s['mean_S5'])}")
    valid = [N for N in NS if is_res[N][2]["natural_dedup"] >= 200 and fin(is_res[N][2]["mean_main"])]
    if not valid:
        P("沒有任何 N 的樣本內自然完成去重事件 ≥ 200：整體 UNKNOWN")
        return save()
    chosen = max(valid, key=lambda N: (is_res[N][2]["mean_main"], -N))
    P(f"選定 N={chosen}（樣本內最高平均淨報酬；平手取較小 N）")
    trades_is, voided_is, s_is = is_res[chosen]

    def baseline_excess(trades, ws, we, include_uneval):
        """-> (evaluable: [(trade, excess)], uneval_s4: [(trade, excess_s4)], dropped, n_eval, cand_total, cand_bad)。"""
        cache, elig_cache = {}, {}
        res, res4, dropped = [], [], 0

        def base(t):
            sym = syms[t["symbol"]]
            L = t["exit"] - t["entry"]
            key = (t["symbol"], t["dir"], L)
            if key not in cache:
                if t["symbol"] not in elig_cache:
                    elig_cache[t["symbol"]] = core.eligible_days(sym, chosen, ws, we)
                cache[key] = core.baseline_mean(sym, t["dir"], L, we, elig_cache[t["symbol"]])
            return cache[key][0]

        for t in trades:
            if t["nets"] is not None:
                b = base(t)
                if b is None:
                    dropped += 1
                else:
                    res.append((t, t["nets"]["main"] - b))
            elif include_uneval and fin(t["exit"]) and t["exit"] > t["entry"]:
                b = base(t)
                if b is not None:
                    res4.append((t, -1.0 - b))
        cand = sum(v[3] for v in cache.values())
        bad = sum(v[2] for v in cache.values())
        return res, res4, dropped, sum(1 for t in trades if t["nets"] is not None), cand, bad

    t0 = time.time()
    ev_is = [t for t in trades_is if t["nets"] is not None]
    un_is = [t for t in trades_is if t["nets"] is None]
    ex_is, ex4_is, dropped_is, nev_is, cand_is, bad_is = baseline_excess(trades_is, 0, CUT, True)
    P(f"IS 基準：耗時 {time.time() - t0:.0f}s；無候選剔除 {dropped_is}/{nev_is}；候選不可評估 {bad_is}/{cand_is}；S4 不可評估交易 {len(un_is)} 筆，其中建立超額 {len(ex4_is)} 筆")

    nv = [t["nets"]["main"] for t in ev_is]
    boot = run_boot(nv, [core.iso_week(t["entry"]) for t in ev_is], [t["entry"] // 28 for t in ev_is], daily_series(syms, ev_is, [], CUT),
                    [v for _, v in ex_is], [core.iso_week(t["entry"]) for t, _ in ex_is], [t["entry"] // 28 for t, _ in ex_is], B=B)
    s4_boot = None
    if un_is:
        allt = ev_is + un_is
        nv4 = nv + [-1.0] * len(un_is)
        ex_all = ex_is + ex4_is
        s4_boot = run_boot(nv4, [core.iso_week(t["entry"]) for t in allt], [t["entry"] // 28 for t in allt], daily_series(syms, ev_is, un_is, CUT),
                           [v for _, v in ex_all], [core.iso_week(t["entry"]) for t, _ in ex_all], [t["entry"] // 28 for t, _ in ex_all], B=B)
    P("bootstrap 下界（Bonferroni 0.05/3 分位）:", {k: (None if v is None else (v[0], round(v[1], 6))) for k, v in boot.items()})
    if s4_boot is not None:
        P("S4 bootstrap 下界:", {k: (None if v is None else (v[0], round(v[1], 6))) for k, v in s4_boot.items()})

    # ============ 樣本外：資金費改用全部紀錄（只看選定的 N、只看一次）============
    for s in syms.values():
        s.set_funding_limit(None)
    trades_oos, voided_oos = [], []
    for nm in names:
        t, v = core.simulate(syms[nm], chosen, targets[chosen][nm], CUT, TOTAL)
        trades_oos += t
        voided_oos += v
    s_oos = summarize(trades_oos, voided_oos)
    P(f"[樣本外 N={chosen}] 交易 {s_oos['n_trades']}（不可評估 {s_oos['n_uneval']}）、作廢 {s_oos['n_voided']}；"
      f"自然完成去重 {s_oos['natural_dedup']}、週 {s_oos['weeks']}；出場原因 {s_oos['reasons']}；平均淨報酬 main={fmt(s_oos['mean_main'])}")
    t0 = time.time()
    ex_oos, _x, dropped_oos, nev_oos, cand_oos, bad_oos = baseline_excess(trades_oos, CUT, TOTAL, False)
    P(f"OOS 基準：耗時 {time.time() - t0:.0f}s；無候選剔除 {dropped_oos}/{nev_oos}；候選不可評估 {bad_oos}/{cand_oos}")
    ex_info = {
        "mean": mean([v for _, v in ex_is]),
        "mean_s4": mean([v for _, v in ex_is + ex4_is]),
        "s4_unevaluable": len(ex4_is) < len(un_is) or dropped_is / max(nev_is, 1) > 0.05 or not ex_is or bad_is / max(cand_is, 1) > 0.05,
        "oos_mean": mean([v for _, v in ex_oos]),
        "unevaluable": (dropped_is / max(nev_is, 1) > 0.05) or (bad_is / max(cand_is, 1) > 0.05) or not ex_is,
        "oos_unevaluable": (dropped_oos / max(nev_oos, 1) > 0.05) or (bad_oos / max(cand_oos, 1) > 0.05) or not ex_oos,
    }
    status, rows = judge_qualification(s_is, s_oos, boot, ex_info, s4_boot)
    P("== 合格判定 ==")
    for name, st, why in rows:
        P(f"  [{st}] {name}：{why}")
    P(f"合格通過：{status}（任何 PASS 都只稱『條件式通過（母體完整性未證實）』）")

    # 組合與月報酬（樣本外）
    items, invalid = [], set()
    first_bad = min((t["entry"] for t in trades_oos if t["nets"] is None), default=None)
    if first_bad is not None:                     # 不可評估交易會改變其後的成交選擇與權益：自其進場月起的所有月份都失效
        for d in range(first_bad, TOTAL):
            invalid.add(month_key(d))
    for t in trades_oos:
        if t["nets"] is None:
            continue
        sym = syms[t["symbol"]]
        a = sym.avg30[t["entry"] - 2] if t["entry"] >= 2 else core.NAN
        path, mid = core.daily_path_parts(sym, t, TOTAL)
        items.append((t, path, a, mid))
    pnl, acc, skp, eq_end = core.portfolio_daily(items, CUT, TOTAL)
    months, excluded = monthly_returns(pnl, CUT, TOTAL, invalid)
    rets = sorted(r for _, r in months)
    med = (rets[len(rets) // 2] if len(rets) % 2 else (rets[len(rets) // 2 - 1] + rets[len(rets) // 2]) / 2) if rets else core.NAN
    worst = rets[0] if rets else core.NAN
    P(f"組合（樣本外）：接受 {acc}、未取得名額 {skp}、期末權益 {eq_end:.4f}；完整有效月數 {len(months)}；月報酬中位數 {fmt(med, 4)}、最差 {fmt(worst, 4)}")
    P("月報酬:", ", ".join(f"{k}:{v:+.4f}" for k, v in months))
    P("被排除的月份:", excluded)
    tstat, twhy = judge_target(status, len(months), med, worst)
    P(f"目標通過：{tstat}（{twhy}）")

    def desc(trs, label):
        ev = [t for t in trs if t["nets"] is not None]
        nets = [t["nets"]["main"] for t in ev]
        win = [x for x in nets if x > 0]
        loss = [x for x in nets if x < 0]
        streak = best = 0
        for t in sorted(ev, key=lambda z: (z["entry"], z["symbol"], z["id"])):
            streak = streak + 1 if t["nets"]["main"] < 0 else 0
            best = max(best, streak)
        by_dir = {d: mean([t["nets"]["main"] for t in ev if t["dir"] == d]) for d in (1, -1)}
        by_sym = {}
        for t in ev:
            by_sym[t["symbol"]] = by_sym.get(t["symbol"], 0.0) + t["nets"]["main"]
        top = sorted(by_sym.values(), reverse=True)[:10]
        den = sum(abs(v) for v in by_sym.values())
        P(f"[{label}] 勝率 {len(win) / max(len(nets), 1):.3f}（零報酬不計勝）、平均盈 {fmt(mean(win))}、平均虧 {fmt(mean(loss))}、最大連續虧損 {best}；"
          f"多單平均 {fmt(by_dir[1])}、空單平均 {fmt(by_dir[-1])}；前10名標的淨損益和 {sum(top):.3f}／Σ|.| {den:.3f}")

    desc(trades_is, f"樣本內 N={chosen}")
    desc(trades_oos, f"樣本外 N={chosen}")
    P(f"總耗時 {time.time() - t_start:.0f}s")
    return save()


def main():
    utf8_stdout()
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("prep", "run"):
        sys.exit("用法：python -I analyze.py prep|run")
    names, syms = load_all()
    h = write_termination(syms, names)
    print("termination.csv SHA-256：", h, flush=True)
    if mode == "prep":
        print("prep 完成：請把『termination.csv SHA-256：<上列雜湊>』追加到 FREEZE.txt 後再執行 run。")
        return
    frz = open(os.path.join(HERE, "FREEZE.txt"), encoding="utf-8").read()
    spec_h = hashlib.sha256(open(os.path.join(HERE, "SPEC.md"), "rb").read()).hexdigest()
    pop_h = hashlib.sha256(open(os.path.join(HERE, "data", "population.csv"), "rb").read()).hexdigest()
    for what, hv in (("termination.csv", h), ("SPEC.md", spec_h), ("population.csv", pop_h)):
        if hv not in frz:
            sys.exit(f"FREEZE.txt 沒有目前 {what} 的雜湊（{hv[:12]}…）：請先登記再 run")
    main_run(names, syms)


if __name__ == "__main__":
    main()
