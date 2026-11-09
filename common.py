"""trend-momentum 共用：符號與檔名規則、資料列解析與驗證（純函式，無網路、無檔案）。規格見 SPEC.md（v3 凍結）。"""
import calendar
import datetime as dt
import math
import re

KLINE_ROOT = "data/futures/um/monthly/klines/"
FUND_ROOT = "data/futures/um/monthly/fundingRate/"
FIRST_MONTH, LAST_MONTH = "2020-01", "2026-10"             # SPEC §1：研究期（最後一個完整月；當月不用）
STABLE_BASES = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "USDE", "DAI", "EUR", "AEUR"}   # SPEC §1 穩定幣清單
SYM_RE = re.compile(r"^[^\W_]+USDT$")          # Unicode 字母數字（含中文代號，如 币安人生USDT）；排除底線、空白、逗號等
MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
DAY_MS = 86_400_000


def symbol_ok_format(sym):
    return SYM_RE.fullmatch(sym) is not None


def keep_symbol(sym):
    """SPEC §1 符號規則：以 USDT 結尾、不含 '_'、基礎幣不在穩定幣清單；格式只允許 Unicode 字母數字（排除底線、空白、逗號、斜線等）。"""
    if not symbol_ok_format(sym):
        return False
    return sym[:-4] not in STABLE_BASES


def valid_month(m):
    return isinstance(m, str) and MONTH_RE.fullmatch(m) is not None


def in_range(month):
    return valid_month(month) and FIRST_MONTH <= month <= LAST_MONTH


def parse_key(key):
    """嚴格解析資料站 key。回傳 dict(kind, symbol, month, checksum) 或 None（不符合預期結構）。
    預期：data/futures/um/monthly/klines/<SYM>/1d/<SYM>-1d-YYYY-MM.zip[.CHECKSUM]
          data/futures/um/monthly/fundingRate/<SYM>/<SYM>-fundingRate-YYYY-MM.zip[.CHECKSUM]"""
    for kind, root, mid in (("kline", KLINE_ROOT, "1d"), ("fund", FUND_ROOT, "fundingRate")):
        if not key.startswith(root):
            continue
        parts = key[len(root):].split("/")
        sym = parts[0]
        if kind == "kline":
            if len(parts) != 3 or parts[1] != "1d":
                return None
            name = parts[2]
        else:
            if len(parts) != 2:
                return None
            name = parts[1]
        checksum = name.endswith(".zip.CHECKSUM")
        if not (checksum or name.endswith(".zip")):
            return None
        stem = name[: -len(".zip.CHECKSUM")] if checksum else name[: -len(".zip")]
        prefix = f"{sym}-{mid}-"
        if not stem.startswith(prefix):
            return None
        month = stem[len(prefix):]
        if not valid_month(month):
            return None
        return {"kind": kind, "symbol": sym, "month": month, "checksum": checksum}
    return None


def month_bounds_ms(month):
    y, m = int(month[:4]), int(month[5:])
    start = dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)
    end = dt.datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=dt.timezone.utc)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def coverage(kline_zip_months, kline_cs_months, fund_zip_months, fund_cs_months):
    """逐標的覆蓋旗標（SPEC §1 缺漏規則的偵測；不下結論，只標記）。"""
    out = []

    def one(label, zips, css):
        zs, cs = sorted(set(zips)), set(css)
        if not zs:
            out.append(f"{label}:empty")
            if cs:
                out.append(f"{label}:orphan_checksum_only")
            return
        for m in sorted(cs - set(zs)):
            out.append(f"{label}:orphan_checksum:{m}")
        for m in sorted(set(zs) - cs):
            out.append(f"{label}:zip_without_checksum:{m}")
        y0, m0 = int(zs[0][:4]), int(zs[0][5:])
        y1, m1 = int(zs[-1][:4]), int(zs[-1][5:])
        n = (y1 - y0) * 12 + (m1 - m0) + 1
        if n != len(zs):
            have = set(zs)
            y, m = y0, m0
            for _ in range(n):
                s = f"{y:04d}-{m:02d}"
                if s not in have:
                    out.append(f"{label}:gap:{s}")
                m += 1
                if m == 13:
                    y, m = y + 1, 1
        if zs[-1] < LAST_MONTH:
            out.append(f"{label}:ends_early:{zs[-1]}")

    one("kline", kline_zip_months, kline_cs_months)
    one("fund", fund_zip_months, fund_cs_months)
    return out


