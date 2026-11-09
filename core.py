"""trend-momentum 核心邏輯（純標準函式庫，無檔案與網路；規格 SPEC.md v3.2）。

天索引 i = (UTC 日 00:00 毫秒 − D0) // DAY；i = 0 為 2020-01-01，TOTAL = 2496 天（至 2026-10-31）。
所有時間點都在 00:00 UTC：進場價 = 第 e 日開盤；訊號出場價 = 第 x 日開盤；資料終止與窗末以第 x−1 日收盤（時間為 x 日 00:00）。
資金費資料有「可見上限」：Sym.set_funding_limit(limit_ms) 之後，所有資金費相關判斷只用時戳 ≤ limit_ms 的資料（樣本內用切點）。
"""
import bisect
import math
import random

D0 = 1_577_836_800_000
DAY = 86_400_000
TOTAL = 2496
CUT = 1872                       # 2025-02-15
FEE = 0.0005                     # 每邊 taker 0.05%
NAN = float("nan")
INF_DAY = 10 ** 9
LIQ = 0.95                       # 強平代理：不利方向日內極端 ≥ 95%
SEED = 20261107
HOUR_MS = 3_600_000


def isnan(x):
    return x != x


def slip_bps(avg):
    """依 information_cutoff 當時 30 日平均成交額分層的每邊滑價（bps）；NaN（資料不全）取最保守的一層。"""
    if isnan(avg):
        return 50.0
    if avg >= 100_000_000:
        return 3.0
    if avg >= 20_000_000:
        return 8.0
    if avg >= 10_000_000:
        return 25.0
    return 50.0


class SparseMax:
    """區間最大值（O(1) 查詢）。"""

    def __init__(self, vals):
        self.t = [list(vals)]
        n = len(vals)
        k = 1
        while (1 << k) <= n:
            prev = self.t[-1]
            half = 1 << (k - 1)
            self.t.append([max(prev[i], prev[i + half]) for i in range(n - (1 << k) + 1)])
            k += 1

    def query(self, a, b):
        if a > b:
            return -math.inf
        k = (b - a + 1).bit_length() - 1
        row = self.t[k]
        return max(row[a], row[b - (1 << k) + 1])


