#!/usr/bin/env python3
"""
SW3 产业链 Z-Score — 通用版 (基于 sw3_lithium_zscore.py 泛化)
=============================================================
数据源: 新浪 vDOWN API (GBK, 累计制)
方法论: PDF《创业板择时框架研究》V2+V3 — 6指标→z-score→Lead/Lag→Divergence→T1-T4→9-Bucket

用法: python sw3_zscore.py "数字芯片设计"
      python sw3_zscore.py "锂电池"
      python sw3_zscore.py --all  # 串行跑全部 SW3 (供 cron)
"""

import urllib.request
import json, csv, os, sys, time, re
import numpy as np
from pathlib import Path
from collections import Counter

# ============================================================
# 0. 配置
# ============================================================

DESKTOP = Path(r"C:\Users\Administrator\Desktop")
OUTPUT_DIR = DESKTOP / "finance" / "sw3_output"
CACHE_DIR = OUTPUT_DIR / "cache"
L3_CSV = DESKTOP / "股票库" / "L3_滞胀期BK黑名单_v2.0.csv"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MIN_Z_WINDOW = 20       # z-score 最小历史窗口 (季)
MIN_STOCKS = 5          # SW3 最少股票数
MIN_QUARTERS = 20       # SW3 最少有效季度数
MAX_STOCKS_PER_SW3 = 80 # 防止单个 SW3 拉太久

URLS = {
    "BS": "https://vip.stock.finance.sina.com.cn/corp/go.php/vDOWN_BalanceSheet/displaytype/4/stockid/{code}/ctrl/all.phtml",
    "CF": "https://vip.stock.finance.sina.com.cn/corp/go.php/vDOWN_CashFlow/displaytype/4/stockid/{code}/ctrl/all.phtml",
    "PL": "https://vip.stock.finance.sina.com.cn/corp/go.php/vDOWN_ProfitStatement/displaytype/4/stockid/{code}/ctrl/all.phtml",
}


# ============================================================
# 0.1 从 CSV 读取 SW3 成分股
# ============================================================

