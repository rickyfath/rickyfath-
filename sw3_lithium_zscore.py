"""
SW3 锂电池 — 产业链 Z-Score Phase 1 端到端脚本
=================================================
数据源: 新浪 vDOWN API (GBK, 累计制)
方法论: L3 产业周期择时框架 / 6指标 Lead-Lag 分解
日期: 2026-07-10
"""

import urllib.request
import json
import os
import time
import re
import numpy as np
from pathlib import Path

# ============================================================
# 0. 配置
# ============================================================

SW3_NAME = "锂电池"
OUTPUT_DIR = Path(r"C:\Users\Administrator\Desktop\finance\sw3_output")
CACHE_DIR = OUTPUT_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 锂电池 SW3 全部 32 只成分股 (code, name)
STOCKS = [
    ("920237", "力佳科技"), ("920239", "长虹能源"), ("920252", "天宏锂电"),
    ("920523", "德瑞锂电"), ("920914", "远航精密"), ("600110", "诺德股份"),
    ("600152", "维科技术"), ("600241", "时代万恒"), ("600478", "科力远"),
    ("688063", "派能科技"), ("688345", "博力威"), ("688388", "嘉元科技"),
    ("688567", "孚能科技"), ("688772", "珠海冠宇"), ("000049", "德赛电池"),
    ("001283", "豪鹏科技"), ("002074", "国轩高科"), ("002245", "蔚蓝锂芯"),
    ("002850", "科达利"), ("300014", "亿纬锂能"), ("300207", "欣旺达"),
    ("300438", "鹏辉能源"), ("300530", "领湃科技"), ("300750", "宁德时代"),
    ("300953", "震裕科技"), ("301121", "紫建电子"), ("301150", "中一科技"),
    ("301210", "金杨精密"), ("301217", "铜冠铜箔"), ("301327", "华宝新能"),
    ("301511", "德福科技"), ("301587", "中瑞股份"),
]

# 新浪 vDOWN 端点
URLS = {
    "BS": "https://vip.stock.finance.sina.com.cn/corp/go.php/vDOWN_BalanceSheet/displaytype/4/stockid/{code}/ctrl/all.phtml",
    "CF": "https://vip.stock.finance.sina.com.cn/corp/go.php/vDOWN_CashFlow/displaytype/4/stockid/{code}/ctrl/all.phtml",
    "PL": "https://vip.stock.finance.sina.com.cn/corp/go.php/vDOWN_ProfitStatement/displaytype/4/stockid/{code}/ctrl/all.phtml",
}

# z-score 最小历史窗口
MIN_Z_WINDOW = 20  # 季度


# ============================================================
# 1. 数据获取（带缓存）
# ============================================================

def fetch_table(code, table_type):
    """拉取一张新浪 vDOWN 表，返回原始文本。带磁盘缓存。"""
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
        time.sleep(1.0)  # 礼貌延迟
        return data
    except Exception as e:
        print(f"  [WARN] fetch failed {code} {table_type}: {e}")
        return None


def parse_table(raw_text):
    """
    解析新浪 vDOWN 表格。
    返回 (dates, rows):
      dates = [YYYYMMDD, ...]  列名（报告期）
      rows  = {field_name: [value_str, ...]}  每个字段对应各列的值
    新浪格式: Tab分隔, 第1行列头(含报告期), 之后每行是一个会计科目。
    累计制: Q1 < H1 < 9M < FY。
    """
    if not raw_text:
        return None, None

    lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
    if len(lines) < 2:
        return None, None

    # 第一行是表头: "科目\时间\t20260331\t20251231\t..."
    # 有时第一列是乱码(报表日期), 跳过
    header_cells = lines[0].split("\t")
    dates = []
    for c in header_cells:
        c = c.strip()
        if re.match(r"^\d{8}$", c):
            dates.append(c)

    # 解析数据行
    rows = {}
    for line in lines[1:]:
        cells = line.split("\t")
        if len(cells) < 2:
            continue
        field_name = cells[0].strip()
        # 跳过空字段名
        if not field_name:
            continue
        # 对齐 dates 偏移
        # 第一个 cell 是字段名，后面每个 cell 对应每个 date
        values = []
        # 找到 cells 中哪些位置对应 dates
        # dates 的数量决定了我们需要多少个值
        offset = len(cells) - len(dates)
        if offset < 1:
            continue
        for i, d in enumerate(dates):
            idx = offset + i
            if idx < len(cells):
                val = cells[idx].strip().replace(",", "")
            else:
                val = ""
            values.append(val)
        rows[field_name] = values

    return dates, rows