class Sym:
    """單一標的。k_rows: [(t_ms, o, h, l, c, qv)]；f_rows: [(t_ms, interval_h, rate)]。"""

    def __init__(self, name, k_rows, f_rows):
        n = TOTAL
        self.name = name
        self.o = [NAN] * n
        self.h = [NAN] * n
        self.l = [NAN] * n
        self.c = [NAN] * n
        self.qv = [NAN] * n
        for t, o, h, l, c, qv in k_rows:
            i = (t - D0) // DAY
            if 0 <= i < n:
                self.o[i], self.h[i], self.l[i], self.c[i], self.qv[i] = o, h, l, c, qv
        self.has = [not isnan(x) for x in self.o]
        # 占位日：quote volume = 0、O=H=L=C、且 close 等於前一個 UTC 日曆日的 close（SPEC §9 B0）
        self.ph = [False] * n
        for i in range(1, n):
            if self.has[i] and self.has[i - 1] and self.qv[i] == 0 and self.o[i] == self.h[i] == self.l[i] == self.c[i] \
                    and self.c[i] == self.c[i - 1]:
                self.ph[i] = True
        self.tradable = [self.has[i] and not self.ph[i] for i in range(n)]
        self.first_trad = next((i for i in range(n) if self.has[i] and self.qv[i] > 0), None)
        # 缺資料累計與 30 日平均成交額（含 i 在內共 30 個日曆日，須齊全；只用 i 以前的資料）
        self.miss_cum = [0] * (n + 1)
        qsum = [0.0] * (n + 1)
        for i in range(n):
            self.miss_cum[i + 1] = self.miss_cum[i] + (0 if self.has[i] else 1)
            qsum[i + 1] = qsum[i] + (self.qv[i] if self.has[i] else 0.0)
        self.avg30 = [NAN] * n
        for i in range(29, n):
            if self.miss_cum[i + 1] - self.miss_cum[i - 29] == 0:
                self.avg30[i] = (qsum[i + 1] - qsum[i - 29]) / 30.0
        # 下一個占位日（嚴格大於 i）；下一個缺資料日（≥ i）。呼叫端須自行限制在窗內使用。
        self.next_ph = [INF_DAY] * (n + 1)
        nxt = INF_DAY
        for i in range(n - 1, -1, -1):
            self.next_ph[i] = nxt
            if self.ph[i]:
                nxt = i
        self.next_miss = [INF_DAY] * (n + 1)
        nxt = INF_DAY
        for i in range(n - 1, -1, -1):
            if not self.has[i]:
                nxt = i
            self.next_miss[i] = nxt
        self.f_rows_all = sorted(f_rows)
        self._hi = None
        self._lo = None
        self.set_funding_limit(None)

    # ---- 資金費（可見上限）----
    def set_funding_limit(self, limit_ms):
        """只使用時戳 ≤ limit_ms 的資金費紀錄建立所有資金費相關結構（None = 全部）。"""
        rows = [r for r in self.f_rows_all if limit_ms is None or r[0] <= limit_ms]
        self.ft = [r[0] for r in rows]
        self.fiv = [r[1] for r in rows]
        self.fr = [r[2] for r in rows]
        self.cw = [0.0]
        self.pb_cum = [0]
        self.gb_cum = [0]
        for k, (t, iv, r) in enumerate(rows):
            d = (t - D0) // DAY
            proxy = self.c[d - 1] if 1 <= d <= TOTAL else NAN      # 結算前最後一根完整日 K 收盤
            bad = isnan(proxy)
            self.cw.append(self.cw[-1] + (0.0 if bad else r * proxy))
            self.pb_cum.append(self.pb_cum[-1] + (1 if bad else 0))
            gap_bad = k >= 1 and (t - rows[k - 1][0]) > max(iv, rows[k - 1][1]) * HOUR_MS + 60_000      # 週期轉換：間隔不超過新舊週期較大者（已用 814 個真實轉換點驗證）
            self.gb_cum.append(self.gb_cum[-1] + (1 if gap_bad else 0))

    def fund_ok(self, e, x):
        """持倉 (e 日 00:00, x 日 00:00] 的資金費結算是否完整（逐筆交易區間判定，只用可見資料）：
        區間內至少有一筆；第一筆距進場不超過 60 秒，或與其前一筆的間隔不超過（新舊週期較大者＋60 秒）；區間內相鄰結算間隔同上限；
        若可見資料在出場之後還有下一筆，且預期的下一筆結算時間（最後一筆＋1 週期）不晚於出場時刻（容許 2 毫秒，代表它屬於本交易），
        則最後一筆到下一筆的間隔也受同一上限（出場時刻結算若遺失會露出）；時戳晚於出場的結算屬於下一部位，其缺失不影響本交易；
        若可見資料止於出場之前，最後一筆距出場不超過 1 個週期加 60 秒；每筆都有結算前一日收盤可用。區間外的缺口不影響。
        已知限制：資料止於出場時刻、且恰在出場時刻的結算（時戳 .000）遺失時無法偵測。"""
        if e < 0 or x > TOTAL or x <= e:
            return False
        e_ms, x_ms = D0 + e * DAY, D0 + x * DAY
        a = bisect.bisect_right(self.ft, e_ms)
        b = bisect.bisect_right(self.ft, x_ms)
        n = len(self.ft)
        if b == a:
            return False
        first_ok = (self.ft[a] - e_ms <= 60_000) or (a > 0 and self.ft[a] - self.ft[a - 1] <= max(self.fiv[a], self.fiv[a - 1]) * HOUR_MS + 60_000)
        if not first_ok:
            return False
        # 緊接在出場之後的下一筆：只有當預期結算時間（最後一筆＋1 週期）落在出場時刻之前（容許 2 毫秒）時，缺失的結算才屬於本交易
        owns_next = b < n and self.ft[b - 1] + self.fiv[b - 1] * HOUR_MS <= x_ms + 2
        hi = b if owns_next else b - 1
        if self.gb_cum[hi + 1] - self.gb_cum[a + 1] > 0:
            return False
        if b == n and x_ms - self.ft[b - 1] > self.fiv[b - 1] * HOUR_MS + 60_000:
            return False
        return self.pb_cum[b] - self.pb_cum[a] == 0

    def funding_sum(self, dr, e, x, pe):
        """持倉 (e 日 00:00, x 日 00:00] 的資金費（佔進場名目；多單付正費率）。"""
        a = bisect.bisect_right(self.ft, D0 + e * DAY)
        b = bisect.bisect_right(self.ft, D0 + x * DAY)
        return dr * (self.cw[b] - self.cw[a]) / pe

    # ---- K 線輔助 ----
    def hi_table(self):
        if self._hi is None:
            self._hi = SparseMax([x if not isnan(x) else -math.inf for x in self.h])
        return self._hi

    def lo_table(self):
        if self._lo is None:
            self._lo = SparseMax([-x if not isnan(x) else -math.inf for x in self.l])
        return self._lo

    def data_ok(self, a, b):
        """[a, b) 內的日 K 都有資料。"""
        return a >= 0 and b <= TOTAL and self.miss_cum[b] - self.miss_cum[a] == 0

    def liquidated(self, dr, pe, a, b):
        """日 [a, b] 閉區間內是否觸發強平代理（多單 1 − low/pe ≥ 95%；空單 high/pe − 1 ≥ 95%）。"""
        if a > b:
            return False
        if dr > 0:
            low = -self.lo_table().query(a, b)
            return 1.0 - low / pe >= LIQ
        high = self.hi_table().query(a, b)
        return high / pe - 1.0 >= LIQ