def get_stocks_for_sw3(sw3_name):
    """读 L3 CSV，返回该 SW3 的所有 (code, name)，按股票数排序"""
    if not L3_CSV.exists():
        print(f"[ERROR] L3 CSV 不存在: {L3_CSV}")
        return []

    stocks = []
    with open(L3_CSV, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sw3 = (row.get("SW3", "") or "").strip()
            if sw3 == sw3_name:
                code = (row.get("code", "") or "").strip()
                name = (row.get("name", "") or row.get("股票名称", "") or "").strip()
                if code and len(code) >= 6:
                    stocks.append((code.zfill(6), name))
    return stocks


def get_all_sw3(min_stocks=MIN_STOCKS):
    """获取所有 SW3 及股票数（满足最低门槛的）"""
    if not L3_CSV.exists():
        return []

    sw3_counts = Counter()
    sw3_names = {}
    with open(L3_CSV, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sw3 = (row.get("SW3", "") or "").strip()
            if not sw3:
                continue
            sw3_counts[sw3] += 1
            if sw3 not in sw3_names:
                sw3_names[sw3] = {
                    "SW2": (row.get("SW2", "") or "").strip(),
                    "SW1": (row.get("SW1", "") or "").strip(),
                }

    return [(name, count, sw3_names.get(name, {}))
            for name, count in sw3_counts.most_common()
            if count >= min_stocks]


# ============================================================
# 1. 数据获取 (同 lithium)
# ============================================================

def fetch_table(code, table_type):
    cache_file = CACHE_DIR / f"{code}_{table_type}.txt"
    if cache_file.exists():
        with open(cache_file, "r", encoding="gbk", errors="replace") as f:
            return f.read()

    url = URLS[table_type].format(code=code)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=45).read().decode("gbk", errors="replace")
        with open(cache_file, "w", encoding="gbk", errors="replace") as f:
            f.write(data)
        time.sleep(1.0)
        return data
    except Exception as e:
        print(f"  [WARN] fetch failed {code} {table_type}: {e}")
        return None


def parse_table(raw_text):
    if not raw_text:
        return None, None
    lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
    if len(lines) < 2:
        return None, None
    header_cells = lines[0].split("\t")
    dates = [c.strip() for c in header_cells if re.match(r"^\d{8}$", c.strip())]
    rows = {}
    for line in lines[1:]:
        cells = line.split("\t")
        if len(cells) < 2:
            continue
        field_name = cells[0].strip()
        if not field_name:
            continue
        values = []
        offset = len(cells) - len(dates)
        if offset < 1:
            continue
        for i in range(len(dates)):
            idx = offset + i
            values.append(cells[idx].strip().replace(",", "") if idx < len(cells) else "")
        rows[field_name] = values
    return dates, rows


def safe_float(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return np.nan


def extract_field(rows, keywords):
    if rows is None:
        return None, None
    for key in rows:
        for kw in keywords:
            if kw == key:
                return key, rows[key]
    for key in rows:
        for kw in keywords:
            if kw in key:
                return key, rows[key]
    return None, None


# ============================================================
# 2. 单只股票处理
# ============================================================

def process_stock(code, name):
    print(f"  [{code} {name}]", end=" ", flush=True)

    bs_raw = fetch_table(code, "BS")
    cf_raw = fetch_table(code, "CF")
    pl_raw = fetch_table(code, "PL")

    bs_dates, bs_rows = parse_table(bs_raw)
    cf_dates, cf_rows = parse_table(cf_raw)
    pl_dates, pl_rows = parse_table(pl_raw)

    if bs_dates is None or cf_dates is None:
        print("[FAIL] parse error")
        return None

    dates = bs_dates
    bs_date_idx = {d: i for i, d in enumerate(bs_dates)}
    cf_date_idx = {d: i for i, d in enumerate(cf_dates)} if cf_dates else {}
    pl_date_idx = {d: i for i, d in enumerate(pl_dates)} if pl_dates else {}

    # 提取 6 指标
    cl_key, cl_vals = extract_field(bs_rows, ["合同负债"])
    if cl_key is None:
        cl_key, cl_vals = extract_field(bs_rows, ["预收款项", "预收账款"])

    cf_goods_key, cf_goods_vals = extract_field(cf_rows, ["购买商品、接受劳务支付的现金"])
    cf_capex_key, cf_capex_vals = extract_field(cf_rows, ["购建固定资产、无形资产和其他长期资产所支付的现金"])
    inv_key, inv_vals = extract_field(bs_rows, ["存货"])
    cip_key, cip_vals = extract_field(bs_rows, ["在建工程(合计)", "在建工程"])
    ta_key, ta_vals = extract_field(bs_rows, ["资产总计"])
    rev_key, rev_vals = extract_field(pl_rows, ["营业收入"])

    stock_data = {"code": code, "name": name, "quarters": []}
    for i, d in enumerate(dates):
        q = {
            "date": d,
            "contract_liab": safe_float(cl_vals[i]) if cl_vals and i < len(cl_vals) else np.nan,
            "cash_paid_goods": np.nan, "capex": np.nan,
            "inventory": safe_float(inv_vals[i]) if inv_vals and i < len(inv_vals) else np.nan,
            "construction": safe_float(cip_vals[i]) if cip_vals and i < len(cip_vals) else np.nan,
            "total_assets": safe_float(ta_vals[i]) if ta_vals and i < len(ta_vals) else np.nan,
            "revenue": np.nan,
        }
        if d in cf_date_idx:
            ci = cf_date_idx[d]
            if cf_goods_vals and ci < len(cf_goods_vals):
                q["cash_paid_goods"] = safe_float(cf_goods_vals[ci])
            if cf_capex_vals and ci < len(cf_capex_vals):
                q["capex"] = safe_float(cf_capex_vals[ci])
        if d in pl_date_idx:
            pi = pl_date_idx[d]
            if rev_vals and pi < len(rev_vals):
                q["revenue"] = safe_float(rev_vals[pi])
        stock_data["quarters"].append(q)

    valid_q = sum(1 for q in stock_data["quarters"]
                  if not (np.isnan(q["inventory"]) and np.isnan(q["total_assets"])))
    print(f"{valid_q}季", flush=True)
    return stock_data


# ============================================================
# 3. 计算函数 (同 lithium)
# ============================================================

def cumulative_to_single_quarter(quarters):
    by_year = {}
    for q in quarters:
        y = int(q["date"][:4])
        mmdd = q["date"][4:]
        by_year.setdefault(y, {})[mmdd] = q

    cumulative_fields = ["revenue", "cash_paid_goods", "capex"]
    for year, year_data in sorted(by_year.items()):
        q1, q2, q3, q4 = year_data.get("0331"), year_data.get("0630"), year_data.get("0930"), year_data.get("1231")
        for field in cumulative_fields:
            if q2 and q1:
                v2, v1 = q2[field], q1[field]
                q2[f"{field}_single"] = v2 - v1 if (not np.isnan(v2) and not np.isnan(v1) and v2 > 0) else np.nan
            if q3 and q2:
                v3, v2c = q3[field], q2[field]
                q3[f"{field}_single"] = v3 - v2c if (not np.isnan(v3) and not np.isnan(v2c) and v3 > 0) else np.nan
            if q4 and q3:
                v4, v3c = q4[field], q3[field]
                q4[f"{field}_single"] = v4 - v3c if (not np.isnan(v4) and not np.isnan(v3c) and v4 > 0) else np.nan
            if q1:
                q1[f"{field}_single"] = q1[field] if not np.isnan(q1[field]) else np.nan


def calc_ttm_revenue(quarters):
    sorted_qs = sorted(quarters, key=lambda q: q["date"])
    for i, q in enumerate(sorted_qs):
        ttm = 0.0
        valid = 0
        for j in range(max(0, i - 3), i + 1):
            rev_s = sorted_qs[j].get("revenue_single", np.nan)
            if not np.isnan(rev_s) and rev_s > 0:
                ttm += rev_s
                valid += 1
        q["revenue_ttm"] = ttm if valid >= 3 else np.nan


def calc_asset_turnover(quarters):
    for q in quarters:
        rev_ttm = q.get("revenue_ttm", np.nan)
        ta = q.get("total_assets", np.nan)
        q["asset_turnover"] = (rev_ttm / ta) if (not np.isnan(rev_ttm) and not np.isnan(ta) and ta > 0) else np.nan


def calc_yoy(quarters):
    yoy_fields = ["contract_liab", "cash_paid_goods_single", "capex_single", "inventory", "construction"]
    date_map = {q["date"]: i for i, q in enumerate(quarters)}

    for i, q in enumerate(quarters):
        d = q["date"]
        year = int(d[:4])
        mmdd = d[4:]
        prev_d = f"{year-1}{mmdd}"
        prev_q = quarters[date_map[prev_d]] if prev_d in date_map else None

        for field in yoy_fields:
            cur_val = q.get(field, np.nan)
            if np.isnan(cur_val) or cur_val == 0 or prev_q is None:
                q[f"{field}_yoy"] = np.nan
                continue
            prev_val = prev_q.get(field, np.nan)
            q[f"{field}_yoy"] = (cur_val - prev_val) / abs(prev_val) if (not np.isnan(prev_val) and prev_val != 0) else np.nan


def winsorize(series, p_low=0.02, p_high=0.98):
    arr = np.array(series, dtype=float)
    mask = ~np.isnan(arr)
    if mask.sum() < 5:
        return arr
    low, high = np.percentile(arr[mask], p_low * 100), np.percentile(arr[mask], p_high * 100)
    arr2 = arr.copy()
    arr2[mask] = np.clip(arr[mask], low, high)
    return arr2


def compute_zscore(values, expanding=True):
    arr = np.array(values, dtype=float)
    z = np.full(len(arr), np.nan)
    for i in range(len(arr)):
        if i < MIN_Z_WINDOW - 1:
            continue
        valid = arr[:i+1][~np.isnan(arr[:i+1])]
        if len(valid) < MIN_Z_WINDOW:
            continue
        mu, sigma = np.mean(valid), np.std(valid, ddof=1)
        if sigma == 0 or np.isnan(sigma) or np.isnan(arr[i]):
            continue
        z[i] = (arr[i] - mu) / sigma
    return z


# ============================================================
# 4. 主入口
# ============================================================

def run(sw3_name):
    """对一个 SW3 执行完整 Z-Score 链路"""
    print("=" * 70)
    print(f"SW3 {sw3_name} — 产业链 Z-Score")
    print("=" * 70)

    # 获取成分股
    all_stocks_raw = get_stocks_for_sw3(sw3_name)
    if len(all_stocks_raw) < MIN_STOCKS:
        print(f"\n[SKIP] SW3 '{sw3_name}' 仅 {len(all_stocks_raw)} 只股票 (<{MIN_STOCKS}), 跳过")
        return {"error": "insufficient_stocks", "count": len(all_stocks_raw), "min_required": MIN_STOCKS}

    # 限制股票数防止超时
    stocks = all_stocks_raw[:MAX_STOCKS_PER_SW3]
    if len(all_stocks_raw) > MAX_STOCKS_PER_SW3:
        print(f"  股票数 {len(all_stocks_raw)} > {MAX_STOCKS_PER_SW3}, 截取前 {MAX_STOCKS_PER_SW3} 只")

    print(f"  成分股: {len(stocks)} 只 (总 {len(all_stocks_raw)}), z-score 窗口: {MIN_Z_WINDOW} 季")

    # Step 1: 拉数据
    print("\n[Step 1/5] 拉取新浪 vDOWN...")
    all_stocks, failed = [], []
    for code, name in stocks:
        result = process_stock(code, name)
        if result:
            all_stocks.append(result)
        else:
            failed.append((code, name))
    print(f"  成功: {len(all_stocks)}/{len(stocks)}, 失败: {len(failed)}")

    if len(all_stocks) < MIN_STOCKS:
        print(f"\n[SKIP] 有效数据仅 {len(all_stocks)} 只 (<{MIN_STOCKS}), 跳过")
        return {"error": "insufficient_valid_data", "count": len(all_stocks), "min_required": MIN_STOCKS}

    # Step 2: 计算
    print("\n[Step 2/5] 累计→单季 + TTM + 资产周转率 + YoY...")
    for s in all_stocks:
        cumulative_to_single_quarter(s["quarters"])
        calc_ttm_revenue(s["quarters"])
        calc_asset_turnover(s["quarters"])
        calc_yoy(s["quarters"])

    for s in all_stocks:
        valid = [q for q in s["quarters"] if not np.isnan(q.get("inventory", np.nan))]
        s["n_valid_quarters"] = len(valid)

    valid_stocks = [s for s in all_stocks if s["n_valid_quarters"] >= 8]
    print(f"  有效股票(≥8季): {len(valid_stocks)}/{len(all_stocks)}")

    # Step 3: SW3 聚合
    print("\n[Step 3/5] SW3层级聚合...")
    all_dates_set = set()
    for s in valid_stocks:
        for q in s["quarters"]:
            all_dates_set.add(q["date"])
    all_dates = sorted(all_dates_set)

    indicators_yoy = ["contract_liab_yoy", "cash_paid_goods_single_yoy",
                       "capex_single_yoy", "inventory_yoy", "construction_yoy"]

    sw3_panel = []
    for d in all_dates:
        row = {"date": d, "n_stocks": 0}
        for ind in indicators_yoy:
            vals = []
            for s in valid_stocks:
                for q in s["quarters"]:
                    if q["date"] == d:
                        v = q.get(ind, np.nan)
                        if not np.isnan(v) and abs(v) < 50:
                            vals.append(v)
                        break
            row[ind] = np.nanmedian(winsorize(vals)) if len(vals) >= 5 else np.nan

        at_vals = []
        for s in valid_stocks:
            for q in s["quarters"]:
                if q["date"] == d:
                    v = q.get("asset_turnover", np.nan)
                    if not np.isnan(v) and 0 < v < 10:
                        at_vals.append(v)
                    break
        row["asset_turnover"] = np.nanmedian(winsorize(at_vals)) if len(at_vals) >= 5 else np.nan
        row["n_stocks"] = len([s for s in valid_stocks if any(q["date"] == d for q in s["quarters"])])
        sw3_panel.append(row)

    # Step 4: z-score + 合成
    print("\n[Step 4/5] z-score + Lead/Lag...")
    z_fields = {
        "contract_liab_z": "contract_liab_yoy", "cash_goods_z": "cash_paid_goods_single_yoy",
        "capex_z": "capex_single_yoy", "inventory_z": "inventory_yoy",
        "construction_z": "construction_yoy", "asset_turnover_raw_z": "asset_turnover",
    }
    for z_name, src_name in z_fields.items():
        series = [row.get(src_name, np.nan) for row in sw3_panel]
        z_series = compute_zscore(series, expanding=True)
        for i, row in enumerate(sw3_panel):
            row[z_name] = z_series[i] if not np.isnan(z_series[i]) else None
    # 资产周转率反向
    for row in sw3_panel:
        v = row.get("asset_turnover_raw_z")
        row["asset_turnover_z"] = -v if v is not None else None
    # 合成
    for row in sw3_panel:
        lead_vals = [row.get(k) for k in ["contract_liab_z", "cash_goods_z", "capex_z"]]
        lag_vals = [row.get(k) for k in ["inventory_z", "construction_z", "asset_turnover_z"]]
        lead_v = [v for v in lead_vals if v is not None]
        lag_v = [v for v in lag_vals if v is not None]
        row["Lead_z"] = np.mean(lead_v) if len(lead_v) >= 2 else None
        row["Lag_z"] = np.mean(lag_v) if len(lag_v) >= 2 else None
        if row["Lead_z"] is not None and row["Lag_z"] is not None:
            row["Divergence"] = row["Lead_z"] - row["Lag_z"]
            row["Overall_z"] = (row["Lead_z"] + row["Lag_z"]) / 2
        else:
            row["Divergence"] = row["Overall_z"] = None
    for i, row in enumerate(sw3_panel):
        if i >= 2:
            cur, prev2 = row.get("Divergence"), sw3_panel[i-2].get("Divergence")
            row["delta_div_2q"] = cur - prev2 if (cur is not None and prev2 is not None) else None
        else:
            row["delta_div_2q"] = None

    # Step 5: 信号判定 + 输出
    print("\n[Step 5/5] 信号 + 输出...")

    def check_signals(row, prev_row):
        signals = []
        lead, lag, div, delta = row.get("Lead_z"), row.get("Lag_z"), row.get("Divergence"), row.get("delta_div_2q")
        if lead is None or lag is None:
            return signals
        if lead < -1.0 and lag < -0.5:
            signals.append("T1_高置信底")
        if prev_row:
            prev_div = prev_row.get("Divergence")
            if prev_div is not None and div is not None:
                if prev_div < 0 and div > 0 and lag < -0.2:
                    signals.append("T2_拐点底")
                if prev_div > 0 and div < 0 and lag > 0.2:
                    signals.append("T3_拐点顶")
        if delta is not None and delta < -0.8 and lag and lag > 0:
            signals.append("T4_高置信顶")
        return signals

    def bucket_position(lead_z, lag_z):
        if lead_z is None or lag_z is None:
            return "N/A"
        lt = "Low" if lead_z < -0.5 else ("High" if lead_z > 0.5 else "Mid")
        lg = "Low" if lag_z < -0.5 else ("High" if lag_z > 0.5 else "Mid")
        return f"Lead{lt} x Lag{lg}"

    all_signals = []
    for i, row in enumerate(sw3_panel):
        prev = sw3_panel[i-1] if i > 0 else None
        sigs = check_signals(row, prev)
        row["signals"] = sigs
        if sigs:
            all_signals.append({"date": row["date"], "signals": sigs,
                "Lead_z": row.get("Lead_z"), "Lag_z": row.get("Lag_z"),
                "Divergence": row.get("Divergence")})

    valid_panel = [r for r in sw3_panel if r.get("Lead_z") is not None]
    if len(valid_panel) < MIN_QUARTERS:
        print(f"\n[SKIP] 有效面板仅 {len(valid_panel)} 季 (<{MIN_QUARTERS}), 不输出信号")
        return {"error": "insufficient_history", "quarters": len(valid_panel), "min_required": MIN_QUARTERS}

    latest = valid_panel[-1]
    today = time.strftime("%Y%m%d")
    safe_name = sw3_name.replace("/", "_").replace("\\", "_")

    output = {
        "_meta": {"sw3_name": sw3_name, "date": today,
            "n_stocks_total": len(all_stocks_raw), "n_stocks_processed": len(all_stocks),
            "n_stocks_valid": len(valid_stocks), "failed_codes": [c for c, _ in failed],
            "min_z_window": MIN_Z_WINDOW, "n_quarters_panel": len(valid_panel),
            "date_range": f"{valid_panel[0]['date']} -> {valid_panel[-1]['date']}" if valid_panel else "N/A"},
        "current_status": {
            "date": latest["date"], "n_stocks_reporting": latest.get("n_stocks"),
            "indicators": {k: latest.get(k) for k in ["contract_liab_z","cash_goods_z","capex_z","inventory_z","construction_z","asset_turnover_z"]},
            "synthesis": {k: latest.get(k) for k in ["Lead_z","Lag_z","Overall_z","Divergence","delta_div_2q"]},
            "bucket": bucket_position(latest.get("Lead_z"), latest.get("Lag_z")),
            "signals": latest.get("signals", []),
        },
        "signal_history": all_signals,
        "panel_last_8q": [{"date": r["date"], "Lead_z": r.get("Lead_z"), "Lag_z": r.get("Lag_z"),
            "Divergence": r.get("Divergence"), "Overall_z": r.get("Overall_z"),
            "delta_div_2q": r.get("delta_div_2q"), "signals": r.get("signals", []),
            "bucket": bucket_position(r.get("Lead_z"), r.get("Lag_z"))} for r in valid_panel[-8:]],
    }

    # 输出 JSON
    json_path = OUTPUT_DIR / f"SW3_{safe_name}_ZScore_{today}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    # 输出 CSV
    csv_path = OUTPUT_DIR / f"SW3_{safe_name}_panel_{today}.csv"
    csv_fields = ["date","n_stocks","contract_liab_z","cash_goods_z","capex_z",
                   "inventory_z","construction_z","asset_turnover_z",
                   "Lead_z","Lag_z","Divergence","Overall_z","delta_div_2q"]
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(",".join(csv_fields) + "\n")
        for row in valid_panel:
            f.write(",".join(str(row.get(k, "")) if row.get(k) is not None else "" for k in csv_fields) + "\n")

    # 终端输出
    print(f"\n[DONE] {json_path}")
    if latest:
        syn = output["current_status"]["synthesis"]
        sigs = output["current_status"]["signals"]
        print(f"  Lead={syn['Lead_z']:.2f}  Lag={syn['Lag_z']:.2f}  Div={syn['Divergence']:.2f}  "
              f"Bucket={output['current_status']['bucket']}  Signals={sigs or '无'}")

    return output


# ============================================================
# 5. CLI
# ============================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python sw3_zscore.py <SW3名称>")
        print("      python sw3_zscore.py --list  # 列出所有 SW3")
        print("      python sw3_zscore.py --all   # 串行跑全部 (供 cron)")
        sys.exit(1)

    arg = sys.argv[1]

    if arg == "--list":
        all_sw3 = get_all_sw3()
        print(f"共 {len(all_sw3)} 个 SW3 (≥{MIN_STOCKS}只):")
        for name, count, meta in all_sw3:
            print(f"  {count:4d}只  {meta.get('SW1','?'):6s} / {meta.get('SW2','?'):10s} / {name}")
        sys.exit(0)

    if arg == "--all":
        all_sw3 = get_all_sw3()
        print(f"串行跑 {len(all_sw3)} 个 SW3...")
        ok, skip, fail = 0, 0, 0
        for i, (name, count, meta) in enumerate(all_sw3):
            print(f"\n[{i+1}/{len(all_sw3)}] {name} ({count}只)")
            result = run(name)
            if result is None:
                fail += 1
            elif "error" in result:
                skip += 1
                print(f"  -> 跳过: {result['error']}")
            else:
                ok += 1
        print(f"\n===== DONE: OK={ok} SKIP={skip} FAIL={fail} =====")
        sys.exit(0)

    run(arg)