# 累计制 → 单季制
def cumulative_to_single(values):
    """
    输入: 按时间排序的累计值列表 (最新在前还是最旧在前取决于排列)
    新浪 vDOWN: 列从左到右 = 最新 → 最旧
    累计制特点: Q4(年报) > Q3 > Q2 > Q1

    转换: Q1_single = Q1_cum
           Q2_single = Q2_cum - Q1_cum
           Q3_single = Q3_cum - Q2_cum
           Q4_single = Q4_cum - Q3_cum

    但需要注意: Q1 是一年第一个累计点 (只有3个月)
    Q1_single = Q1_cumulative
    Q2_single = H1_cumulative - Q1_cumulative
    Q3_single = 9M_cumulative - H1_cumulative
    Q4_single = FY_cumulative - 9M_cumulative

    关键: 识别每个列是哪个季度的累计点
    YYYY0331 = Q1累计(3个月), YYYY0630 = H1累计(6个月)
    YYYY0930 = 9M累计(9个月), YYYY1231 = FY累计(12个月)
    """
    return values  # 先不做转换，后面按需处理


def extract_quarter(dates, values, quarter_mmdd):
    """从 dates 中提取指定季度末尾日期对应的值索引"""
    result = {}
    for i, d in enumerate(dates):
        if d[4:] == quarter_mmdd:  # 0331, 0630, 0930, 1231
            year = int(d[:4])
            result[year] = i
    return result


def get_ttm_window(dates, values, year, quarter_mmdd):
    """获取 TTM 窗口 (最近4季单列) 的营收，用于资产周转率"""
    # 需要根据当前日期找到前4季的单季值
    pass


# ============================================================
# 2. 六大指标提取
# ============================================================

def extract_contract_liabilities(bs_rows):
    """
    合同负债 (2018新准则后) 或 预收款项 (2018前)
    双名搜索，优先合同负债
    """
    for key in bs_rows:
        if "合同负债" in key:
            return key, bs_rows[key]
    for key in bs_rows:
        if "预收款项" in key or "预收账款" in key:
            return key, bs_rows[key]
    return None, None


def extract_field(rows, keywords, table_name=""):
    """模糊搜索字段名，支持多个关键词匹配"""
    if rows is None:
        return None, None
    # 精确匹配优先
    for key in rows:
        for kw in keywords:
            if kw == key:
                return key, rows[key]
    # 模糊匹配
    for key in rows:
        for kw in keywords:
            if kw in key:
                return key, rows[key]
    return None, None


def safe_float(s):
    """安全转浮点，空字符串/非数字返回 NaN"""
    if s is None or s == "" or s == "--" or s == "0.00":
        # 注意: "0.00" may be legitimate, but often from empty fields in vDOWN
        pass
    try:
        v = float(s)
        return v
    except (ValueError, TypeError):
        return np.nan


# ============================================================
# 3. 主流程
# ============================================================