def _num(s):
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _records(text, header_first):
    """依序產生 (行號, 欄位列表)；略過空行；去掉 BOM；只有第一個非空紀錄且第一欄等於已知標頭名稱時視為標頭。"""
    first = True
    for i, line in enumerate(text.lstrip("﻿").splitlines()):
        line = line.strip()
        if not line:
            continue
        f = [x.strip() for x in line.split(",")]
        if first:
            first = False
            if f[0] == header_first:
                continue
        yield i + 1, f


def parse_kline_csv(text, month):
    """日 K CSV（12 欄；第一個非空紀錄若第一欄為 open_time 視為標頭，其他非數字時戳一律是錯誤）
    -> (rows[(open_time_ms, open, high, low, close, quote_volume)], errors[str], n_raw)。
    驗證：欄位數、數值有限且價格為正、高低開收一致、成交額非負、時戳落在該月且對齊 UTC 日界、不重複、檔內嚴格遞增。
    n_raw = 非標頭、非空的原始資料列數；rows 只含通過驗證者。"""
    rows, errors, seen, n_raw, last = [], [], set(), 0, None
    lo, hi = month_bounds_ms(month)
    for ln, f in _records(text, "open_time"):
        n_raw += 1
        if len(f) != 12:
            errors.append(f"line{ln}:columns")
            continue
        if not f[0].isdigit():
            errors.append(f"line{ln}:time")
            continue
        t = int(f[0])
        o, h, l, c, qv = _num(f[1]), _num(f[2]), _num(f[3]), _num(f[4]), _num(f[7])
        if None in (o, h, l, c, qv):
            errors.append(f"line{ln}:nonfinite")
            continue
        if min(o, h, l, c) <= 0 or qv < 0:
            errors.append(f"line{ln}:nonpositive")
            continue
        if h < max(o, c) or l > min(o, c) or h < l:
            errors.append(f"line{ln}:ohlc")
            continue
        if not (lo <= t < hi) or t % DAY_MS != 0:
            errors.append(f"line{ln}:time_range")
            continue
        if t in seen:
            errors.append(f"line{ln}:duplicate")
            continue
        if last is not None and t < last:
            errors.append(f"line{ln}:order")
            continue
        seen.add(t)
        last = t
        rows.append((t, o, h, l, c, qv))
    return rows, errors, n_raw


def parse_funding_csv(text, month):
    """資金費 CSV（3 欄 calc_time, funding_interval_hours, last_funding_rate；第一個非空紀錄第一欄為 calc_time 視為標頭）
    -> (rows[(calc_time_ms, interval_h, rate)], errors, n_raw)。
    calc_time 是資料站記錄的原始毫秒時戳，**不四捨五入**；落在該月 [月初, 下月初)（以原始 calc_time 歸月，
    未以實際檔案樣本驗證所有月份的歸屬）；下游以原始 calc_time 與 execution_time 比較，採 (進場, 出場]。不重複、檔內嚴格遞增。"""
    rows, errors, seen, n_raw, last = [], [], set(), 0, None
    lo, hi = month_bounds_ms(month)
    for ln, f in _records(text, "calc_time"):
        n_raw += 1
        if len(f) != 3:
            errors.append(f"line{ln}:columns")
            continue
        if not f[0].isdigit():
            errors.append(f"line{ln}:time")
            continue
        t = int(f[0])
        iv, r = _num(f[1]), _num(f[2])
        if iv is None or r is None or iv <= 0:
            errors.append(f"line{ln}:nonfinite")
            continue
        if not (lo <= t < hi):
            errors.append(f"line{ln}:time_range")
            continue
        if t in seen:
            errors.append(f"line{ln}:duplicate")
            continue
        if last is not None and t < last:
            errors.append(f"line{ln}:order")
            continue
        seen.add(t)
        last = t
        rows.append((t, iv, r))
    return rows, errors, n_raw
