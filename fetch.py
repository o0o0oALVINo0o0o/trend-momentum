"""trend-momentum 資料抓取（只讀公開資料，不需金鑰）。規格見 SPEC.md（v3，凍結於 FREEZE.txt）。

子命令（同一時間只能執行一個，用 data/.fetch.lock 互斥）：
  python fetch.py list                 列出資料站目錄，原子發布 data/plan.jsonl、population.csv、listing_log.jsonl、
                                       coverage.jsonl、list_done.json（批次完成標記，含批次編號、筆數與雜湊）；不下載資料檔
  python fetch.py download [--workers 8] [--limit N]
                                       依完整的 plan 下載 zip 與 .CHECKSUM；已存在者核對大小、manifest 的 SHA-256、
                                       以及 zip 與 CHECKSUM 是否相符，不符者成對重抓；有失敗時以非零碼結束
  python fetch.py verify               逐檔驗證（存在、大小、manifest SHA、CHECKSUM、內容），寫 data/content.jsonl 與
                                       verify_failures.jsonl；有任何失敗以非零碼結束

重要：
- verify 通過只代表「傳輸與內容驗證通過」，不代表研究資料完整；覆蓋缺口（缺月、提早結束、孤立 CHECKSUM）列在
  coverage.jsonl，依 SPEC §1 由分析程式把受影響的交易判為不可評估。
- 資金費 REST 備援：SPEC §1 寫「fundingRate 不存在則改用官方 REST」。本程式的解讀是「整種資料檔對某標的完全不存在」
  才觸發（目前 0 個標的屬此情況）；月份缺口依 SPEC 缺漏規則使該區間內的交易不可評估，不由 REST 補。此解讀未獲 SPEC 逐字確認，
  若要改為補缺口須另寫結果前修訂。
- 資金費 calc_time 保留資料站原始毫秒，不四捨五入；下游以原始 calc_time 與 execution_time 比較，區間 (進場, 出場]。
只用標準函式庫。
"""
import argparse
import atexit
import csv
import email.utils
import hashlib
import http.client
import io
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # python -I 不會自動加入腳本目錄
from common import (FUND_ROOT, KLINE_ROOT, coverage, in_range, keep_symbol, parse_funding_csv, parse_key,
                    parse_kline_csv, symbol_ok_format)

BUCKET = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
MAX_WORKERS = 16
MAX_RETRY_AFTER = 600.0            # Retry-After 超過此秒數就停止並回報，不截短提前重試

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
RAW = os.path.join(DATA, "raw")
PLAN = os.path.join(DATA, "plan.jsonl")
POP = os.path.join(DATA, "population.csv")
LISTLOG = os.path.join(DATA, "listing_log.jsonl")
COVER = os.path.join(DATA, "coverage.jsonl")
DONE = os.path.join(DATA, "list_done.json")
MANIFEST = os.path.join(DATA, "manifest.jsonl")
INCIDENTS = os.path.join(DATA, "manifest_incidents.jsonl")
CONTENT = os.path.join(DATA, "content.jsonl")
FAILS = os.path.join(DATA, "verify_failures.jsonl")
LOCK = os.path.join(DATA, ".fetch.lock")
NOTES = os.path.join(HERE, "population_notes.csv")        # 選用：symbol,note（更名／遷移的文件證據；不拼接、不刪除）


# ---------- 純函式 ----------
def parse_listing(xml_bytes):
    """解析 S3 ListObjects 回應 -> (entries[(key, size)], prefixes[str], truncated, next_marker)。
    缺少 IsTruncated 或值不是 true/false 視為格式錯誤（不當作最後一頁）。"""
    root = ET.fromstring(xml_bytes)
    entries = [(c.find(NS + "Key").text, int(c.find(NS + "Size").text)) for c in root.iter(NS + "Contents")]
    prefixes = [p.find(NS + "Prefix").text for p in root.iter(NS + "CommonPrefixes")]
    t = root.find(NS + "IsTruncated")
    if t is None or t.text not in ("true", "false"):
        raise ValueError("回應缺少合法的 IsTruncated")
    nm = root.find(NS + "NextMarker")
    return entries, prefixes, t.text == "true", (nm.text if nm is not None else None)