def process_stock(code, name):
    """处理一只股票：拉三张表 → 提取6指标"""
    print(f"  [{code} {name}]", end=" ", flush=True)

    # 拉数据
    bs_raw = fetch_table(code, "BS")
    cf_raw = fetch_table(code, "CF")
    pl_raw = fetch_table(code, "PL")

    bs_dates, bs_rows = parse_table(bs_raw)
    cf_dates, cf_rows = parse_table(cf_raw)
    pl_dates, pl_rows = parse_table(pl_raw)

    if bs_dates is None or cf_dates is None:
        print("[FAIL] data parse error")
        return None

    # 统一日期基准 (用 BS 的日期作为主时间轴, BS 通常列最多)
    dates = bs_dates

    # 提取6个指标
    # Lead 指标
    cl_key, cl_vals = extract_contract_liabilities(bs_rows)  # 合同负债/预收款项
    cf_goods_key, cf_goods_vals = extract_field(cf_rows, ["购买商品、接受劳务支付的现金"])  # 现金购买
    cf_capex_key, cf_capex_vals = extract_field(cf_rows, ["购建固定资产、无形资产和其他长期资产所支付的现金"])  # CAPEX

    # Lag 指标
    inv_key, inv_vals = extract_field(bs_rows, ["存货"])  # 存货（不含跌价）
    cip_key, cip_vals = extract_field(bs_rows, ["在建工程(合计)", "在建工程"])  # 在建工程
    total_assets_key, ta_vals = extract_field(bs_rows, ["资产总计"])  # 总资产

    # 资产周转率需要营收 (从 PL)
    rev_key, rev_vals = extract_field(pl_rows, ["营业收入"])
    # 如果 PL 没拉到, 尝试从 PL 的"一、营业总收入"或类似名称

    # 对齐日期 → 统一用 BS dates 为基准
    # 需要建立 date → index 映射
    bs_date_idx = {d: i for i, d in enumerate(bs_dates)}
    cf_date_idx = {d: i for i, d in enumerate(cf_dates)} if cf_dates else {}
    pl_date_idx = {d: i for i, d in enumerate(pl_dates)} if pl_dates else {}

    n_quarters = len(dates)

    # 输出季度级别数据
    stock_data = {
        "code": code,
        "name": name,
        "quarters": [],
    }

    for i, d in enumerate(dates):
        q = {
            "date": d,
            "contract_liab": safe_float(cl_vals[i]) if cl_vals and i < len(cl_vals) else np.nan,
            "cash_paid_goods": np.nan,
            "capex": np.nan,
            "inventory": safe_float(inv_vals[i]) if inv_vals and i < len(inv_vals) else np.nan,
            "construction": safe_float(cip_vals[i]) if cip_vals and i < len(cip_vals) else np.nan,
            "total_assets": safe_float(ta_vals[i]) if ta_vals and i < len(ta_vals) else np.nan,
            "revenue": np.nan,
        }

        # 从 CF 表取数(日期对齐)
        if d in cf_date_idx:
            ci = cf_date_idx[d]
            if cf_goods_vals and ci < len(cf_goods_vals):
                q["cash_paid_goods"] = safe_float(cf_goods_vals[ci])
            if cf_capex_vals and ci < len(cf_capex_vals):
                q["capex"] = safe_float(cf_capex_vals[ci])

        # 从 PL 表取营收
        if d in pl_date_idx:
            pi = pl_date_idx[d]
            if rev_vals and pi < len(rev_vals):
                q["revenue"] = safe_float(rev_vals[pi])

        stock_data["quarters"].append(q)

    # 统计有效季度数
    valid_q = sum(1 for q in stock_data["quarters"]
                  if not (np.isnan(q["inventory"]) and np.isnan(q["total_assets"])))
    print(f"{valid_q}季有效", flush=True)

    return stock_data


