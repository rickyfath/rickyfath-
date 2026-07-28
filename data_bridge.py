"""
data_bridge.py — A股投资体系 v5.0 数据桥
========================================
一次跑完所有前置脚本 + 数据采集，输出结构化 JSON。
Hermes 直接读取此 JSON，填入 schema 模板 + 做文字推理（PART 1/PART 6）。

用法:
  python data_bridge.py <股票代码> [--quick]
  python data_bridge.py 688008
  python data_bridge.py 688981 --quick

输出:
  C:/Users/Administrator/Desktop/<代码>_data_bridge.json
"""

import json, sys, os, re, csv, subprocess, urllib.request
from datetime import date

SCRIPTS_DIR  = r"C:\Users\Administrator\AppData\Local\hermes\scripts"
FINANCE_DIR  = r"C:\Users\Administrator\Desktop\finance"
DESKTOP      = r"C:\Users\Administrator\Desktop"
FETCH_REPORT = os.path.join(FINANCE_DIR, "fetch_report.py")
HERMES_FIN   = os.path.join(FINANCE_DIR, "hermes_finance.py")
WW_SCRIPT    = os.path.join(SCRIPTS_DIR, "ww_indicator.py")
SP500_SCRIPT = os.path.join(SCRIPTS_DIR, "compute_sp500_score.py")

# CSV paths — try multiple locations
CSV_PATHS = [
    os.path.join(DESKTOP, "股票库", "A股全市场.csv"),
    os.path.join(DESKTOP, "股票库", "全市场概念分类表_v3.csv"),
    os.path.join(DESKTOP, "A股全市场.csv"),
]
BLACKLIST_PATHS = [
    os.path.join(DESKTOP, "股票库", "L3_滞胀期BK黑名单_v2.0.csv"),
    os.path.join(DESKTOP, "L3_滞胀期BK黑名单_v2.0.csv"),
]

TODAY = date.today().isoformat()


def safe_float(val):
    if val is None: return None
    try: return float(val)
    except: return None


def run_script(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout, cwd=FINANCE_DIR, encoding='gbk', errors='replace')
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT", -1
    except Exception as e:
        return None, str(e), -1


def parse_json_from_output(stdout_text):
    """从混合输出中提取 JSON 对象"""
    if not stdout_text: return None
    # 尝试直接解析
    try: return json.loads(stdout_text)
    except: pass
    # 找最外层 {}
    m = re.search(r'\{.*\}', stdout_text, re.DOTALL)
    if m:
        try: return json.loads(m.group())
        except: pass
    return None


def find_existing_csv(paths):
    for p in paths:
        if os.path.exists(p): return p
    return None