# ---------------------------------------------------------------- 訊號
def signal_ok(sym, N, t):
    """第 t 日收盤的訊號資格（基準與策略共用）：t−N..t 共 N+1 根日 K 齊全且 t 為可交易日，上市天數（自第一個成交量>0 日）≥ max(N+1, 30)。"""
    if t < N or sym.first_trad is None or t < sym.first_trad or not sym.tradable[t]:
        return False
    if not sym.data_ok(t - N, t + 1):
        return False
    return t - sym.first_trad + 1 >= max(N + 1, 30)


def compute_targets(sym, N):
    """每日目標部位（第 t 日收盤後可得）：+1／−1／0。含遲滯：同向續抱只要 30 日均量 ≥ 10M，新方向須 ≥ 20M 且 signal_ok。"""
    n = TOTAL
    T = [0] * n
    prev = 0
    for t in range(n):
        if not sym.tradable[t]:
            prev = 0
            continue
        s = 0
        if t >= N and sym.has[t - N]:
            r = sym.c[t] / sym.c[t - N] - 1.0
            s = 1 if r > 0 else (-1 if r < 0 else 0)
        a = sym.avg30[t]
        if s == 0 or isnan(a) or t < N or not sym.data_ok(t - N, t + 1):
            v = 0
        elif prev == s:
            v = s if a >= 10_000_000 else 0
        else:
            v = s if (a >= 20_000_000 and signal_ok(sym, N, t)) else 0
        T[t] = v
        prev = v
    return T


# ---------------------------------------------------------------- 淨報酬
def net_return(sym, dr, e, x, exit_px, slip_mult=1.0):
    """佔進場名目的淨報酬；資料或資金費不全回傳 None。e = 進場日，x = 出場時間（x 日 00:00）。"""
    pe = sym.o[e]
    if isnan(pe) or isnan(exit_px) or not sym.data_ok(e, x) or not sym.fund_ok(e, x):
        return None
    se = slip_bps(sym.avg30[e - 2] if e >= 2 else NAN) * slip_mult / 1e4
    sx = slip_bps(sym.avg30[x - 2] if x >= 2 else NAN) * slip_mult / 1e4
    ratio = exit_px / pe
    gross = dr * (ratio - 1.0)
    cost = (FEE + se) + (FEE + sx) * ratio
    v = gross - cost - sym.funding_sum(dr, e, x, pe)
    return v if math.isfinite(v) else None