def cumulative_to_single_quarter(quarters):
    """
    将累计制值转换为单季值。
    新浪 vDOWN 的 CF 和 PL 是累计制 (从年初累计到报告期)
    BS 是时点值 (不需要转换)

    累计制识别: Q1(0331) < Q2(0630) < Q3(0930) < Q4(1231)
    不跨年: Q1是每年新累计起点

    转换规则:
    - Q1 (0331): 单季 = 累计值 (就是3个月)
    - Q2 (0630): 单季 = 累计值 - Q1累计值
    - Q3 (0930): 单季 = 累计值 - Q2累计值
    - Q4 (1231): 单季 = 累计值 - Q3累计值

    需要处理的字段: revenue, cash_paid_goods, capex
    不需要处理的字段: contract_liab, inventory, construction, total_assets (BS时点值)
    """
    # 按年份分组
    by_year = {}
    for q in quarters:
        y = int(q["date"][:4])
        mmdd = q["date"][4:]
        by_year.setdefault(y, {})[mmdd] = q

    cumulative_fields = ["revenue", "cash_paid_goods", "capex"]

    for year, year_data in sorted(by_year.items()):
        q1 = year_data.get("0331")
        q2 = year_data.get("0630")
        q3 = year_data.get("0930")
        q4 = year_data.get("1231")

        for field in cumulative_fields:
            # Q2单季 = Q2累计 - Q1累计
            if q2 and q1:
                q2_val = q2[field]
                q1_val = q1[field]
                if not np.isnan(q2_val) and not np.isnan(q1_val) and q2_val > 0:
                    q2[f"{field}_single"] = q2_val - q1_val
                else:
                    q2[f"{field}_single"] = np.nan

            # Q3单季 = Q3累计 - Q2累计
            if q3 and q2:
                q3_val = q3[field]
                q2_val_cum = q2[field]
                if not np.isnan(q3_val) and not np.isnan(q2_val_cum) and q3_val > 0:
                    q3[f"{field}_single"] = q3_val - q2_val_cum
                else:
                    q3[f"{field}_single"] = np.nan

            # Q4单季 = Q4累计 - Q3累计
            if q4 and q3:
                q4_val = q4[field]
                q3_val_cum = q3[field]
                if not np.isnan(q4_val) and not np.isnan(q3_val_cum) and q4_val > 0:
                    q4[f"{field}_single"] = q4_val - q3_val_cum
                else:
                    q4[f"{field}_single"] = np.nan

            # Q1单季 = Q1累计值
            if q1:
                q1[f"{field}_single"] = q1[field] if not np.isnan(q1[field]) else np.nan


def calc_ttm_revenue(quarters):
    """计算每个季度的 TTM 营收 = 最近4个单季营收之和
    注意: quarters 是新→旧排列 (新浪 vDOWN 格式)
    使用日期对齐而非索引对齐，避免排序方向 bug"""
    # 按日期升序排列 (旧→新)，便于 TTM 滑窗
    sorted_qs = sorted(quarters, key=lambda q: q["date"])

    for i, q in enumerate(sorted_qs):
        # 最近4季 (包括当前季, 在当前季及之前3季中找)
        ttm = 0.0
        valid_count = 0
        for j in range(max(0, i - 3), i + 1):
            rev_s = sorted_qs[j].get("revenue_single", np.nan)
            if not np.isnan(rev_s) and rev_s > 0:
                ttm += rev_s
                valid_count += 1
        q["revenue_ttm"] = ttm if valid_count >= 3 else np.nan


def calc_asset_turnover(quarters):
    """资产周转率 = TTM营收 / 总资产(当季末)"""
    for q in quarters:
        rev_ttm = q.get("revenue_ttm", np.nan)
        ta = q.get("total_assets", np.nan)
        if not np.isnan(rev_ttm) and not np.isnan(ta) and ta > 0:
            q["asset_turnover"] = rev_ttm / ta
        else:
            q["asset_turnover"] = np.nan