# ═══════════════════════════════════════
# Step A: W&W 情绪指标
# ═══════════════════════════════════════
def step_a_ww():
    result = {
        "executed": False, "ww": "未知", "zone": "未知",
        "multiplier": 0.4, "six_factors": {}, "trend_20d": [], "error": None
    }
    stdout, stderr, rc = run_script(f'python "{WW_SCRIPT}"')
    if rc != 0:
        result["error"] = f"脚本返回码 {rc}: {stderr[:200] if stderr else '未知错误'}"
        return result

    result["executed"] = True

    # 解析 W&W 数值 — 从 stdout 找 "W&W: 85.6" 模式
    for line in stdout.split('\n'):
        m = re.search(r'W&W:\s*([\d.]+)', line)
        if m:
            try:
                result["ww"] = float(m.group(1))
            except: pass
            break

    # 如果 stdout 解析失败，用 CSV 兜底
    if not isinstance(result["ww"], (int, float)):
        try:
            import pandas as pd
            df = pd.read_csv('/tmp/ww_indicator.csv')
            if 'W&W' in df.columns:
                result["ww"] = round(float(df.dropna(subset=['W&W']).iloc[-1]['W&W']), 1)
        except: pass

    # 区间
    ww = result["ww"] if isinstance(result["ww"], (int, float)) else 99
    if   ww < 20: result["zone"] = "Very Bullish"
    elif ww < 40: result["zone"] = "Bullish"
    elif ww < 60: result["zone"] = "Neutral"
    elif ww < 80: result["zone"] = "Bearish"
    else:         result["zone"] = "Very Bearish"

    result["multiplier"] = {"Very Bullish":1.2, "Bullish":1.0, "Neutral":1.0, "Bearish":0.6, "Very Bearish":0.4}.get(result["zone"], 1.0)

    # 六因子 / 趋势 — 从 CSV 读（比解析 stdout 更可靠）
    try:
        import pandas as pd
        df = pd.read_csv('/tmp/ww_indicator.csv')
        if 'W&W' in df.columns:
            latest = df.dropna(subset=['W&W']).iloc[-1]
            factor_map = {
                'f1_pct': '融资余额', 'f2_pct': 'CSI300_PE', 'f3_pct': '融资买入比',
                'f4_pct': '10年国债', 'f5_pct': '成交量', 'f6_pct': '60日动量'
            }
            for col, label in factor_map.items():
                if col in df.columns:
                    val = safe_float(latest[col])
                    if val is not None:
                        result["six_factors"][label] = round(val, 1)

            # 最近 7 个有效读数
            recent = df.dropna(subset=['W&W']).tail(7)
            for _, row in recent.iterrows():
                result["trend_20d"].append({
                    "date": str(row['dt'])[:10],
                    "value": round(float(row['W&W']), 1)
                })
    except Exception as e:
        # fallback: 从 stdout 解析六因子
        factor_labels = {
            "融资余额": "融资余额", "CSI300 PE": "CSI300_PE", "融资买入比": "融资买入比",
            "10年国债": "10年国债", "成交量": "成交量", "60日动量": "60日动量"
        }
        for label, key in factor_labels.items():
            for line in stdout.split('\n'):
                if label in line:
                    parts = line.strip().split()
                    for p in parts:
                        v = safe_float(p)
                        if v is not None and 0 <= v <= 100:
                            result["six_factors"][key] = round(v, 1)
                            break
                    break

    return result


# ═══════════════════════════════════════
# Step B: SP500 风控
# ═══════════════════════════════════════
def step_b_sp500():
    result = {
        "executed": False, "sp500": "未知", "vix": "未知", "l1_level": "未知",
        "intervention": False, "intervention_detail": "",
        "composite_score": "未知", "percentile": "未知",
        "z_scores": {"VIX_z":"未知","HY_z":"未知","SP偏离_z":"未知","MA斜率_z":"未知","Sahm_z":"未知"},
        "error": None
    }
    stdout, stderr, rc = run_script(f'python "{SP500_SCRIPT}"')
    if rc != 0:
        result["error"] = f"返回码 {rc}: {stderr[:200] if stderr else '未知错误'}"
        return result

    result["executed"] = True
    for line in stdout.split('\n'):
        line_s = line.strip()
        if 'SP500' in line_s and ':' in line_s and 'Sina' in line_s:
            try: result["sp500"] = float(line_s.split(':')[-1].strip().split()[0])
            except: pass
        if 'VIX' in line_s and ':' in line_s and 'Yahoo' in line_s:
            try: result["vix"] = float(line_s.split(':')[-1].strip().split()[0])
            except: pass
        if u'状态' in line_s or u'状态' in line_s:
            m = re.search(r'L\d', line_s)
            if m: result["l1_level"] = m.group()
        if u'百分位' in line_s:
            try: result["percentile"] = float(re.search(r'([\d.]+)%', line_s).group(1))
            except: pass

    # z-scores
    for line in stdout.split('\n'):
        m = re.findall(r'(\w+[zZ])=?=\s*([-\d.nan]+)', line)
        for key, val in m:
            if 'VIX' in key: result["z_scores"]["VIX_z"] = val if val!='nan' else "未知"
            if 'HY' in key: result["z_scores"]["HY_z"] = val if val!='nan' else "未知"
            if 'Sahm' in key: result["z_scores"]["Sahm_z"] = val if val!='nan' else "未知"

    # v1.1: VIX sanity guard — if VIX < 20 and model outputs L4/L5, it's likely a false reading
    # (missing Z-scores cause inflated composite). Downgrade to L2.
    vix_val = result.get("vix")
    if isinstance(vix_val, (int, float)) and vix_val < 20 and result["l1_level"] in ("L4", "L5"):
        result["l1_level"] = "L2"
        result["intervention"] = False
        result["intervention_detail"] = (
            f"L1={result['l1_level']} (模型原报L4/L5, VIX={vix_val:.1f}<20 → VIX守卫降级至L2, 不介入)"
        )
    elif result["l1_level"] in ("L4", "L5"):
        result["intervention"] = True
        cap = "35%" if result["l1_level"]=="L4" else "15%"
        result["intervention_detail"] = f"L1={result['l1_level']} -> 仓位 <= {cap}"
    return result