def scenario_nets(sym, tr):
    """對一筆已完成的交易 dict 計算各情境淨報酬（None = 不可評估）。"""
    dr, e, x, reason = tr["dir"], tr["entry"], tr["exit"], tr["reason"]
    if reason == "liq":
        v = -1.0
        return {"main": v, "S1": v, "S2": v, "S3": v, "S5": v}
    main = net_return(sym, dr, e, x, tr["exit_px"], 1.0)
    if main is None:
        return None
    s1 = net_return(sym, dr, e, x, tr["exit_px"], 3.0)
    if s1 is None:
        return None
    out = {"main": main, "S1": s1, "S2": main, "S3": s1, "S5": main}
    if reason == "term":
        out["S2"] = main - 0.10
        out["S3"] = s1 - 0.10
        out["S5"] = -0.25
    return out


# ---------------------------------------------------------------- 模擬
def simulate(sym, N, T, ws, we):
    """在窗 [ws, we) 內模擬一個標的。回傳 (trades, voided)。
    trade: dict(id,symbol,N,dir,entry,exit,entry_px,exit_px,reason,nets|None)。reason: sig／miss／liq／term／cens。
      sig = 目標部位改變造成的自然完成；miss = 因缺資料無法成交而結束（不計自然完成）。
    voided: dict(id,symbol,N,dir,entry)：預定 execution_time 落在不可交易日或缺資料日的進場事件。
    只使用 [0, we) 的資料：占位偵測只看到 d+1 < we。"""
    trades, voided = [], []
    pos = None

    def close(pos, x, px, reason):
        pos["exit"], pos["exit_px"], pos["reason"] = x, px, reason
        trades.append(pos)

    for d in range(ws, we):
        tgt = T[d - 2] if d >= 2 else 0
        prv = T[d - 3] if d >= 3 else 0
        # (2) 開盤退出
        if pos is not None and tgt != pos["dir"]:
            if sym.tradable[d]:
                close(pos, d, sym.o[d], "sig")
            else:
                close(pos, d, NAN, "miss")
            pos = None
        # (3) 開盤進場（目標部位改變才算進場事件）
        if pos is None and tgt != 0 and tgt != prv:
            if sym.tradable[d]:
                pos = {"id": f"{sym.name}|{N}|{tgt}|{d}", "symbol": sym.name, "N": N, "dir": tgt, "entry": d,
                       "entry_px": sym.o[d], "signal_day": d - 2}
            else:
                voided.append({"id": f"{sym.name}|{N}|{tgt}|{d}", "symbol": sym.name, "N": N, "dir": tgt, "entry": d})
        if pos is None:
            continue
        # (4) 強平代理（進場日也算）
        if not sym.has[d]:
            close(pos, d, NAN, "miss")
            pos = None
            continue
        pe = pos["entry_px"]
        hit = (sym.l[d] <= pe * (1.0 - LIQ)) if pos["dir"] > 0 else (sym.h[d] >= pe * (1.0 + LIQ))
        if hit:
            close(pos, d + 1, sym.c[d], "liq")
            pos = None
            continue
        # (5) 次日是占位日 -> 終止；窗末 -> 行政截尾
        if d + 1 < we:
            if sym.ph[d + 1]:
                close(pos, d + 1, sym.c[d], "term")
                pos = None
        elif d == we - 1:
            close(pos, d + 1, sym.c[d], "cens")
            pos = None
    for tr in trades:
        tr["nets"] = scenario_nets(sym, tr)
    return trades, voided


# ---------------------------------------------------------------- 去重與統計
def dedup_flags(events):
    """events: [{'symbol','entry',...}]（含作廢者）。依 (entry, symbol) 排序，同標的 7 日冷卻貪婪取樣，回傳 id(event) -> counted。"""
    last = {}
    out = {}
    for ev in sorted(events, key=lambda z: (z["entry"], z["symbol"])):
        l = last.get(ev["symbol"])
        if l is None or ev["entry"] - l >= 7:
            out[id(ev)] = True
            last[ev["symbol"]] = ev["entry"]
        else:
            out[id(ev)] = False
    return out


def iso_week(day):
    import datetime as dt
    d = (dt.datetime(2020, 1, 1) + dt.timedelta(days=day)).date()
    y, w, _ = d.isocalendar()
    return f"{y}-{w:02d}"