def calc_yoy(quarters):
    """计算5个指标的 YoY（资产周转率除外，它是比率不取YoY）"""
    yoy_fields = ["contract_liab", "cash_paid_goods_single", "capex_single", "inventory", "construction"]

    # 建立日期索引
    date_map = {}
    for i, q in enumerate(quarters):
        date_map[q["date"]] = i

    for i, q in enumerate(quarters):
        d = q["date"]
        year = int(d[:4])
        mmdd = d[4:]
        prev_d = f"{year-1}{mmdd}"

        for field in yoy_fields:
            cur_val = q.get(field, np.nan)
            # 对于单季字段，使用 _single 版本
            src_field = field

            if np.isnan(cur_val) or cur_val == 0:
                q[f"{field}_yoy"] = np.nan
                continue

            if prev_d in date_map:
                prev_q = quarters[date_map[prev_d]]

                # 对于 CF 字段，优先使用 _single
                if field in ["cash_paid_goods_single", "capex_single"]:
                    prev_val = prev_q.get(field, np.nan)
                else:
                    prev_val = prev_q.get(field, np.nan)

                if not np.isnan(prev_val) and prev_val != 0:
                    q[f"{field}_yoy"] = (cur_val - prev_val) / abs(prev_val)
                else:
                    q[f"{field}_yoy"] = np.nan
            else:
                q[f"{field}_yoy"] = np.nan


def winsorize(series, p_low=0.02, p_high=0.98):
    """Winsorize 一个序列"""
    arr = np.array(series, dtype=float)
    mask = ~np.isnan(arr)
    if mask.sum() < 5:
        return arr
    low = np.percentile(arr[mask], p_low * 100)
    high = np.percentile(arr[mask], p_high * 100)
    arr2 = arr.copy()
    arr2[mask] = np.clip(arr[mask], low, high)
    return arr2


def compute_zscore(values, expanding=True):
    """
    计算 z-score 序列。
    expanding=True: 扩展窗口 (固定起点，终点延伸)
    最小窗口: MIN_Z_WINDOW 季
    """
    arr = np.array(values, dtype=float)
    z = np.full(len(arr), np.nan)

    for i in range(len(arr)):
        if i < MIN_Z_WINDOW - 1:
            continue
        # 取历史到当前的所有值
        hist = arr[:i+1]
        valid = hist[~np.isnan(hist)]
        if len(valid) < MIN_Z_WINDOW:
            continue
        mu = np.mean(valid)
        sigma = np.std(valid, ddof=1)  # 样本标准差
        if sigma == 0 or np.isnan(sigma):
            continue
        if not np.isnan(arr[i]):
            z[i] = (arr[i] - mu) / sigma

    return z


# ============================================================
# 4. 主入口
# ============================================================