# ═══════════════════════════════════════
# Step C: K线 + 财务 (fetch_report.py)
# ═══════════════════════════════════════
def step_c_fetch_report(code):
    code_6 = code.zfill(6)
    result = {
        "executed": False, "daily": {}, "weekly": {}, "monthly": {},
        "financials": {}, "error": None
    }
    stdout, stderr, rc = run_script(f'python "{FETCH_REPORT}" {code_6}')
    if rc != 0:
        result["error"] = f"fetch_report.py 返回码 {rc}: {stderr[:200] if stderr else '未知错误'}"
        return result

    data = parse_json_from_output(stdout)
    if data is None:
        result["error"] = f"无法解析 fetch_report.py JSON: {stdout[:300]}"
        return result

    result["executed"] = True
    tech = data.get("g4_technicals", {})
    fin_metrics = data.get("g3_financials", {}).get("metrics", {})

    # v1.3: 独立拉取日线计算MA5/MA200 (fetch_report不提供)
    ma5_value = "未知"
    ma200_value = "未知"
    try:
        sina_sym = ("sh" if code_6.startswith(('6','68')) else "sz") + code_6
        url = (f"https://quotes.sina.cn/cn/api/jsonp_v2.php/"
               f"data/CN_MarketDataService.getKLineData"
               f"?symbol={sina_sym}&scale=240&ma=no&datalen=210")
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn"
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8")
            s = raw.find("[")
            e = raw.rfind("]")
            if s != -1 and e != -1:
                bars = json.loads(raw[s:e+1])
                closes = []
                for b in bars:
                    try:
                        closes.append(float(b["close"]))
                    except (ValueError, KeyError):
                        continue
                if len(closes) >= 5:
                    ma5_value = round(sum(closes[-5:]) / 5, 2)
                if len(closes) >= 200:
                    ma200_value = round(sum(closes[-200:]) / 200, 2)
    except Exception:
        pass  # MA5/MA200计算失败, 保持"未知"

    # ── 日线 ──
    daily_ind = tech.get("daily", {}).get("indicators", {})
    if daily_ind:
        trend_raw = daily_ind.get("trend", "unknown")
        # fetch_report.py 可能返回编码后的中文，做一次规范化
        trend_map = {u"上升":"上升", u"震荡":"震荡", u"真DOWN":"真DOWN", u"偏弱":"震荡"}
        trend = trend_map.get(trend_raw, trend_raw)

        result["daily"] = {
            "date": daily_ind.get("date", TODAY), "close": daily_ind.get("close"),
            "MA5": ma5_value, "MA20": daily_ind.get("MA20"), "MA30": daily_ind.get("MA30"),
            "MA60": daily_ind.get("MA60"), "MA200": ma200_value,
            "MA20_slope_pct": daily_ind.get("MA20_slope_pct"),
            "MA30_direction": daily_ind.get("MA30_direction", "未知"),
            "deviation_from_MA20_pct": daily_ind.get("deviation_from_MA20_pct"),
            "ATR14": daily_ind.get("ATR14"), "ATR_pct": daily_ind.get("ATR_pct"),
            "volume_ratio_20d": daily_ind.get("volume_ratio_20d"),
            "chg_5d_pct": daily_ind.get("chg_5d_pct"),
            "chg_20d_pct": daily_ind.get("chg_20d_pct"),
            "alignment": daily_ind.get("alignment", "未知"),
            "trend": trend, "data_bars": 500
        }
    else:
        result["daily"] = {"error": "fetch_report.py 未返回日线指标"}

    # ── 周线 ──
    # fetch_report.py 周线只返回 close/MA20_direction/bars，不计算 MA20 具体值
    weekly_ind = tech.get("weekly", {}).get("indicators", {})
    weekly_err = tech.get("weekly", {}).get("error")
    if weekly_ind and not weekly_err:
        ma20_dir = weekly_ind.get("MA20_direction", "未知")
        result["weekly"] = {
            "close": weekly_ind.get("close"),
            "MA20_value": "未知 — fetch_report.py 未计算周线MA20具体数值",
            "MA20_direction": ma20_dir,
            "price_vs_MA20_pct": "未知 — 需独立计算周线偏离",
            "vol_trend_4w": "未知 — 周线量未聚合",
            "mid_term_trend": {"UP":"向上","DOWN":"向下"}.get(ma20_dir, "横盘"),
            "data_bars": weekly_ind.get("bars", 0)
        }
    else:
        result["weekly"] = {"error": weekly_err or "fetch_report.py 未返回周线数据"}

    # ── 月线 ──
    monthly_ind = tech.get("monthly", {}).get("indicators", {})
    monthly_err = tech.get("monthly", {}).get("error")
    if monthly_ind and not monthly_err:
        from_ath = monthly_ind.get("distance_from_ATH_pct", 0)
        chg_3m = monthly_ind.get("chg_3m_pct", 0)
        # 推算大周期位置
        if from_ath is not None and from_ath > -15:
            pos = "顶部区" if chg_3m and chg_3m > 30 else "中部"
        elif from_ath is not None and from_ath < -40:
            pos = "底部区"
        elif from_ath is not None and from_ath < -20:
            pos = "下跌中继"
        else:
            pos = "中部"

        result["monthly"] = {
            "close": monthly_ind.get("close"), "ATH": monthly_ind.get("ATH"),
            "distance_from_ATH_pct": from_ath,
            "consecutive_green_months": monthly_ind.get("consecutive_green_months", 0),
            "chg_3m_pct": chg_3m, "cycle_position": pos,
            "data_bars": monthly_ind.get("bars", 0)
        }
    else:
        result["monthly"] = {"error": monthly_err or "fetch_report.py 未返回月线数据"}

    # ── 财务 ──
    cfo_raw = safe_float(fin_metrics.get("cfo"))
    np_raw  = safe_float(fin_metrics.get("net_profit")) or safe_float(fin_metrics.get("np_parent")) or safe_float(fin_metrics.get("deduct_np"))
    # 单位归一化: fetch_report.py 已将金额统一归一化为元 → 统一转亿
    def to_yi(raw, label="value"):
        if raw is None:
            return None
        return raw / 1e8
    cfo_yi = to_yi(cfo_raw, "CFO")
    np_yi = to_yi(np_raw, "NP")
    cfo_np_val = round(cfo_yi / np_yi, 2) if (cfo_yi and np_yi and np_yi != 0) else None

    # ── 合规 ──
    compliance = data.get("g1_compliance", {})
    pledge_pct = safe_float(compliance.get("pledge_ratio_pct"))
    pledge_shares = safe_float(compliance.get("pledge_shares"))
    audit_raw = compliance.get("audit_opinion", "需核实")
    # 股票名称从 meta 取（fetch_report.py 从 CSV 注入）
    stock_name = data.get("meta", {}).get("name", "") or code_6

    # 如果需要核实，生成 Tavily 搜索指令给 Hermes
    audit_needs_verify = ("核实" in str(audit_raw) or "未知" in str(audit_raw))
    tavily_audit = ""
    if audit_needs_verify:
        tavily_audit = (
            f"TAVILY_SEARCH: '{stock_name} {code_6} 年报 审计意见 会计师事务所' "
            f"— 查找最新年报的审计意见类型(标准无保留/保留/无法表示/否定)"
        )

    result["financials"] = {
        "GPM": safe_float(fin_metrics.get("gpm")),
        "NP_rate": "未知 — THS未返回单季NP率",
        "ROE": safe_float(fin_metrics.get("roe_weighted")),
        "ROE_note": "Q1加权ROE(单季年化)" if fin_metrics.get("roe_weighted") else "未知",
        "nonrec_pct": "未知 — 需4季归母+扣非汇总",
        "goodwill_to_equity_pct": safe_float(fin_metrics.get("goodwill_to_equity_pct", 0)),
        "debt_to_equity_pct": safe_float(fin_metrics.get("debt_to_equity_pct")),
        "cfo_to_np": cfo_np_val,
        "cfo_note": f"[脚本输出] CFO={cfo_yi:.2f}亿, NP={np_yi:.2f}亿, CFO/NP={cfo_np_val}" if cfo_np_val is not None else "未知",
        "revenue_yoy_pct": "未知 — fetch_report未返回YoY增速",
        "ar": safe_float(fin_metrics.get("ar")),
        "ap": safe_float(fin_metrics.get("ap")),
        "contract_liability": "未知 — BS未返回合同负债",
        "inventory": safe_float(fin_metrics.get("inventory")),
        "total_assets": safe_float(fin_metrics.get("total_assets")),
        "equity": safe_float(fin_metrics.get("equity_parent")),
        "bps": safe_float(fin_metrics.get("bps")),
        "eps": safe_float(fin_metrics.get("eps")),
        "total_shares_note": "[计算推导] 总股本≈equity/bps, 用于PB计算。Hermes: PB应使用price/bps, 不要用流通股本。",
        # 合规字段
        "pledge_ratio_pct": pledge_pct,
        "pledge_detail": compliance.get("pledge_detail", "未获取"),
        "audit_opinion": audit_raw,
        "audit_proxy": compliance.get("audit_proxy", ""),
        "audit_tavily_search": tavily_audit,  # Hermes: 用 Tavily 查审计意见
    }
    return result