def quantile(vals, q):
    """線性內插分位數（等同 numpy.quantile 預設 method='linear'）。"""
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return NAN
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def cluster_bootstrap(values, keys, B=10_000, seed=SEED):
    """群集 bootstrap：重抽群集後合併所有觀察求平均。回傳 (n_clusters, [B 個統計量])；空輸入回傳 (0, [])。"""
    sums, cnts, idx = [], [], {}
    for v, k in zip(values, keys):
        if k not in idx:
            idx[k] = len(sums)
            sums.append(0.0)
            cnts.append(0)
        sums[idx[k]] += v
        cnts[idx[k]] += 1
    C = len(sums)
    if C == 0:
        return 0, []
    rng = random.Random(seed)
    out = []
    rr = rng.randrange
    for _ in range(B):
        s = 0.0
        c = 0
        for _i in range(C):
            j = rr(C)
            s += sums[j]
            c += cnts[j]
        out.append(s / c)
    return C, out


def stationary_bootstrap(series, mean_block=28, B=10_000, seed=SEED):
    """平穩 bootstrap（幾何分佈區塊長，環狀）求平均；空序列回傳 []。"""
    n = len(series)
    if n == 0:
        return []
    rng = random.Random(seed)
    p = 1.0 / mean_block
    rnd, rr = rng.random, rng.randrange
    out = []
    for _ in range(B):
        j = rr(n)
        s = 0.0
        for _i in range(n):
            s += series[j]
            j = rr(n) if rnd() < p else (j + 1) % n
        out.append(s / n)
    return out


BONF_Q = 0.05 / 3


def lower_bound(stats):
    return quantile(stats, BONF_Q)


# ---------------------------------------------------------------- 隨機基準（全列舉）
def eligible_days(sym, N, ws, we):
    """窗 [ws, we) 內滿足進場資格的 execution 日 e：e 為可交易日；資訊日 t=e−2 的 30 日均量 ≥ 20M 且 signal_ok（暖機與上市天數）。"""
    out = []
    for e in range(max(ws, 2), we):
        if not sym.tradable[e]:
            continue
        t = e - 2
        a = sym.avg30[t]
        if isnan(a) or a < 20_000_000 or not signal_ok(sym, N, t):
            continue
        out.append(e)
    return out


def baseline_mean(sym, dr, L, we, elig):
    """同標的、同方向、持有 L 天的候選進場（elig）之平均淨報酬（主要情境）。
    原定出場 e+L 在窗末或之後者於窗末行政截尾；碰到占位日者於占位日終止（不剔除）；觸發強平代理者記 −1（依時間順序先於缺資料判斷）。
    回傳 (mean|None, n_used, n_uneval, n_total_candidates)。"""
    tot, used, bad, cand = 0.0, 0, 0, 0
    for e in elig:
        x0 = e + L
        cand += 1
        D = sym.next_ph[e]
        if D < we and D <= x0:
            x, px = D, sym.c[D - 1]
        elif x0 >= we:                       # 原定出場在窗末或之後：行政截尾（SPEC §9 B5）
            x, px = we, sym.c[we - 1]
        else:
            x, px = x0, sym.o[x0]
        pe = sym.o[e]
        if isnan(pe):
            bad += 1
            continue
        m = sym.next_miss[e]
        last_ok = min(x - 1, m - 1) if m < INF_DAY else x - 1       # 缺資料之前的最後一天
        if sym.liquidated(dr, pe, e, last_ok):
            tot += -1.0
            used += 1
            continue
        if m < x:
            bad += 1
            continue
        v = net_return(sym, dr, e, x, px, 1.0)
        if v is None:
            bad += 1
            continue
        tot += v
        used += 1
    return (tot / used if used else None), used, bad, cand


