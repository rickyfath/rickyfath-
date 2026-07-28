"""
fetch_report.py — A股研究助手 v2.0 数据层
一次拉取一只股票的四关所需数据，输出结构化 JSON

用法: python fetch_report.py <股票代码>
示例: python fetch_report.py 002156
      python fetch_report.py 688187

数据源:
  K线: 新浪 Finance API (月/周/日)
  财务: akshare → 新浪财经 fallback
  分类: 本地 CSV (全市场概念分类表_v3.csv, L3黑名单)

输出: JSON to stdout
"""

import json
import sys
import os
import urllib.request
import csv
from datetime import date, datetime
from collections import OrderedDict

# === 配置 ===
CSV_DIR = r"C:\Users\Administrator\Desktop\股票库"
CONCEPT_CSV = os.path.join(CSV_DIR, "全市场概念分类表_v3.csv")
BLACKLIST_CSV = os.path.join(CSV_DIR, "L3_滞胀期BK黑名单_v1.3.csv")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def resolve_code(raw_code: str):
    """解析股票代码，返回 (code_6digit, market_name, sina_symbol)"""
    code = raw_code.strip().replace(".SZ", "").replace(".SH", "").replace(".BJ", "")
    code = code.zfill(6)

    sh_prefixes = ("688", "600", "601", "603", "605")
    sz_prefixes = ("000", "001", "002", "003", "300", "301")
    bj_prefixes = ("8", "4")

    if code.startswith(sh_prefixes):
        return code, "上交所", f"sh{code}"
    elif code.startswith(sz_prefixes):
        return code, "深交所", f"sz{code}"
    elif code.startswith(bj_prefixes):
        return code, "北交所", f"bj{code}"
    else:
        return code, "未知", f"sz{code}"