# ═══════════════════════════════════════
# Step D: CSV 分类查询
# ═══════════════════════════════════════
def step_d_csv(code):
    code_6 = code.zfill(6)
    result = {
        "executed": False, "name": "未知", "SW1": "未知", "SW2": "未知", "SW3": "未知",
        "SSHY": "未知", "主赛道": "未知", "概念标签": "未知", "自定义赛道": "无",
        "L3_判定": "未收录", "L3_来源": "未收录于 L3 黑名单",
        "L3_T4_signal": "数据缺失", "L3_T2_signal": "数据缺失",
        "GPM_from_csv": None, "csv_timestamp": "未知",
        "csv_freshness_ok": False, "peers_raw": [], "error": None
    }

    # —— 查全市场分类表（列名可能是 代码/code, 申万一级/SW1 等）——
    csv_path = find_existing_csv(CSV_PATHS)
    if csv_path:
        result["executed"] = True
        try:
            with open(csv_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                headers = reader.fieldnames or []
                # 自适应列名
                code_col = next((h for h in headers if h.strip() in ('code','代码')), None)
                name_col = next((h for h in headers if h.strip() in ('name','名称','股票名称')), None)
                sw1_cols = [h for h in headers if h.strip() in ('SW1','申万一级')]
                sw2_cols = [h for h in headers if h.strip() in ('SW2','申万二级')]
                sw3_cols = [h for h in headers if h.strip() in ('SW3','申万三级')]
                sshy_col = next((h for h in headers if h.strip() == 'SSHY'), None)
                track_cols = [h for h in headers if h.strip() in ('主赛道',)]
                tag_col = next((h for h in headers if h.strip() in ('概念标签',)), None)
                gpm_cols = [h for h in headers if h.strip() in ('GPM','GPM_Q1','GPM_FY25')]

                for row in reader:
                    raw_code = (row.get(code_col, "") if code_col else "") or ""
                    csv_code = raw_code.strip().replace(".SH","").replace(".SZ","").replace(".BJ","")
                    if csv_code == code_6:
                        # name
                        if name_col:
                            v = (row.get(name_col, "") or "").strip()
                            if v: result["name"] = v
                        # SW1
                        for c in sw1_cols:
                            v = (row.get(c, "") or "").strip()
                            if v: result["SW1"] = v; break
                        # SW2
                        for c in sw2_cols:
                            v = (row.get(c, "") or "").strip()
                            if v: result["SW2"] = v; break
                        # SW3
                        for c in sw3_cols:
                            v = (row.get(c, "") or "").strip()
                            if v: result["SW3"] = v; break
                        # SSHY
                        if sshy_col:
                            v = (row.get(sshy_col, "") or "").strip()
                            if v: result["SSHY"] = v
                        # 主赛道
                        for c in track_cols:
                            v = (row.get(c, "") or "").strip()
                            if v: result["主赛道"] = v; break
                        # 概念标签
                        if tag_col:
                            v = (row.get(tag_col, "") or "").strip()
                            if v: result["概念标签"] = v
                        # GPM
                        for c in gpm_cols:
                            gpm_val = safe_float(row.get(c, ""))
                            if gpm_val is not None:
                                result["GPM_from_csv"] = gpm_val
                                break
                        result["csv_freshness_ok"] = True
                        break

                if result["SW1"] == "未知":
                    result["error"] = f"code={code_6} 未在 {os.path.basename(csv_path)} 中找到"
        except Exception as e:
            result["error"] = f"CSV读取失败: {str(e)[:100]}"
    else:
        result["error"] = "全市场分类CSV文件不存在"

    # —— 查 L3 黑名单 ——
    bl_path = find_existing_csv(BLACKLIST_PATHS)
    if bl_path:
        try:
            with open(bl_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                found = False
                same_sshy_peers = []
                target_sshy = result.get("SSHY", "")

                for row in reader:
                    raw_code = (row.get("code", "") or "").strip()
                    csv_code = raw_code.replace(".SH","").replace(".SZ","").replace(".BJ","")
                    sshy_val = (row.get("SSHY", "") or "").strip()

                    if csv_code == code_6:
                        result["L3_判定"] = (row.get("SW3_判定", "") or row.get("L3_判定", "") or "未收录").strip()
                        result["L3_来源"] = (row.get("判定理由", "") or row.get("L3_来源", "") or "未知").strip()
                        # 从黑名单 CSV 回填 SW2/SW3/name (第一张 CSV 可能没这些列)
                        bl_sw2 = (row.get("SW2", "") or "").strip()
                        bl_sw3 = (row.get("SW3", "") or "").strip()
                        bl_name = (row.get("name", "") or "").strip()
                        if bl_sw2 and (result.get("SW2") in (None, "未知")):
                            result["SW2"] = bl_sw2
                        if bl_sw3 and (result.get("SW3") in (None, "未知")):
                            result["SW3"] = bl_sw3
                        if bl_name and (result.get("name") in (None, "未知")):
                            result["name"] = bl_name
                        found = True
                        break

                    if target_sshy and sshy_val and sshy_val == target_sshy and len(same_sshy_peers) < 5:
                        same_sshy_peers.append({
                            "code": csv_code,
                            "name": (row.get("name", "") or "").strip(),
                            "L3_判定": (row.get("L3_判定", "") or "").strip(),
                            "L3_来源": ((row.get("L3_来源", "") or "").strip())[:150]
                        })

                if not found:
                    result["L3_判定"] = "未收录"
                    result["L3_来源"] = f"{code_6} 不在 L3 黑名单中"
                result["peers_raw"] = same_sshy_peers
        except Exception as e:
            result["error"] = ((result.get("error") or "") + f"; L3黑名单: {str(e)[:100]}").strip("; ")
    else:
        result["error"] = ((result.get("error") or "") + "; L3黑名单CSV不存在").strip("; ")

    # —— 注入 SW3 Z-Score 信号 ——
    sw3_name = result.get("SW3", "")
    if sw3_name and sw3_name != "未知":
        safe_name = sw3_name.replace("/", "_").replace("\\", "_")
        zscore_dir = os.path.join(DESKTOP, "finance", "sw3_output")
        if os.path.isdir(zscore_dir):
            matches = sorted(
                [f for f in os.listdir(zscore_dir)
                 if f.startswith(f"SW3_{safe_name}_ZScore_") and f.endswith(".json")],
                reverse=True
            )
            if matches:
                try:
                    with open(os.path.join(zscore_dir, matches[0]), "r", encoding="utf-8") as zf:
                        zdata = json.load(zf)
                    cs = zdata.get("current_status", {})
                    sigs = cs.get("signals", [])
                    syn = cs.get("synthesis", {})
                    result["L3_T4_signal"] = "有" if any("T4" in s for s in sigs) else "无"
                    result["L3_T2_signal"] = "有" if any("T2" in s for s in sigs) else "无"
                    result["L3_lead_z"] = syn.get("Lead_z")
                    result["L3_lag_z"] = syn.get("Lag_z")
                    result["L3_divergence"] = syn.get("Divergence")
                    result["L3_bucket"] = cs.get("bucket")
                    result["L3_zscore_date"] = cs.get("date")
                except Exception:
                    pass  # Z-Score JSON 损坏或不可读, 静默跳过

    return result


# ═══════════════════════════════════════
# Step F: 8季财务明细 (hermes_finance.py)
# ═══════════════════════════════════════
def step_f_quarterly(code):
    code_6 = code.zfill(6)
    result = {"executed": False, "quarters": [], "metrics_extra": {}, "error": None}

    stdout, stderr, rc = run_script(f'python "{HERMES_FIN}" {code_6}')
    if rc != 0:
        result["error"] = f"hermes_finance.py 返回码 {rc}: {stderr[:200] if stderr else '未知'}"
        return result

    # hermes_finance.py 输出到 股票库/分析记录/{code}_finance.json
    finance_json = os.path.join(DESKTOP, "股票库", "分析记录", f"{code_6}_finance.json")
    if not os.path.exists(finance_json):
        # 尝试从 stdout 直接解析
        data = parse_json_from_output(stdout)
        if data is None:
            result["error"] = "hermes_finance.py JSON 未找到"
            return result
    else:
        with open(finance_json, 'r', encoding='utf-8') as f:
            data = json.load(f)

    result["executed"] = True
    d = data.get("data", {})
    quarters_raw = d.get("quarters", [])
    for q in quarters_raw[-8:]:
        period = str(q.get("period", ""))
        # 将 20260331 / 2026-03-31 / 2026/03/31 统一转为 26Q1
        import re
        m = re.search(r'(\d{4})[/-]?(\d{2})', period)
        if m:
            yy = m.group(1)[2:4]
            mm = m.group(2)
            q_num = {"03":"1", "06":"2", "09":"3", "12":"4"}.get(mm, "?")
            q_label = f"{yy}Q{q_num}"
        else:
            q_label = period

        gpm_val = q.get("gpm")
        if gpm_val is not None:
            try:
                gpm_val = round(float(gpm_val), 2)
            except (ValueError, TypeError):
                gpm_val = "未知 — GPM值无法解析"
        else:
            gpm_val = "未知 — hermes_finance季度数据不含GPM字段"
        result["quarters"].append({
            "q": q_label,
            "revenue": safe_float(q.get("revenue")),
            "net_profit": safe_float(q.get("net_profit")),
            "gpm": gpm_val
        })

    # v1.2: 从8季数据自算NonRec% (归母-扣非)/归母, 不再依赖hermes_finance传递
    qs = result["quarters"]
    if len(qs) >= 4:
        recent = qs[-4:]
        np_sum = sum(q["net_profit"] for q in recent if q["net_profit"] is not None)
        # 扣非从原始数据取
        deduct_sum = 0
        deduct_count = 0
        for q_raw in quarters_raw[-4:]:
            dnp = q_raw.get("deduct_np")
            if dnp is not None:
                try:
                    deduct_sum += float(dnp)
                    deduct_count += 1
                except (ValueError, TypeError):
                    pass
        if np_sum and np_sum != 0 and deduct_count >= 3:
            nonrec = round((np_sum - deduct_sum) / abs(np_sum) * 100, 1)
            result["nonrec_pct"] = nonrec
            result["nonrec_detail"] = f"自算: 归母{np_sum:.2f}亿 扣非{deduct_sum:.2f}亿 NonRec={nonrec}% | {deduct_count}/4季有效"
        else:
            result["nonrec_pct"] = None
            result["nonrec_detail"] = f"自算失败: 归母={np_sum} 扣非有效季数={deduct_count}"
    else:
        result["nonrec_pct"] = None
        result["nonrec_detail"] = "季度数据不足4季"

    # 补充指标
    for k in ["roe_annualized_pct", "cfo_to_np", "revenue_yoy_pct",
              "goodwill_to_equity_pct", "debt_to_equity_pct"]:
        if k in d:
            result["metrics_extra"][k] = d[k]

    return result


# ═══════════════════════════════════════
# 主流程
# ═══════════════════════════════════════
def build_bridge(code, quick=False):
    code_6 = code.strip().replace(".SZ","").replace(".SH","").replace(".BJ","").zfill(6)

    preflight = {
        "step_a": {}, "step_b": {}, "step_c": {}, "step_d": {},
        "step_e": {
            "quadrant": "供给冲击型滞胀", "ceiling_pct": 50,
            "avoid_direction": "成本敏感型中游制造业",
            "ppi_ppirm_gap": -1.9, "data_source": "公开数据近似"
        }
    }

    bridge = {
        "_meta": {
            "bridge_version": "1.0", "code": code_6, "date": TODAY,
            "generated_by": "data_bridge.py - Claude Code",
            "usage": "Hermes: 读取此 JSON, 填入 a-share-trading SKILL 模板。数据已解析, 直接用。"
        },
        "preflight": preflight,
        "quarterly": {}
    }

    print(f"[1/5] W&W ...")
    preflight["step_a"] = step_a_ww()
    print(f"      W&W = {preflight['step_a'].get('ww')}  |  {preflight['step_a'].get('zone')}")

    if not quick:
        print(f"[2/5] SP500 ...")
        preflight["step_b"] = step_b_sp500()
        print(f"      L1 = {preflight['step_b'].get('l1_level')}  |  SP500 = {preflight['step_b'].get('sp500')}")
    else:
        preflight["step_b"] = {
            "executed": False, "sp500": "未知", "vix": "未知", "l1_level": "未知",
            "intervention": False, "intervention_detail": "",
            "composite_score": "未知", "percentile": "未知",
            "z_scores": {"VIX_z":"未知","HY_z":"未知","SP偏离_z":"未知","MA斜率_z":"未知","Sahm_z":"未知"},
            "error": "[--quick] 跳过SP500脚本"
        }

    print(f"[3/5] K线+财务 (fetch_report.py {code_6}) ...")
    preflight["step_c"] = step_c_fetch_report(code_6)
    daily = preflight["step_c"].get("daily", {})
    fin = preflight["step_c"].get("financials", {})
    print(f"      日线: close={daily.get('close')} MA20={daily.get('MA20')} trend={daily.get('trend')}")
    print(f"      财务: GPM={fin.get('GPM')}%  ROE={fin.get('ROE')}%  debt/eq={fin.get('debt_to_equity_pct')}%")

    print(f"[4/5] CSV分类 ...")
    preflight["step_d"] = step_d_csv(code_6)
    # 回填 stock_name + SW2/SW3 到 _meta 供 Hermes 直读
    bridge["_meta"]["stock_name"] = preflight["step_d"].get("name", code_6)
    bridge["_meta"]["SW1"] = preflight["step_d"].get("SW1", "未知")
    bridge["_meta"]["SW2"] = preflight["step_d"].get("SW2", "未知")
    bridge["_meta"]["SW3"] = preflight["step_d"].get("SW3", "未知")
    print(f"      SW1={preflight['step_d'].get('SW1')}  SSHY={preflight['step_d'].get('SSHY')}  L3={preflight['step_d'].get('L3_判定')}")

    print(f"[5/5] 8季明细 (hermes_finance.py) ...")
    bridge["quarterly"] = step_f_quarterly(code_6)
    print(f"      季度数: {len(bridge['quarterly'].get('quarters', []))}")

    # v1.2: 将季度数据自算的NonRec注入financials, 覆盖fetch_report的"未知"
    q_nonrec = bridge["quarterly"].get("nonrec_pct")
    q_nonrec_detail = bridge["quarterly"].get("nonrec_detail", "")
    if q_nonrec is not None:
        preflight["step_c"]["financials"]["nonrec_pct"] = q_nonrec
        preflight["step_c"]["financials"]["nonrec_detail"] = q_nonrec_detail

    # 保存
    out_path = os.path.join(DESKTOP, f"{code_6}_data_bridge.json")
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(bridge, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[DONE] -> {out_path}")
    return out_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python data_bridge.py <股票代码> [--quick]")
        sys.exit(1)
    build_bridge(sys.argv[1], "--quick" in sys.argv)