def main():
    print("=" * 70)
    print(f"SW3 锂电池 — 产业链 Z-Score Phase 1")
    print(f"成分股: {len(STOCKS)} 只")
    print(f"z-score 最小窗口: {MIN_Z_WINDOW} 季")
    print(f"缓存目录: {CACHE_DIR}")
    print("=" * 70)

    # ---- Step 1: 拉取所有股票数据 ----
    print("\n[Step 1/5] 拉取新浪 vDOWN 数据...")
    all_stocks = []
    failed = []
    for code, name in STOCKS:
        result = process_stock(code, name)
        if result:
            all_stocks.append(result)
        else:
            failed.append((code, name))

    print(f"\n  成功: {len(all_stocks)}/{len(STOCKS)}, 失败: {len(failed)}")
    if failed:
        print(f"  失败列表: {[c for c,n in failed]}")

    # ---- Step 2: 累计→单季 + TTM + 资产周转率 + YoY ----
    print("\n[Step 2/5] 累计→单季转换 + TTM + 资产周转率 + YoY...")
    for s in all_stocks:
        cumulative_to_single_quarter(s["quarters"])
        calc_ttm_revenue(s["quarters"])
        calc_asset_turnover(s["quarters"])
        calc_yoy(s["quarters"])

    # 统计每个股票的有效季度
    for s in all_stocks:
        valid = [q for q in s["quarters"]
                 if not np.isnan(q.get("inventory", np.nan))]
        s["n_valid_quarters"] = len(valid)

    valid_stocks = [s for s in all_stocks if s["n_valid_quarters"] >= 8]
    print(f"  有效股票(≥8季): {len(valid_stocks)}/{len(all_stocks)}")

    # ---- Step 3: SW3 聚合 (中位数) ----
    print("\n[Step 3/5] SW3层级聚合...")

    # 收集所有日期
    all_dates_set = set()
    for s in valid_stocks:
        for q in s["quarters"]:
            all_dates_set.add(q["date"])
    all_dates = sorted(all_dates_set)

    # 6个指标的 YoY (资产周转率本身是比率)
    indicators_yoy = [
        "contract_liab_yoy",
        "cash_paid_goods_single_yoy",
        "capex_single_yoy",
        "inventory_yoy",
        "construction_yoy",
    ]

    # 对每个季度，收集所有股票的指标值 → winsorize → 取中位数
    sw3_panel = []  # [{date, n_stocks, contract_liab_yoy, ...}]

    for d in all_dates:
        row = {"date": d, "n_stocks": 0}

        for ind in indicators_yoy:
            vals = []
            for s in valid_stocks:
                for q in s["quarters"]:
                    if q["date"] == d:
                        v = q.get(ind, np.nan)
                        if not np.isnan(v) and abs(v) < 50:  # 过滤异常极端值 (>5000%)
                            vals.append(v)
                        break

            if len(vals) >= 5:  # 至少5只股票才聚合
                w_vals = winsorize(vals)
                row[ind] = np.nanmedian(w_vals)
            else:
                row[ind] = np.nan

        # 资产周转率单独处理 (非YoY, 原值聚合)
        at_vals = []
        for s in valid_stocks:
            for q in s["quarters"]:
                if q["date"] == d:
                    v = q.get("asset_turnover", np.nan)
                    if not np.isnan(v) and 0 < v < 10:  # 合理范围
                        at_vals.append(v)
                    break
        if len(at_vals) >= 5:
            w_at = winsorize(at_vals)
            row["asset_turnover"] = np.nanmedian(w_at)
        else:
            row["asset_turnover"] = np.nan

        # 统计有效股票数
        all_counts = []
        for ind in indicators_yoy + ["asset_turnover"]:
            v = row.get(ind)
            if v is not None and not (isinstance(v, float) and np.isnan(v)):
                # 回数有多少股票贡献了这个值
                pass
        row["n_stocks"] = len([s for s in valid_stocks
                               if any(q["date"] == d for q in s["quarters"])])

        sw3_panel.append(row)

    print(f"  面板: {len(sw3_panel)} 个季度")

    # ---- Step 4: z-score + 合成 ----
    print("\n[Step 4/5] z-score 标准化 + Lead/Lag 合成...")

    # 对每个聚合后的指标算 z-score
    z_fields = {
        "contract_liab_z": "contract_liab_yoy",
        "cash_goods_z": "cash_paid_goods_single_yoy",
        "capex_z": "capex_single_yoy",
        "inventory_z": "inventory_yoy",
        "construction_z": "construction_yoy",
        "asset_turnover_raw_z": "asset_turnover",
    }

    for z_name, src_name in z_fields.items():
        series = [row.get(src_name, np.nan) for row in sw3_panel]
        z_series = compute_zscore(series, expanding=True)
        for i, row in enumerate(sw3_panel):
            row[z_name] = z_series[i] if not np.isnan(z_series[i]) else None

    # 资产周转率取反 (高效→低位→底部)
    for row in sw3_panel:
        z_val = row.get("asset_turnover_raw_z")
        if z_val is not None:
            row["asset_turnover_z"] = -z_val
        else:
            row["asset_turnover_z"] = None

    # 合成
    for row in sw3_panel:
        lead_vals = [row.get("contract_liab_z"), row.get("cash_goods_z"), row.get("capex_z")]
        lag_vals = [row.get("inventory_z"), row.get("construction_z"), row.get("asset_turnover_z")]

        lead_valid = [v for v in lead_vals if v is not None]
        lag_valid = [v for v in lag_vals if v is not None]

        row["Lead_z"] = np.mean(lead_valid) if len(lead_valid) >= 2 else None
        row["Lag_z"] = np.mean(lag_valid) if len(lag_valid) >= 2 else None

        if row["Lead_z"] is not None and row["Lag_z"] is not None:
            row["Divergence"] = row["Lead_z"] - row["Lag_z"]
            row["Overall_z"] = (row["Lead_z"] + row["Lag_z"]) / 2
        else:
            row["Divergence"] = None
            row["Overall_z"] = None

    # Δdiv(2Q)
    for i, row in enumerate(sw3_panel):
        if i >= 2:
            div_cur = row.get("Divergence")
            div_prev2 = sw3_panel[i-2].get("Divergence")
            if div_cur is not None and div_prev2 is not None:
                row["delta_div_2q"] = div_cur - div_prev2
            else:
                row["delta_div_2q"] = None
        else:
            row["delta_div_2q"] = None

    # ---- Step 5: 信号判定 + 输出 ----
    print("\n[Step 5/5] 信号判定 + 输出...")

    # 找最新有效季度
    latest = None
    for row in reversed(sw3_panel):
        if row.get("Lead_z") is not None and row.get("Lag_z") is not None:
            latest = row
            break

    # 信号判定
    def check_signals(row, prev_row, prev2_row):
        signals = []
        lead = row.get("Lead_z")
        lag = row.get("Lag_z")
        div = row.get("Divergence")
        delta = row.get("delta_div_2q")

        if lead is None or lag is None:
            return signals

        # T1: 高置信底
        if lead < -1.0 and lag < -0.5:
            signals.append("T1_高置信底")

        # T2: 拐点底 (分歧从负转正)
        if prev_row:
            prev_div = prev_row.get("Divergence")
            if prev_div is not None and div is not None:
                if prev_div < 0 and div > 0 and lag < -0.2:
                    signals.append("T2_拐点底")

        # T3: 拐点顶 (分歧从正转负)
        if prev_row:
            prev_div = prev_row.get("Divergence")
            if prev_div is not None and div is not None:
                if prev_div > 0 and div < 0 and lag > 0.2:
                    signals.append("T3_拐点顶")

        # T4: 高置信顶
        if delta is not None and delta < -0.8 and lag > 0:
            signals.append("T4_高置信顶")

        return signals

    all_signals = []
    for i, row in enumerate(sw3_panel):
        prev_row = sw3_panel[i-1] if i > 0 else None
        prev2_row = sw3_panel[i-2] if i > 1 else None
        sigs = check_signals(row, prev_row, prev2_row)
        row["signals"] = sigs
        if sigs:
            all_signals.append({"date": row["date"], "signals": sigs,
                                "Lead_z": row.get("Lead_z"), "Lag_z": row.get("Lag_z"),
                                "Divergence": row.get("Divergence")})

    # 9-Bucket 定位
    def bucket_position(lead_z, lag_z):
        if lead_z is None or lag_z is None:
            return "N/A"
        lead_tier = "Low" if lead_z < -0.5 else ("High" if lead_z > 0.5 else "Mid")
        lag_tier = "Low" if lag_z < -0.5 else ("High" if lag_z > 0.5 else "Mid")
        return f"Lead{lead_tier} x Lag{lag_tier}"

    # ---- 构建输出 ----
    # 完整面板（仅有效z-score季度）
    valid_panel = [row for row in sw3_panel if row.get("Lead_z") is not None]

    output = {
        "_meta": {
            "title": "SW3 锂电池 — 产业链 Z-Score Phase 1",
            "date": "2026-07-10",
            "sw3_name": SW3_NAME,
            "n_stocks_total": len(STOCKS),
            "n_stocks_fetched": len(all_stocks),
            "n_stocks_valid": len(valid_stocks),
            "failed_stocks": [{"code": c, "name": n} for c, n in failed],
            "min_z_window": MIN_Z_WINDOW,
            "n_quarters_panel": len(valid_panel),
            "date_range": f"{valid_panel[0]['date']} → {valid_panel[-1]['date']}" if valid_panel else "N/A",
        },
        "current_status": {
            "date": latest["date"] if latest else None,
            "n_stocks_reporting": latest["n_stocks"] if latest else None,
            "indicators": {
                "contract_liab_z": latest.get("contract_liab_z") if latest else None,
                "cash_goods_z": latest.get("cash_goods_z") if latest else None,
                "capex_z": latest.get("capex_z") if latest else None,
                "inventory_z": latest.get("inventory_z") if latest else None,
                "construction_z": latest.get("construction_z") if latest else None,
                "asset_turnover_z": latest.get("asset_turnover_z") if latest else None,
            },
            "synthesis": {
                "Lead_z": latest.get("Lead_z") if latest else None,
                "Lag_z": latest.get("Lag_z") if latest else None,
                "Overall_z": latest.get("Overall_z") if latest else None,
                "Divergence": latest.get("Divergence") if latest else None,
                "delta_div_2q": latest.get("delta_div_2q") if latest else None,
            },
            "bucket": bucket_position(latest.get("Lead_z"), latest.get("Lag_z")) if latest else None,
            "signals": latest.get("signals", []) if latest else [],
        },
        "signal_history": all_signals,
        "panel_last_8q": [
            {
                "date": row["date"],
                "Lead_z": row.get("Lead_z"),
                "Lag_z": row.get("Lag_z"),
                "Divergence": row.get("Divergence"),
                "Overall_z": row.get("Overall_z"),
                "delta_div_2q": row.get("delta_div_2q"),
                "signals": row.get("signals", []),
                "bucket": bucket_position(row.get("Lead_z"), row.get("Lag_z")),
            }
            for row in valid_panel[-8:]
        ],
    }

    # 写 JSON
    output_path = OUTPUT_DIR / "SW3_锂电池_ZScore_20260710.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    # 写完整面板 CSV
    csv_path = OUTPUT_DIR / "SW3_锂电池_panel_20260710.csv"
    csv_fields = ["date", "n_stocks", "contract_liab_z", "cash_goods_z", "capex_z",
                   "inventory_z", "construction_z", "asset_turnover_z",
                   "Lead_z", "Lag_z", "Divergence", "Overall_z", "delta_div_2q"]
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(",".join(csv_fields) + "\n")
        for row in valid_panel:
            vals = [str(row.get(k, "")) if row.get(k) is not None else "" for k in csv_fields]
            f.write(",".join(vals) + "\n")

    print(f"\n{'='*70}")
    print(f"[DONE]")
    print(f"   JSON: {output_path}")
    print(f"   CSV:  {csv_path}")
    print(f"{'='*70}")

    # ---- 终端输出当前状态 ----
    if latest:
        print(f"\n{'='*50}")
        print(f"  SW3 锂电池 -- 当前状态 ({latest['date']})")
        print(f"  有效股票数: {latest['n_stocks']}")
        print(f"{'='*50}")

        inds = output["current_status"]["indicators"]
        syn = output["current_status"]["synthesis"]

        for name, val in inds.items():
            bar = "#" * min(int(abs(val or 0) * 5), 15)
            direction = "/\\" if (val or 0) > 0 else "\\/"
            print(f"  {name:25s} {val or 'N/A':>7.3f}  {direction} {bar}")

        print(f"  {'='*50}")
        for name, val in syn.items():
            print(f"  {name:25s} {val or 'N/A':>7.3f}")
        print(f"  {'='*50}")
        print(f"  9-Bucket:  {output['current_status']['bucket']}")
        print(f"  信号:      {', '.join(output['current_status']['signals']) if output['current_status']['signals'] else '无触发'}")
        print(f"{'='*50}")

    # 信号历史
    if all_signals:
        print(f"\n  历史信号触发 ({len(all_signals)}次):")
        for s in all_signals:
            print(f"    {s['date']}  {'+'.join(s['signals']):20s}  Lead={s['Lead_z']:.2f}  Lag={s['Lag_z']:.2f}  Div={s['Divergence']:.2f}")

    return output


if __name__ == "__main__":
    main()