def http_get(url, timeout=15):
    """带 UA 的 HTTP GET，返回 (text, error)"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8"), None
    except Exception as e:
        return None, str(e)


# === K线数据（新浪 API — 已验证可用） ===

def fetch_kline_sina(sina_symbol: str, scale: int, count: int):
    """
    scale: 240=日线, 1200=周线, 7200=月线
    返回: list of {day, open, high, low, close, volume}
    """
    url = (f"https://quotes.sina.cn/cn/api/jsonp_v2.php/data/"
           f"CN_MarketDataService.getKLineData?symbol={sina_symbol}&scale={scale}&ma=no&datalen={count}")
    raw, err = http_get(url)
    if err:
        return None, err

    # 解析 data([...]) 或 (...[...]) 包装
    try:
        # 找 JSON 数组
        start = raw.find("[")
        end = raw.rfind("]")
        if start == -1 or end == -1:
            return None, f"no JSON array in response: {raw[:100]}"
        inner = raw[start:end + 1]
        data = json.loads(inner)
        return data, None
    except Exception as e:
        return None, f"parse error: {str(e)[:100]}"


def compute_ma(closes, n):
    """计算 N 日均线"""
    result = []
    for i in range(len(closes)):
        if i < n - 1:
            result.append(None)
        else:
            result.append(round(sum(closes[i - n + 1:i + 1]) / n, 2))
    return result


def compute_slope(values, n=5):
    """最近 N 个有效值的斜率百分比"""
    valid = [v for v in values if v is not None]
    if len(valid) < n or valid[-n] == 0:
        return None
    return round((valid[-1] - valid[-n]) / valid[-n] * 100, 2)


def compute_atr(bars, n=14):
    """ATR(14)"""
    if len(bars) < n + 1:
        return None
    tr_list = []
    for i in range(1, len(bars)):
        high = float(bars[i]["high"])
        low = float(bars[i]["low"])
        prev_close = float(bars[i - 1]["close"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
    if len(tr_list) < n:
        return None
    return round(sum(tr_list[-n:]) / n, 2)


def monthly_from_daily(daily_bars):
    """从日线数据聚合成月线（简化版：按年月分组）"""
    months = OrderedDict()
    for bar in daily_bars:
        day_str = bar.get("day", "")
        if len(day_str) < 7:
            continue
        ym = day_str[:7]
        if ym not in months:
            months[ym] = {"day": ym + "-01", "open": float(bar["open"]),
                          "high": float(bar["high"]), "low": float(bar["low"]),
                          "close": float(bar["close"]), "volume": int(float(bar["volume"]))}
        else:
            m = months[ym]
            m["high"] = max(m["high"], float(bar["high"]))
            m["low"] = min(m["low"], float(bar["low"]))
            m["close"] = float(bar["close"])
            m["volume"] += int(float(bar["volume"]))
    return list(months.values())


def weekly_from_daily(daily_bars):
    """从日线数据聚合成周线（简化版：按ISO周分组）"""
    weeks = OrderedDict()
    for bar in daily_bars:
        day_str = bar.get("day", "")
        if len(day_str) < 10:
            continue
        try:
            d = date.fromisoformat(day_str)
            iso_week = d.isocalendar()[:2]  # (year, week)
        except ValueError:
            continue
        if iso_week not in weeks:
            weeks[iso_week] = {"day": day_str, "open": float(bar["open"]),
                               "high": float(bar["high"]), "low": float(bar["low"]),
                               "close": float(bar["close"]), "volume": int(float(bar["volume"]))}
        else:
            w = weeks[iso_week]
            w["high"] = max(w["high"], float(bar["high"]))
            w["low"] = min(w["low"], float(bar["low"]))
            w["close"] = float(bar["close"])
            w["volume"] += int(float(bar["volume"]))
    return list(weeks.values())


def analyze_technicals(sina_symbol: str):
    """拉取 K线，计算技术指标。日线→聚合周线/月线"""
    result = {
        "daily": {"data": None, "error": None, "indicators": {}},
        "weekly": {"data": None, "error": None, "indicators": {}},
        "monthly": {"data": None, "error": None, "indicators": {}},
        "sources_ok": [],
        "missing": []
    }

    # 拉取日线（500根 ≈ 2年）
    daily_bars, err = fetch_kline_sina(sina_symbol, 240, 500)
    if not daily_bars or len(daily_bars) < 60:
        result["daily"]["error"] = err or f"insufficient bars ({len(daily_bars or [])})"
        result["missing"].append("daily K-line")
        return result

    result["daily"]["data"] = f"{len(daily_bars)} bars"
    result["sources_ok"].append("daily:新浪API")

    closes = [float(b["close"]) for b in daily_bars]
    ma20 = compute_ma(closes, 20)
    ma30 = compute_ma(closes, 30)
    ma60 = compute_ma(closes, 60)
    atr14 = compute_atr(daily_bars, 14)

    last_close = closes[-1]
    last_ma20 = ma20[-1] or 0
    last_ma30 = ma30[-1] or 0
    last_ma60 = ma60[-1] or 0
    ma20_slope = compute_slope(ma20, 5)
    ma30_slope = compute_slope(ma30, 5)
    dev_ma20 = round((last_close - last_ma20) / last_ma20 * 100, 2) if last_ma20 else None

    # 量比（近20日 vs 前20日）
    vols = [int(float(b["volume"])) for b in daily_bars]
    if len(vols) >= 40:
        vol_ratio = round(sum(vols[-20:]) / (sum(vols[-40:-20]) or 1), 2)
    else:
        vol_ratio = None

    chg_5d = round((closes[-1] - closes[-5]) / closes[-5] * 100, 2) if len(closes) >= 5 else None
    chg_20d = round((closes[-1] - closes[-20]) / closes[-20] * 100, 2) if len(closes) >= 20 else None

    # 多头排列
    if last_ma20 and last_ma30 and last_ma60:
        if last_ma20 > last_ma30 > last_ma60:
            alignment = "bull"
        elif last_ma20 < last_ma30 < last_ma60:
            alignment = "bear"
        else:
            alignment = "mixed"
    else:
        alignment = "unknown"

    # 趋势判定
    if ma20_slope is not None:
        if ma20_slope < -0.5 and (ma30_slope or 0) < 0:
            trend = "真DOWN"
        elif ma20_slope > 0.1 and (ma30_slope or 0) >= -0.1:
            trend = "上升"
        elif abs(ma20_slope) < 0.5:
            trend = "震荡"
        else:
            trend = "偏弱"
    else:
        trend = "unknown"

    result["daily"]["indicators"] = {
        "date": daily_bars[-1]["day"],
        "close": last_close,
        "MA20": last_ma20, "MA30": last_ma30, "MA60": last_ma60,
        "MA20_slope_pct": ma20_slope,
        "MA30_direction": "UP" if (ma30_slope or 0) > 0 else "DOWN",
        "deviation_from_MA20_pct": dev_ma20,
        "ATR14": atr14,
        "ATR_pct": round(atr14 / last_close * 100, 2) if atr14 and last_close else None,
        "volume_ratio_20d": vol_ratio,
        "chg_5d_pct": chg_5d,
        "chg_20d_pct": chg_20d,
        "alignment": alignment,
        "trend": trend
    }

    # 周线（从日线聚合）
    weekly_bars = weekly_from_daily(daily_bars)
    if len(weekly_bars) >= 20:
        result["weekly"]["data"] = f"{len(weekly_bars)} bars"
        result["sources_ok"].append("weekly:聚合自日线")
        w_closes = [float(b["close"]) for b in weekly_bars]
        w_ma20 = compute_ma(w_closes, 20)
        w_slope = compute_slope(w_ma20, 4)
        result["weekly"]["indicators"] = {
            "close": w_closes[-1],
            "MA20_direction": "UP" if (w_slope or 0) > 0 else "DOWN",
            "bars": len(weekly_bars)
        }
    else:
        result["weekly"]["error"] = f"insufficient weeks ({len(weekly_bars)})"
        result["missing"].append("weekly K-line")

    # 月线（从日线聚合）
    monthly_bars = monthly_from_daily(daily_bars)
    if len(monthly_bars) >= 6:
        result["monthly"]["data"] = f"{len(monthly_bars)} bars"
        result["sources_ok"].append("monthly:聚合自日线")
        m_closes = [float(b["close"]) for b in monthly_bars]
        m_highs = [float(b["high"]) for b in monthly_bars]
        ath = max(m_highs)
        # 月线连阳
        consecutive_green = 0
        for b in reversed(monthly_bars):
            if float(b["close"]) > float(b["open"]):
                consecutive_green += 1
            else:
                break
        # 近3月涨跌
        chg_3m = None
        if len(m_closes) >= 4:
            chg_3m = round((m_closes[-1] - m_closes[-4]) / m_closes[-4] * 100, 2)

        result["monthly"]["indicators"] = {
            "close": m_closes[-1],
            "ATH": ath,
            "distance_from_ATH_pct": round((m_closes[-1] - ath) / ath * 100, 2) if ath else None,
            "consecutive_green_months": consecutive_green,
            "chg_3m_pct": chg_3m,
            "bars": len(monthly_bars)
        }
    else:
        result["monthly"]["error"] = f"insufficient months ({len(monthly_bars)})"
        result["missing"].append("monthly K-line")

    return result


# === 财务数据（新浪 vDOWN — 直接 HTTP, 不需要 akshare）===

def parse_sina_tsv(raw_bytes):
    """解析新浪 vDOWN GBK TSV 格式 → dict {科目名: [各期数值]}"""
    text = raw_bytes.decode('gbk', errors='replace')
    lines = text.strip().split('\n')
    if len(lines) < 3:
        return {}
    # 第一行: 日期头  \t20260331\t20251231\t...
    dates = lines[0].strip().split('\t')[1:]  # 跳过第一个空字段
    # 第二行: 单位
    unit = lines[1].strip().split('\t')[1] if len(lines[1].strip().split('\t')) > 1 else '元'
    result = {'_dates': dates, '_unit': unit}
    for line in lines[2:]:
        parts = line.strip().split('\t')
        if len(parts) >= 2:
            key = parts[0].strip()
            values = parts[1:]
            # 只保留数字列（与 dates 对齐）
            num_vals = []
            for v in values[:len(dates)]:
                try:
                    num_vals.append(float(v) if v else 0.0)
                except ValueError:
                    num_vals.append(v)
            result[key] = num_vals
    return result


def fetch_sina_vdown_raw(code, report_type):
    """新浪 vDOWN 接口 — 直接返回 raw bytes（GBK 编码）"""
    url = f'https://vip.stock.finance.sina.com.cn/corp/go.php/vDOWN_{report_type}/displaytype/4/stockid/{code}.phtml'
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read(), None
    except Exception as e:
        return None, str(e)


def fetch_sina_balance_sheet(code):
    raw, err = fetch_sina_vdown_raw(code, "BalanceSheet")
    if err: return None
    return parse_sina_tsv(raw)


def fetch_profit_via_ths(code):
    """akshare 同花顺接口 — 利润表 + 关键比率（已验证可用）"""
    try:
        import akshare as ak
        df = ak.stock_financial_abstract_ths(symbol=code, indicator='按报告期')
        if df is not None and not df.empty:
            return df, None
        return None, "empty DataFrame"
    except Exception as e:
        return None, str(e)[:200]


def fetch_sina_cashflow(code):
    raw, err = fetch_sina_vdown_raw(code, "CashFlow")
    if err: return None
    return parse_sina_tsv(raw)


def extract_key_financials(bs, pl, cf):
    """从三张表提取关键指标"""
    result = {}

    # 资产负债表关键项（取最新一期，index 0）
    bs_keys = {
        '货币资金': 'cash', '应收账款': 'ar', '存货': 'inventory',
        '商誉': 'goodwill', '短期借款': 'short_borrow',
        '长期借款': 'long_borrow', '应付债券': 'bonds_payable',
        '资产总计': 'total_assets', '流动负债合计': 'current_liab',
        '归属于母公司股东权益合计': 'equity_parent',
        '固定资产': 'fixed_assets', '在建工程': 'construction',
        '应付票据及应付账款': 'ap', '合同负债': 'contract_liab',
    }
    if bs:
        for cn_key, en_key in bs_keys.items():
            if cn_key in bs:
                vals = bs[cn_key]
                result[en_key] = vals[0] if vals else 0.0
        # 计算衍生指标
        equity = result.get('equity_parent', 0) or 1
        result['goodwill_to_equity_pct'] = round(result.get('goodwill', 0) / equity * 100, 1) if equity > 0 else 0
        debt = result.get('short_borrow', 0) + result.get('long_borrow', 0) + result.get('bonds_payable', 0)
        result['interest_debt'] = debt
        result['debt_to_equity_pct'] = round(debt / equity * 100, 1) if equity > 0 else 0
        result['total_liab_to_assets_pct'] = round(result.get('current_liab', 0) / result.get('total_assets', 1) * 100, 1)

    # 利润表关键项
    pl_keys = {
        '营业总收入': 'revenue', '营业总支出': 'total_cost',
        '研发费用': 'rd_expense', '销售费用': 'sell_expense',
        '管理费用': 'admin_expense', '财务费用': 'fin_expense',
        '净利润': 'net_profit', '归属于母公司所有者的净利润': 'np_parent',
    }
    if pl:
        for cn_key, en_key in pl_keys.items():
            if cn_key in pl:
                vals = pl[cn_key]
                result[en_key] = vals[0] if vals else 0.0
        # 营收增速（最新期 vs 去年同期 = index 0 vs index 4，如果是季度数据）
        if 'revenue' in result and len(pl.get('营业总收入', [])) >= 5:
            revs = pl['营业总收入']
            curr, prev = revs[0], revs[4] if len(revs) > 4 else revs[-1]
            if prev and prev != 0:
                result['revenue_yoy_pct'] = round((curr - prev) / abs(prev) * 100, 1)
        # NP增速
        if 'np_parent' in result and '归属于母公司所有者的净利润' in pl:
            nps = pl['归属于母公司所有者的净利润']
            if len(nps) >= 5:
                curr_np, prev_np = nps[0], nps[4]
                if prev_np and prev_np != 0:
                    result['np_yoy_pct'] = round((curr_np - prev_np) / abs(prev_np) * 100, 1)

    # 现金流表关键项
    cf_keys = {
        '经营活动产生的现金流量净额': 'cfo',
        '购建固定资产、无形资产和其他长期资产所支付的现金': 'capex',
    }
    if cf:
        for cn_key, en_key in cf_keys.items():
            if cn_key in cf:
                vals = cf[cn_key]
                result[en_key] = vals[0] if vals else 0.0
        if result.get('cfo') and result.get('np_parent'):
            np = result['np_parent']
            result['cfo_to_np'] = round(result['cfo'] / np, 2) if np != 0 else None

    return result


# === CSV 分类查询 ===

def lookup_csv(code: str):
    """从本地 CSV 查询股票分类信息"""
    result = {"concept": None, "blacklist": None}

    # 查概念分类表（主CSV + 备选CSV）
    concept_csv_paths = [CONCEPT_CSV, os.path.join(CSV_DIR, "A股全市场.csv")]
    for csv_path in concept_csv_paths:
        if not os.path.exists(csv_path):
            continue
        try:
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    csv_code = (row.get("code", "") or row.get("代码", "")).strip()
                    csv_code_6 = csv_code.replace(".SZ", "").replace(".SH", "").replace(".BJ", "")
                    if csv_code_6 == code:
                        result["concept"] = {
                            "name": row.get("name", "") or row.get("名称", ""),
                            "SW1": row.get("SW1", "") or row.get("申万一级", ""),
                            "SSHY": row.get("SSHY", ""),
                            "主赛道": row.get("主赛道", ""),
                            "概念标签": row.get("概念标签", ""),
                            "GPM": row.get("GPM", ""),
                            "PE": row.get("PE", ""),
                            "市值": row.get("市值", ""),
                            "Tier": row.get("Tier", ""),
                        }
                        break
            if result["concept"] is not None:
                break
        except Exception as e:
            continue
    if result["concept"] is None:
        result["concept"] = {"error": "not found in any CSV"}

    # 查 L3 黑名单
    if os.path.exists(BLACKLIST_CSV):
        try:
            with open(BLACKLIST_CSV, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    csv_code = (row.get("code", "") or row.get("代码", "")).strip()
                    if not csv_code:
                        # 有些行 code 为空，按名称匹配
                        continue
                    csv_code_6 = csv_code.replace(".SZ", "").replace(".SH", "").replace(".BJ", "")
                    if csv_code_6 == code:
                        result["blacklist"] = {
                            "L3_判定": row.get("L3_判定", "未知"),
                            "L3_来源": row.get("L3_来源", ""),
                            "Tier": row.get("Tier", ""),
                            "主赛道": row.get("主赛道", ""),
                        }
                        break
            if result["blacklist"] is None:
                result["blacklist"] = {"L3_判定": "未收录"}
        except Exception as e:
            result["blacklist"] = {"error": f"CSV read error: {str(e)[:100]}"}
    else:
        result["blacklist"] = {"error": f"file not found: {BLACKLIST_CSV}"}

    return result


# === 合规数据（质押 + 审计代理检测）===

# 缓存质押全量数据，避免每次调用都抓取
_pledge_cache = None

def fetch_compliance(code: str):
    """获取质押比例 + ST审计代理检测"""
    global _pledge_cache
    result = {
        "pledge_ratio_pct": None,       # 大股东质押比例(%)
        "pledge_shares": None,          # 质押股数(万)
        "pledge_detail": "未获取",
        "audit_opinion": "需核实",       # akshare无审计意见接口
        "audit_proxy": "",              # ST检测代理
    }

    # --- 质押比例 ---
    try:
        import akshare as ak
        if _pledge_cache is None:
            _pledge_cache = ak.stock_gpzy_pledge_ratio_em()
        df = _pledge_cache
        row = df[df['股票代码'] == code]
        if not row.empty:
            r = row.iloc[0]
            result["pledge_ratio_pct"] = float(r.get('质押比例', 0))
            result["pledge_shares"] = float(r.get('质押股数', 0))
            result["pledge_detail"] = (
                f"质押比例={result['pledge_ratio_pct']}%, "
                f"质押股数={result['pledge_shares']:.0f}万股"
            )
        else:
            result["pledge_detail"] = "未在质押名单中(可能无质押)"
    except Exception as e:
        result["pledge_detail"] = f"质押数据获取失败: {str(e)[:80]}"

    # --- ST 审计代理检测 ---
    # ST/*ST 通常意味着审计意见非标或持续经营问题
    try:
        import akshare as ak
        st_df = ak.stock_zh_a_st_em()
        st_row = st_df[st_df['代码'] == code]
        if not st_row.empty:
            st_name = str(st_row.iloc[0].get('名称', ''))
            result["audit_opinion"] = "⚠️ ST/*ST — 审计意见极可能非标"
            result["audit_proxy"] = f"股票在ST名单中: {st_name}"
        else:
            result["audit_proxy"] = "非ST, 审计意见需手动核实年报"
    except Exception:
        # ST检测不关键，失败了就当非ST处理
        result["audit_proxy"] = "ST检测接口暂不可用, 审计意见需手动核实年报"

    return result


# === 主流程 ===

def generate_report(raw_code: str):
    """生成完整研究数据包"""
    code, market, sina_symbol = resolve_code(raw_code)

    report = {
        "meta": {
            "code": f"{code}.{'SH' if market == '上交所' else 'SZ' if market == '深交所' else 'BJ'}",
            "name": None,
            "market": market,
            "date": date.today().isoformat(),
            "version": "2.0"
        },
        "g1_integrity": {},
        "g2_business": {},
        "g3_financials": {},
        "g4_technicals": {},
        "data_quality": {
            "sources_ok": [],
            "sources_failed": [],
            "missing_fields": [],
        }
    }

    # G2: CSV 分类（先查，确定股票身份）
    csv_info = lookup_csv(code)
    report["g2_business"] = csv_info
    if csv_info.get("concept") and "error" not in csv_info["concept"]:
        report["meta"]["name"] = csv_info["concept"].get("name")
        report["data_quality"]["sources_ok"].append("全市场概念分类表_v3.csv")
    else:
        report["data_quality"]["sources_failed"].append(
            f"概念分类表CSV: {csv_info.get('concept', {}).get('error', 'unknown')}"
        )

    if csv_info.get("blacklist") and "error" not in csv_info["blacklist"]:
        report["data_quality"]["sources_ok"].append("L3黑名单CSV")

    # G4: K线
    tech = analyze_technicals(sina_symbol)
    report["g4_technicals"] = tech
    for src in tech.get("sources_ok", []):
        report["data_quality"]["sources_ok"].append(src)
    for miss in tech.get("missing", []):
        report["data_quality"]["missing_fields"].append(miss)

    # G3: 财务数据
    bs = fetch_sina_balance_sheet(code)         # 新浪 vDOWN BS — working
    cf = fetch_sina_cashflow(code)              # 新浪 vDOWN CF — working
    pl_df, pl_err = fetch_profit_via_ths(code)  # akshare THS — PL + key ratios

    # 从 BS/CF 提取硬指标
    fin = extract_key_financials(bs, None, cf) if (bs or cf) else {}

    # 从 THS 提取利润表指标
    ths_metrics = {}
    if pl_df is not None and hasattr(pl_df, 'iloc'):
        try:
            latest = pl_df.iloc[-1]
            col_map = {
                '营业总收入': 'revenue', '归母净利润': 'np_parent',
                '扣非净利润': 'deduct_np', '销售毛利率': 'gpm',
                '净资产收益率': 'roe_weighted', '营业总收入同比增长': 'revenue_yoy',
                '归母净利润同比增长': 'np_yoy', '基本每股收益': 'eps',
                '每股净资产': 'bps', '资产负债率': 'debt_ratio',
            }
            # 金额类指标 — THS 可能返回 亿/万/元 混合, 归一化到 元
            MONETARY_KEYS = {'营业总收入', '归母净利润', '扣非净利润'}
            for cn, en in col_map.items():
                if cn in pl_df.columns:
                    val = str(latest[cn])
                    try:
                        if cn in MONETARY_KEYS:
                            if '亿' in val:
                                num = float(val.replace('亿', '')) * 1e8
                            elif '万' in val:
                                num = float(val.replace('万', '')) * 1e4
                            else:
                                num = float(val)
                                if num < 100:  # 纯数字且<100 → 实际是亿
                                    num = num * 1e8
                            ths_metrics[en] = num
                        else:
                            ths_metrics[en] = float(val.replace('%', ''))
                    except (ValueError, TypeError):
                        ths_metrics[en] = str(val)
            # 如果 PL 有 '归母净利润同比增长'，它是百分比形式
            if '归母净利润同比增长' in pl_df.columns:
                try:
                    ths_metrics['np_yoy_pct'] = float(str(latest['归母净利润同比增长']).replace('%',''))
                except: pass
            if '营业总收入同比增长' in pl_df.columns:
                try:
                    ths_metrics['revenue_yoy_pct'] = float(str(latest['营业总收入同比增长']).replace('%',''))
                except: pass
        except Exception as e:
            ths_metrics['_error'] = str(e)[:100]

    # 合并
    fin.update(ths_metrics)

    # GPM fallback: THS优先，否则从CSV取
    if 'gpm' not in fin or not fin.get('gpm'):
        concept = csv_info.get("concept", {}) if isinstance(csv_info.get("concept"), dict) and "error" not in csv_info["concept"] else {}
        if concept.get("GPM"):
            fin['gpm'] = float(concept['GPM'])

    report["g3_financials"] = {
        "source": "新浪vDOWN(BS+CF) + akshareTHS(PL)",
        "metrics": fin,
        "bs_available": bs is not None,
        "pl_available": pl_df is not None,
        "cf_available": cf is not None,
    }
    report["data_quality"]["sources_ok"].append("新浪vDOWN(BS+CF)+akshareTHS(PL)")

    # G1: 诚信检查（从财务数据 + CSV 提取）
    integrity_flags = []
    concept = csv_info.get("concept", {}) if csv_info.get("concept") else {}

    # 从新浪 vDOWN 提取的硬指标
    goodwill_pct = fin.get('goodwill_to_equity_pct', 0) if fin else 0
    debt_pct = fin.get('debt_to_equity_pct', 0) if fin else 0
    cfo_np = fin.get('cfo_to_np') if fin else None

    if goodwill_pct and goodwill_pct > 30:
        integrity_flags.append(f"商誉/净资产={goodwill_pct}% > 30% — [WARN]减值风险")
    elif goodwill_pct is not None and goodwill_pct >= 0:
        integrity_flags.append(f"商誉/净资产={goodwill_pct}% [OK]")
    if debt_pct and debt_pct > 70:
        integrity_flags.append(f"有息负债/净资产={debt_pct}% > 70% — [WARN]债务风险")
    if cfo_np is not None:
        if cfo_np < 0.5:
            integrity_flags.append(f"CFO/NP={cfo_np} < 0.5 — [WARN]利润含金量不足")
        else:
            integrity_flags.append(f"CFO/NP={cfo_np} [OK]")

    # 从 CSV 可提取的基础标记
    if concept and "error" not in concept:
        try:
            gpm = float(concept.get("GPM", 0))
            if gpm < 12:
                integrity_flags.append(f"GPM={gpm}%<12% — 天然上限")
        except ValueError:
            pass

    # 从黑名单提取
    bl = csv_info.get("blacklist", {}) if csv_info.get("blacklist") else {}
    if bl.get("L3_判定") == "[不碰]":
        integrity_flags.append(f"L3黑名单: 不碰 — {bl.get('L3_来源', '')}")

    # G1 补充: 合规数据（质押 + 审计代理检测）
    compliance = fetch_compliance(code)
    report["g1_compliance"] = compliance

    # 质押标记
    pledge_pct = compliance.get("pledge_ratio_pct")
    if pledge_pct is not None:
        if pledge_pct > 80:
            integrity_flags.append(f"[WARN] 大股东质押={pledge_pct}% > 80%")
        elif pledge_pct > 50:
            integrity_flags.append(f"[FLAG] 大股东质押={pledge_pct}% > 50%")
    # 审计代理
    if compliance.get("audit_opinion", "").startswith("⚠️"):
        integrity_flags.append(f"[WARN] {compliance['audit_opinion']}")

    report["g1_integrity"] = {
        "flags": integrity_flags if integrity_flags else ["无明显硬伤"],
        "note": "质押/审计/商誉/CFO — akshare + Sina vDOWN"
    }

    return report


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python fetch_report.py <股票代码>")
        print("示例: python fetch_report.py 002156")
        sys.exit(1)

    code = sys.argv[1]
    report = generate_report(code)
    # 确保控制台能输出中文
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