# ---------------------------------------------------------------- 日損益路徑與組合
def daily_path_parts(sym, tr, we):
    """交易的逐日損益（佔進場名目；加總 = 主要情境淨報酬）與其中『當日 00:00 時刻』發生的部分。
    日 j 的損益 = 開盤到下一開盤的價格變動；進場成本記在進場日；發生在 x 日 00:00 的事項（出場成本、時戳在該日開頭 60 秒內的資金費）
    記在 x 日（x ≥ we 時併入 we−1）。回傳 (vals, mid)，長度皆為 last−entry+1，last = x（x < we）或 x−1。
    mid[j] = vals[j] 中發生在 (entry+j) 日 00:00 的金額（含上一日留下的出場成本不在此列；出場成本與午夜資金費在 x 日的 mid）。"""
    dr, e, x, pe = tr["dir"], tr["entry"], tr["exit"], tr["entry_px"]
    liq = tr["reason"] == "liq"
    last = x - 1 if liq else (x if x < we else x - 1)          # 強平：最終損失在強平日（x−1）入帳，沒有一般出場日
    vals = [0.0] * (last - e + 1)
    mid = [0.0] * (last - e + 1)
    for j in range(e, x):
        p0 = sym.o[j]
        p1 = sym.o[j + 1] if j + 1 < x else tr["exit_px"]
        vals[j - e] += dr * (p1 - p0) / pe
    ratio = tr["exit_px"] / pe
    se = slip_bps(sym.avg30[e - 2] if e >= 2 else NAN) / 1e4
    sx = slip_bps(sym.avg30[x - 2] if x >= 2 else NAN) / 1e4
    vals[0] -= FEE + se
    cost_x = 0.0 if liq else (FEE + sx) * ratio
    vals[last - e] -= cost_x
    if x < we and not liq:
        mid[last - e] -= cost_x
    a = bisect.bisect_right(sym.ft, D0 + e * DAY)
    b = bisect.bisect_right(sym.ft, D0 + x * DAY)
    for k in range(a, b):
        day = (sym.ft[k] - D0) // DAY
        proxy = sym.c[day - 1] if day >= 1 else NAN
        j = min(max(day, e), last) - e
        if not isnan(proxy):
            f = dr * sym.fr[k] * proxy / pe
            vals[j] -= f
            if sym.ft[k] - (D0 + day * DAY) < 60_000 and day <= last:
                mid[j] -= f
    adj = tr["nets"]["main"] - sum(vals)
    vals[last - e] += adj
    return vals, mid


def daily_path(sym, tr, we):
    return daily_path_parts(sym, tr, we)[0]


def portfolio_daily(items, ws, we, notional=0.05, max_pos=20):
    """items: [(trade, path, avg30_info[, mid])]（僅可評估交易；path／mid 由 daily_path_parts 產生，起於進場日）。
    SPEC §5 組合規則：初始權益 1、不複利、固定名目、100% 抵押、名額上限；同日先退出後進場，依 avg30 由大到小再依代號排序，
    未取得名額不重試。權益快照 = 前一日收盤累計 + 當日 00:00 時刻的項目（出場成本、午夜資金費；有提供 mid 時）。
    回傳 (daily_pnl[we-ws], accepted_count, skipped_count, equity_end)。"""
    by_entry = {}
    for it in items:
        by_entry.setdefault(it[0]["entry"], []).append(it)
    holding = []                        # (exit_day, ...)：占用名額與抵押到 exit_day 開盤
    active = []                         # 仍有損益要入帳的 (path, mid, start_day)
    pnl = [0.0] * (we - ws)
    equity = 1.0
    accepted = skipped = 0
    for d in range(ws, we):
        holding = [p for p in holding if p[0] > d]
        if equity > 0:
            midnight = 0.0
            for path, mid, st in active:
                k = d - st
                if mid is not None and 0 <= k < len(mid):
                    midnight += notional * mid[k]
            snapshot = equity + midnight
            cands = sorted(by_entry.get(d, []), key=lambda it: (-(it[2] if not isnan(it[2]) else 0.0), it[0]["symbol"]))
            for it in cands:
                tr, path = it[0], it[1]
                mid = it[3] if len(it) > 3 else None
                free = snapshot - notional * len(holding)
                if len(holding) < max_pos and free >= notional - 1e-12:      # 浮點容差
                    holding.append((tr["exit"],))
                    active.append((path, mid, d))
                    accepted += 1
                else:
                    skipped += 1
        day_pnl = 0.0
        keep = []
        for path, mid, st in active:
            k = d - st
            if 0 <= k < len(path):
                day_pnl += notional * path[k]
            if k + 1 < len(path):
                keep.append((path, mid, st))
        active = keep
        pnl[d - ws] = day_pnl
        equity += day_pnl
    return pnl, accepted, skipped, equity