def parse_checksum(text, filename):
    """CHECKSUM 檔格式：'<64 位十六進位>  <檔名>'。檔名不符或格式不對回傳 None。"""
    parts = text.strip().split()
    if len(parts) != 2 or len(parts[0]) != 64:
        return None
    try:
        int(parts[0], 16)
    except ValueError:
        return None
    return parts[0].lower() if parts[1].lstrip("*") == filename else None


def retryable(code):
    return code == 429 or code >= 500


def parse_retry_after(value, now=None):
    """Retry-After：秒數或 HTTP-date -> 秒（float）；無法解析回傳 None。"""
    if value is None:
        return None
    v = str(value).strip()
    try:
        return max(0.0, float(v))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(v)
    except (TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    return max(0.0, when.timestamp() - now)


def backoff_seconds(attempt, retry_after=None):
    base = min(60.0, 2.0 ** attempt)
    if retry_after is not None:
        base = max(base, retry_after)
    return base + random.uniform(0, 1.0)


def check_unique(keys):
    seen, dup = set(), []
    for k in keys:
        if k in seen:
            dup.append(k)
        seen.add(k)
    return dup


def load_manifest_lines(lines):
    """-> (latest{key: record}, bad_lines_count, partial_tail)。只採以換行結尾的有效 JSON 行；
    最後一行若沒有換行視為殘行（partial_tail=True），不採用。後出現的有效紀錄覆蓋先前的。"""
    latest, bad, partial = {}, 0, False
    for line in lines:
        if not line.endswith("\n"):
            partial = True
            continue
        try:
            r = json.loads(line)
            latest[r["key"]] = r
        except (ValueError, KeyError, TypeError):
            bad += 1
    return latest, bad, partial


def split_partial(lines):
    """-> (complete_lines, partial_text|None)：把沒有換行結尾的尾端殘行分離出來。"""
    if lines and not lines[-1].endswith("\n"):
        return lines[:-1], lines[-1]
    return list(lines), None


def item_bad(item, rec, local_size, local_sha):
    """單一檔案是否需要（重新）下載：本機缺、大小不符、manifest 沒有成功紀錄、或 manifest 的 SHA-256 與本機不符。"""
    if local_size is None or local_size != item["size"]:
        return True
    if rec is None or rec.get("status") != "downloaded":
        return True
    if rec.get("sha256") is not None and rec.get("sha256") != local_sha:
        return True
    return False


def pair_decision(z_bad, c_bad, z_sha, c_text, z_name):
    """zip 與其 CHECKSUM 的成對修復決策 -> (redo_zip, redo_checksum)。
    任一方本身有問題就重抓該方；兩者都存在且各自正常時，CHECKSUM 無法解析或與 zip 的 SHA-256 不符 -> 兩者都重抓。"""
    redo_z, redo_c = z_bad, c_bad
    if not z_bad and not c_bad:
        want = parse_checksum(c_text or "", z_name)
        if want is None or want != z_sha:
            redo_z = redo_c = True
    return redo_z, redo_c


# ---------- 網路與檔案 ----------
def http_get(url, tries=5, timeout=60):
    last = None
    for i in range(tries):
        retry_after = None
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if not retryable(e.code):
                raise                                      # 404 等：不重試，由呼叫端記錄
            last = e
            retry_after = parse_retry_after(e.headers.get("Retry-After") if e.headers else None)
            if retry_after is not None and retry_after > MAX_RETRY_AFTER:
                raise RuntimeError(f"Retry-After {retry_after:.0f} 秒超過上限 {MAX_RETRY_AFTER:.0f}，停止")
        except (OSError, http.client.HTTPException) as e:  # 網路暫時性錯誤（含逾時、連線中斷）
            last = e
        time.sleep(backoff_seconds(i, retry_after))
    raise last


def list_prefix(prefix, delimiter=None):
    """列出前綴下全部項目；回傳 (entries, prefixes, pages_log)。分頁沒走完、標記未前進、格式錯誤都丟例外。"""
    entries, prefixes, pages, marker = [], [], [], ""
    while True:
        q = {"prefix": prefix, "max-keys": "1000"}
        if delimiter:
            q["delimiter"] = delimiter
        if marker:
            q["marker"] = marker
        e, p, trunc, nm = parse_listing(http_get(BUCKET + "?" + urllib.parse.urlencode(q)))
        entries += e
        prefixes += p
        last_item = e[-1][0] if e else (p[-1] if p else None)
        pages.append({"marker_in": marker, "marker_out": nm, "first_key": e[0][0] if e else None, "last_key": last_item,
                      "n_keys": len(e), "n_prefixes": len(p), "truncated": trunc})
        if not trunc:
            return entries, prefixes, pages
        new = nm or last_item or ""
        if not new or new <= marker:
            raise RuntimeError("分頁標記遺失或未前進：" + prefix)
        marker = new


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def publish(path, text):
    """原子寫入：先寫 .tmp 再 os.replace。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def acquire_lock():
    """同一時間只允許一個 list／download／verify。殘留的鎖檔需人工確認沒有程序在跑後刪除。"""
    os.makedirs(DATA, exist_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        sys.exit(f"另一個 fetch 指令可能正在執行（{LOCK} 存在）。確認沒有時請手動刪除該檔。")
    os.write(fd, f"{os.getpid()}\n".encode())
    os.close(fd)
    atexit.register(lambda: os.path.exists(LOCK) and os.remove(LOCK))


# ---------- list ----------
def cmd_list(args):
    acquire_lock()
    batch = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    _, prefixes, root_pages = list_prefix(KLINE_ROOT, "/")
    all_syms = sorted(p[len(KLINE_ROOT):].strip("/") for p in prefixes)
    bad_format = [s for s in all_syms if s.endswith("USDT") and "_" not in s and not symbol_ok_format(s)]
    syms = [s for s in all_syms if keep_symbol(s)]
    print(f"目錄標的 {len(all_syms)}，符合符號規則 {len(syms)}，USDT 結尾但格式不被支援 {len(bad_format)}", flush=True)

    def one(sym):
        out = {}
        for kind, root, sub in (("kline", KLINE_ROOT, "1d/"), ("fund", FUND_ROOT, "")):
            pref = f"{root}{sym}/{sub}"
            entries, _prefixes, pages = list_prefix(pref)
            out[kind] = (pref, entries, pages)
        return sym, out

    results = {}
    with ThreadPoolExecutor(max_workers=min(args.workers, MAX_WORKERS)) as ex:
        for f in as_completed([ex.submit(one, s) for s in syms]):
            sym, out = f.result()
            results[sym] = out
    plan, log, cover, anomalies = [], [{"batch": batch, "kind": "root", "prefix": KLINE_ROOT, "pages": root_pages}], [], []
    for sym in syms:
        zips, css = {"kline": [], "fund": []}, {"kline": [], "fund": []}
        for kind in ("kline", "fund"):
            pref, entries, pages = results[sym][kind]
            log.append({"batch": batch, "kind": kind, "symbol": sym, "prefix": pref, "pages": pages, "entries": len(entries)})
            for key, size in entries:
                info = parse_key(key)
                if info is None or info["symbol"] != sym or info["kind"] != kind:
                    anomalies.append({"symbol": sym, "key": key, "why": "unexpected_key"})
                    continue
                if not in_range(info["month"]):
                    continue
                (css if info["checksum"] else zips)[kind].append(info["month"])
                plan.append({"symbol": sym, "kind": kind, "key": key, "size": size, "month": info["month"]})
        flags = coverage(zips["kline"], css["kline"], zips["fund"], css["fund"])
        cover.append({"symbol": sym, "kline_files": len(zips["kline"]), "fund_files": len(zips["fund"]), "flags": flags,
                      "kline_first": min(zips["kline"], default=""), "kline_last": max(zips["kline"], default="")})
    dups = check_unique(p["key"] for p in plan)
    if dups:
        sys.exit(f"plan 內有重複 key（{len(dups)} 個，例：{dups[0]}），停止")
    notes = {}
    if os.path.exists(NOTES):
        with open(NOTES, encoding="utf-8", newline="") as f:
            notes = {r["symbol"]: r.get("note", "") for r in csv.DictReader(f)}
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["symbol", "product", "first_month_in_range", "last_month_in_range", "kline_files", "funding_files", "note"])
    for c in cover:
        w.writerow([c["symbol"], "USDT-M perpetual (archive um)", c["kline_first"], c["kline_last"],
                    c["kline_files"], c["fund_files"], notes.get(c["symbol"], "")])
    pop_text = buf.getvalue()
    plan_text = "".join(json.dumps(p, ensure_ascii=False, sort_keys=True) + "\n" for p in sorted(plan, key=lambda x: x["key"]))
    log_text = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in log)
    cov_text = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in cover)
    if os.path.exists(DONE):
        os.remove(DONE)                                       # 發布期間先撤掉完成標記，避免新舊混用
    for path, text in ((PLAN, plan_text), (POP, pop_text), (LISTLOG, log_text), (COVER, cov_text)):
        publish(path, text)
    done = {"batch": batch, "n_plan": len(plan), "n_symbols": len(syms), "n_unexpected_keys": len(anomalies),
            "n_unsupported_symbols": len(bad_format),
            "sha256": {os.path.basename(p): sha_file(p) for p in (PLAN, POP, LISTLOG, COVER)}}
    publish(DONE, json.dumps(done, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    total = sum(p["size"] for p in plan)
    nz = sum(1 for p in plan if p["key"].endswith(".zip"))
    flagged = {}
    for c in cover:
        for fl in c["flags"]:
            parts = fl.split(":")
            k = ":".join(parts[:2])
            flagged[k] = flagged.get(k, 0) + 1
    print(f"批次 {batch}：計畫檔案 {len(plan)}（zip {nz}、CHECKSUM {len(plan) - nz}），合計 {total / 1e6:.1f} MB；"
          f"預期外的 key {len(anomalies)} 個", flush=True)
    print("覆蓋旗標統計：", json.dumps(flagged, ensure_ascii=False, sort_keys=True), flush=True)
    print(f"population.csv SHA-256：{done['sha256']['population.csv']}", flush=True)


# ---------- download ----------
def read_plan():
    if not (os.path.exists(PLAN) and os.path.exists(DONE)):
        sys.exit("找不到完整的 plan（缺 plan.jsonl 或 list_done.json），請先執行 list")
    done = json.load(open(DONE, encoding="utf-8"))
    for p in (PLAN, POP, LISTLOG, COVER):
        if sha_file(p) != done["sha256"].get(os.path.basename(p)):
            sys.exit(f"{os.path.basename(p)} 與 list_done.json 的雜湊不符（可能中斷或混用批次），請重新執行 list")
    plan = [json.loads(l) for l in open(PLAN, encoding="utf-8") if l.strip()]
    if len(plan) != done["n_plan"]:
        sys.exit("plan 筆數與 list_done.json 不符")
    dups = check_unique(p["key"] for p in plan)
    if dups:
        sys.exit(f"plan 內有重複 key：{dups[0]}")
    return plan, done["batch"]


def local_path(key):
    return os.path.join(RAW, *key.split("/"))


def read_manifest_lines():
    if not os.path.exists(MANIFEST):
        return []
    with open(MANIFEST, encoding="utf-8", newline="") as f:
        return f.readlines()


def fetch_one(item, batch):
    key = item["key"]
    path = local_path(key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    base = {"key": key, "batch": batch, "expected": item["size"]}
    try:
        body = http_get(BUCKET + "/" + urllib.parse.quote(key))
    except urllib.error.HTTPError as e:
        return dict(base, status="http_" + str(e.code), size=0)
    except Exception as e:
        return dict(base, status="error:" + type(e).__name__, size=0)
    if len(body) != item["size"]:
        return dict(base, status="size_mismatch", size=len(body))
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, path)
    return dict(base, status="downloaded", size=len(body), sha256=hashlib.sha256(body).hexdigest())


def plan_todo(plan, latest):
    """-> todo[plan item]。逐檔核對大小與 manifest SHA；zip／CHECKSUM 成對修復。"""
    by_key = {p["key"]: p for p in plan}
    state = {}
    for p in plan:
        path = local_path(p["key"])
        if os.path.exists(path):
            state[p["key"]] = (os.path.getsize(path), sha_file(path))
        else:
            state[p["key"]] = (None, None)
    redo = set()
    for p in plan:
        if p["key"].endswith(".CHECKSUM"):
            continue
        ck = p["key"] + ".CHECKSUM"
        z_bad = item_bad(p, latest.get(p["key"]), *state[p["key"]])
        if ck not in by_key:
            if z_bad:
                redo.add(p["key"])
            continue
        c_bad = item_bad(by_key[ck], latest.get(ck), *state[ck])
        c_text = None
        if not z_bad and not c_bad:
            c_text = open(local_path(ck), encoding="utf-8", errors="replace").read()
        rz, rc = pair_decision(z_bad, c_bad, state[p["key"]][1], c_text, os.path.basename(local_path(p["key"])))
        if rz:
            redo.add(p["key"])
        if rc:
            redo.add(ck)
    zip_keys = {p["key"] for p in plan if p["key"].endswith(".zip")}
    for p in plan:                                              # 孤立 CHECKSUM（plan 內沒有對應 zip）
        if p["key"].endswith(".CHECKSUM") and p["key"][:-len(".CHECKSUM")] not in zip_keys:
            if item_bad(p, latest.get(p["key"]), *state[p["key"]]):
                redo.add(p["key"])
    return [by_key[k] for k in sorted(redo)]


def cmd_download(args):
    acquire_lock()
    plan, batch = read_plan()
    lines = read_manifest_lines()
    complete, partial_text = split_partial(lines)
    if partial_text is not None:                                # 尾端殘行：留證據後原子重建 manifest（只留完整行）
        with open(INCIDENTS, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"ts": int(time.time()), "kind": "manifest_partial_tail", "text": partial_text[:500]},
                               ensure_ascii=False) + "\n")
        publish(MANIFEST, "".join(complete))
        print("manifest 尾端殘行已移除並記錄在 manifest_incidents.jsonl", flush=True)
    latest, bad, _ = load_manifest_lines(complete)
    if bad:
        print(f"警告：manifest 有 {bad} 行無法解析（verify 會因此失敗；後出現的有效紀錄為準）", flush=True)
    todo = plan_todo(plan, latest)
    if args.limit:
        todo = todo[: args.limit]
    print(f"計畫 {len(plan)} 個，待下載／重抓 {len(todo)} 個", flush=True)
    os.makedirs(RAW, exist_ok=True)
    n = failed = 0
    with open(MANIFEST, "a", encoding="utf-8", newline="\n") as mf:
        with ThreadPoolExecutor(max_workers=min(args.workers, MAX_WORKERS)) as ex:
            for fut in as_completed([ex.submit(fetch_one, p, batch) for p in todo]):
                r = fut.result()
                r["ts"] = int(time.time())
                mf.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
                mf.flush()
                n += 1
                if r["status"] != "downloaded":
                    failed += 1
                if n % 2000 == 0:
                    print(f"  已處理 {n}/{len(todo)}，失敗 {failed}", flush=True)
    print(f"完成：處理 {n}，失敗 {failed}。接著執行 verify（下載完成不等於驗證通過）。", flush=True)
    sys.exit(1 if failed else 0)


# ---------- verify ----------
def verify_zip(item, latest, batch, has_plan_checksum):
    """-> (reasons[str], record)。reasons 為空代表傳輸與內容驗證通過。record 一律含 key、batch、status、checksum_ok。"""
    key = item["key"]
    zpath, cpath, ckey = local_path(key), local_path(key) + ".CHECKSUM", key + ".CHECKSUM"
    rec = {"key": key, "batch": batch, "status": "fail", "checksum_ok": None, "rows_valid": None, "rows_raw": None}
    reasons = []
    if not os.path.exists(zpath):
        return ["missing"], rec
    body = open(zpath, "rb").read()
    got = hashlib.sha256(body).hexdigest()
    if len(body) != item["size"]:
        return ["size"], rec
    zrec = latest.get(key)
    if zrec is None or zrec.get("status") != "downloaded":
        reasons.append("manifest_not_downloaded")
    elif zrec.get("sha256") != got:
        reasons.append("manifest_sha_mismatch")
    if not has_plan_checksum:
        reasons.append("no_plan_checksum")
    elif not os.path.exists(cpath):
        reasons.append("no_checksum_file")
    else:
        ctext = open(cpath, encoding="utf-8", errors="replace").read()
        crec = latest.get(ckey)
        if os.path.getsize(cpath) != item.get("checksum_size"):
            reasons.append("checksum_file_size")
        if crec is None or crec.get("status") != "downloaded":
            reasons.append("manifest_checksum_not_downloaded")
        elif crec.get("sha256") != sha_file(cpath):
            reasons.append("manifest_checksum_sha_mismatch")
        want = parse_checksum(ctext, os.path.basename(zpath))
        rec["checksum_ok"] = want is not None and want == got
        if not rec["checksum_ok"]:
            reasons.append("checksum")
    if reasons:
        return reasons, rec
    info = parse_key(key)
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            names = z.namelist()
            if len(names) != 1:
                return ["zip_entries"], rec
            text = z.read(names[0]).decode("utf-8")
    except Exception as e:
        return ["zip_unreadable:" + type(e).__name__], rec
    rows, errs, n_raw = (parse_kline_csv if info["kind"] == "kline" else parse_funding_csv)(text, info["month"])
    rec.update({"rows_valid": len(rows), "rows_raw": n_raw, "n_errors": len(errs), "errors": errs[:20],
                "first_ts": rows[0][0] if rows else None, "last_ts": rows[-1][0] if rows else None})
    if errs:
        reasons.append("content_errors")
    if not rows:
        reasons.append("no_rows")
    if not reasons:
        rec["status"] = "ok"
    return reasons, rec


def cmd_verify(args):
    acquire_lock()
    plan, batch = read_plan()
    latest, bad, partial = load_manifest_lines(read_manifest_lines())
    sizes = {p["key"]: p["size"] for p in plan}
    zip_keys = {p["key"] for p in plan if p["key"].endswith(".zip")}
    stat, fails, content = {}, [], []

    def add_fail(key, reasons, rec=None):
        for r in reasons:
            stat[r] = stat.get(r, 0) + 1
        fails.append({"key": key, "reasons": reasons})
        if rec is not None:
            rec["reasons"] = reasons
            content.append(rec)

    for p in plan:
        if p["key"].endswith(".CHECKSUM") and p["key"][:-len(".CHECKSUM")] not in zip_keys:
            add_fail(p["key"], ["orphan_checksum_in_plan"])
    for p in plan:
        if not p["key"].endswith(".zip"):
            continue
        ck = p["key"] + ".CHECKSUM"
        item = dict(p, checksum_size=sizes.get(ck))
        reasons, rec = verify_zip(item, latest, batch, ck in sizes)
        if reasons:
            add_fail(p["key"], reasons, rec)
        else:
            stat["ok"] = stat.get("ok", 0) + 1
            rec["reasons"] = []
            content.append(rec)
    if partial:
        stat["manifest_partial_tail"] = 1
    publish(CONTENT, "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in content))
    publish(FAILS, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in fails))
    cov = [json.loads(l) for l in open(COVER, encoding="utf-8") if l.strip()]
    flags = {}
    for c in cov:
        for fl in c["flags"]:
            k = ":".join(fl.split(":")[:2])
            flags[k] = flags.get(k, 0) + 1
    print(json.dumps({"batch": batch, "zip_status": stat, "manifest_bad_lines": bad, "manifest_partial_tail": partial,
                      "coverage_flags": flags}, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"失敗清單：{FAILS}（{len(fails)} 筆）。注意：通過只代表傳輸與內容驗證通過，不代表研究資料完整；覆蓋旗標見 coverage.jsonl。")
    sys.exit(1 if (fails or bad or partial) else 0)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("list")
    a.add_argument("--workers", type=int, default=8)
    a.set_defaults(fn=cmd_list)
    b = sub.add_parser("download")
    b.add_argument("--workers", type=int, default=8)
    b.add_argument("--limit", type=int, default=0)
    b.set_defaults(fn=cmd_download)
    c = sub.add_parser("verify")
    c.set_defaults(fn=cmd_verify)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
