"""
rule_engine.py -- L5 Trading Execution Engine v1.1
===================================================
Reads bridge JSON from data_bridge.py, applies L5 rules,
outputs action_signal + full position diagnosis.

v1.1 (2026-07-18):
  - #1 B condition explicitly checks consecutive days below MA20, N<2 -> wait
  - #2 Panic modifier adds CSI300 single-day drop >3% condition
  - #3 Weak B slope tier labels unified to v3.3 raw format
  - #4 Step0 ex-rights detection now uses max(single_day) not cumulative
  - #5 Uses Sina API to independently fetch daily K-line + CSI300

Usage:
  python rule_engine.py <code> [--cost=<cost>] [--position=<pct>] [--holding-type=<type>]

Output:
  stdout: terminal diagnosis report
  JSON:   C:/Users/Administrator/Desktop/<code>_action_signal.json

Trigger keywords (say to Claude):
  "L5 <code>"     -> run rule engine
  "chu xinhao <code>" -> run rule engine
"""

import json, sys, os, re, csv, urllib.request
from datetime import date, datetime

# Windows GBK encoding compat
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

DESKTOP = r"C:\Users\Administrator\Desktop"
FINANCE_DIR = os.path.dirname(os.path.abspath(__file__))
HOLDINGS_PATH = os.path.join(FINANCE_DIR, "holdings.json")
TODAY = date.today().isoformat()

# ============================================================
# External Data Fetch (Sina API)
# ============================================================

def code_to_sina_symbol(code_6):
    """6-digit code -> Sina symbol (sh688008 / sz000001)"""
    if code_6.startswith(('6', '68')):
        return f"sh{code_6}"
    elif code_6.startswith(('0', '3', '2')):
        return f"sz{code_6}"
    else:
        return f"sh{code_6}"


def fetch_daily_kline(sina_sym, count=30):
    """Fetch daily K-line (scale=240), returns [{close, date, ...}, ...]"""
    url = (
        f"https://quotes.sina.cn/cn/api/jsonp_v2.php/"
        f"data/CN_MarketDataService.getKLineData"
        f"?symbol={sina_sym}&scale=240&ma=no&datalen={count}"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn"
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8")
            start = raw.find("[")
            end = raw.rfind("]")
            if start != -1 and end != -1:
                return json.loads(raw[start:end+1]), None
            return None, "parse error"
    except Exception as e:
        return None, str(e)


def fetch_csi300_daily():
    """Fetch CSI300 last 2 days, returns {today_chg_pct, yesterday_close, today_close}"""
    bars, err = fetch_daily_kline("sh000300", 5)
    if err or not bars or len(bars) < 2:
        return None
    try:
        t_close = float(bars[-1]["close"])
        y_close = float(bars[-2]["close"])
        chg_pct = (t_close - y_close) / y_close * 100
        return {
            "today_date": bars[-1].get("day", ""),
            "today_close": t_close,
            "yesterday_close": y_close,
            "chg_pct": round(chg_pct, 2)
        }
    except (ValueError, KeyError):
        return None


def check_consecutive_below_ma20(kline_bars, ma20, n_days=5):
    """
    Check how many consecutive days close has been below MA20.
    Returns: {"consecutive_days": int, "below_dates": [...], "should_wait": bool}
    """
    if not kline_bars or ma20 is None:
        return {"consecutive_days": 0, "below_dates": [], "should_wait": True, "error": "K-line or MA20 data missing"}

    below_count = 0
    below_dates = []
    for bar in reversed(kline_bars):
        try:
            close = float(bar["close"])
        except (ValueError, KeyError):
            continue
        if close < ma20:
            below_count += 1
            below_dates.append(bar.get("day", "?"))
        else:
            break

    should_wait = below_count < 2

    return {
        "consecutive_days": below_count,
        "below_dates": below_dates,
        "should_wait": should_wait,
        "detail": f"Consecutive {below_count} days close < MA20 {ma20:.2f}" + (" -> insufficient <2 days, wait to confirm" if should_wait else " -> meets consecutive 2-day condition")
    }


# ============================================================
# Utility Functions
# ============================================================

def safe_float(val, default=None):
    if val is None: return default
    try: return float(val)
    except: return default


def load_bridge(code):
    """Load bridge JSON from data_bridge.py, compatible with old and new formats"""
    code_6 = code.strip().replace(".SZ","").replace(".SH","").replace(".BJ","").zfill(6)
    path = os.path.join(DESKTOP, f"{code_6}_data_bridge.json")

    if not os.path.exists(path):
        print(f"[ERROR] Bridge file not found: {path}")
        print(f"        Run first: python data_bridge.py {code_6}")
        sys.exit(1)

    with open(path, 'r', encoding='utf-8') as f:
        raw = json.load(f)

    if "preflight" in raw:
        pf = raw["preflight"]
    else:
        pf = raw

    meta = raw.get("_meta", {})
    code_out = meta.get("code", code_6)
    stock_name = meta.get("stock_name", code_6)

    step_a = pf.get("step_a", {})
    step_b = pf.get("step_b", {})
    step_c = pf.get("step_c", {})
    step_d = pf.get("step_d", {})
    step_e = pf.get("step_e", {})

    daily    = step_c.get("daily", {})
    weekly   = step_c.get("weekly", {})
    monthly  = step_c.get("monthly", {})
    fin      = step_c.get("financials", {})

    return {
        "code": code_out, "name": stock_name, "path": path,
        "step_a": step_a, "step_b": step_b, "step_c": step_c,
        "step_d": step_d, "step_e": step_e,
        "daily": daily, "weekly": weekly, "monthly": monthly, "fin": fin
    }


def load_holdings():
    """Load holdings config, auto-inject cost/position/holding_type"""
    if not os.path.exists(HOLDINGS_PATH):
        return {}
    try:
        with open(HOLDINGS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get("holdings", {})
    except Exception:
        return {}


# ============================================================
# Step 0: Pre-checks
# ============================================================

def step0_precheck(bridge, kline_bars=None):
    """Four pre-checks. v1.1: ex-rights detection uses max single-day change."""
    results = []

    # [1] Six-quadrant ROE leading indicator
    quadrant = bridge["step_e"].get("quadrant", "Unknown")
    ceiling  = safe_float(bridge["step_e"].get("ceiling_pct"), 50)
    results.append({
        "check": "L2 Quadrant",
        "value": f"{quadrant} | ceiling {ceiling}%",
        "status": "info"
    })

    # [2] Residual momentum tag (from L4)
    results.append({
        "check": "Residual Momentum Tag",
        "value": "Need L4 data -- bridge JSON missing this field",
        "status": "pending_data"
    })

    # [3] Market style tag
    sw1 = bridge["step_d"].get("SW1", "Unknown")
    results.append({
        "check": "Market Style",
        "value": f"SW1={sw1} (style divergence needs CSI300/CSI500 relative strength)",
        "status": "pending_data"
    })

    # [4] Ex-rights detection -- v1.1: check max single-day, not cumulative
    ex_rights_warn = None
    max_single_day_chg = 0

    if kline_bars and len(kline_bars) >= 22:
        for i in range(1, len(kline_bars)):
            try:
                prev_close = float(kline_bars[i-1]["close"])
                curr_close = float(kline_bars[i]["close"])
                if prev_close > 0:
                    day_chg = abs((curr_close - prev_close) / prev_close * 100)
                    max_single_day_chg = max(max_single_day_chg, day_chg)
            except (ValueError, KeyError):
                continue

        if max_single_day_chg > 30:
            ex_rights_warn = f"Suspected high stock split (single day >{max_single_day_chg:.0f}%) -> wait 10 trading days, MA distorted"
        elif max_single_day_chg > 20:
            ex_rights_warn = f"Suspected ex-rights (single day {max_single_day_chg:.1f}%) -> wait 5 trading days"

        chg_detail = f"Max single-day change in 22 days: {max_single_day_chg:.1f}%"
    else:
        chg_5d = safe_float(bridge["daily"].get("chg_5d_pct"), 0)
        max_single_day_chg = abs(chg_5d) / 5
        chg_detail = f"Daily bars unavailable, using 5d cumulative={chg_5d}% approx (max single ~{max_single_day_chg:.1f}%)"
        if abs(chg_5d) > 40:
            ex_rights_warn = "Suspected ex-rights -- daily bars missing, based on cumulative estimate, verify manually"

    results.append({
        "check": "Ex-rights Detection",
        "value": chg_detail,
        "status": "warn" if ex_rights_warn else "ok",
        "detail": ex_rights_warn
    })

    return results


# ============================================================
# Step 1: Sell Dual-Confirm (A+B)
# ============================================================

def step1_sell_dual_confirm(bridge, kline_bars=None, csi300_data=None):
    """
    A condition = framework layer (L3 industry cycle negation)
    B condition = technical layer (MA20 break + v1.1 consecutive day verification)

    v1.1 additions:
      - B trigger requires explicit "consecutive N days below MA20" check
      - Panic modifier dual condition: WW<20 OR CSI300 single-day drop >3%
    """
    daily = bridge["daily"]
    step_d = bridge["step_d"]
    step_a = bridge["step_a"]

    close = safe_float(daily.get("close"))
    ma20 = safe_float(daily.get("MA20"))
    ma20_slope = safe_float(daily.get("MA20_slope_pct"), 0)
    deviation = safe_float(daily.get("deviation_from_MA20_pct"), 0)
    vol_ratio = safe_float(daily.get("volume_ratio_20d"), 1.0)
    atr_pct = safe_float(daily.get("ATR_pct"), 3.0)

    # v1.1: consecutive days below MA20 verification
    consecutive_info = check_consecutive_below_ma20(kline_bars, ma20, n_days=5)

    result = {
        "a_condition": {"triggered": False, "level": "Not Triggered", "detail": ""},
        "b_condition": {"triggered": False, "level": "Not Triggered", "detail": "",
                        "slope_pct": ma20_slope, "slope_tier_raw": "",
                        "volume_category": "Normal",
                        "consecutive_below": consecutive_info},
        "panic_modifier": {"triggered": False, "ww_condition": False, "csi300_condition": False},
        "macd_divergence": "Data missing -- bridge JSON lacks MACD DIF",
        "action": "Hold",
        "reasoning": ""
    }

    # -- A Condition: Framework Layer --
    l3_judgement = step_d.get("L3_判定", "Not Listed")
    l3_reason = step_d.get("L3_来源", "")

    # Strong A keywords: clear industry cycle end
    strong_a_keywords = ["清仓", "衰退", "过热", "顶部", "淘汰"]
    weak_a_keywords = ["减仓", "边际走弱", "估值透支", "谨慎", "WARN"]

    a_triggered = False
    a_level = "Not Triggered"
    a_detail = ""

    if any(kw in l3_judgement for kw in strong_a_keywords):
        a_triggered = True
        a_level = "Strong A"
        a_detail = f"L3={l3_judgement} -> {l3_reason}"
    elif any(kw in l3_judgement for kw in weak_a_keywords):
        a_triggered = True
        a_level = "Weak A"
        a_detail = f"L3={l3_judgement} -> {l3_reason}"
    else:
        a_detail = f"L3={l3_judgement}, A condition not triggered"

    result["a_condition"] = {
        "triggered": a_triggered, "level": a_level, "detail": a_detail
    }

    # -- B Condition: Technical Layer --
    if close is None or ma20 is None or ma20 == 0:
        result["b_condition"]["detail"] = "Insufficient data (close or MA20 missing)"
        result["action"] = "No Data"
        return result

    price_below_ma20 = close < ma20
    slope_negative = ma20_slope < 0

    # Volume classification
    if vol_ratio and vol_ratio < 0.8:
        vol_cat = "Shrinking"
    elif vol_ratio and vol_ratio > 1.5:
        vol_cat = "Expanding"
    else:
        vol_cat = "Normal"

    result["b_condition"]["volume_category"] = vol_cat

    # Deviation vs amplitude auxiliary
    if price_below_ma20:
        break_pct = abs(deviation)
        if atr_pct and atr_pct > 10:
            if break_pct < 3:
                result["b_condition"]["detail"] += f"Break {break_pct:.1f}% but daily amp {atr_pct:.1f}% -> may be noise; "
            else:
                result["b_condition"]["detail"] += f"Break {break_pct:.1f}% + high amp {atr_pct:.1f}% -> warning; "

    # v1.1: Weak B slope 4-tier -- unified to v3.3 raw format
    # v3.3 raw -> pct equivalent: >=1.0 ~>=3%, 0.5~1.0 ~1.5~3%, 0.1~0.5 ~0.3~1.5%, <0.1 ~<0.3%
    slope_abs_pct = abs(ma20_slope)
    slope_raw = slope_abs_pct / 3.0

    if slope_raw >= 1.0:
        slope_tier_raw = ">=1.0 Strong Rise"
        slope_tier_pct = f"~>={3.0:.0f}%"
    elif slope_raw >= 0.5:
        slope_tier_raw = "0.5~1.0 Medium Rise"
        slope_tier_pct = f"~{1.5:.0f}%~{3.0:.0f}%"
    elif slope_raw >= 0.1:
        slope_tier_raw = "0.1~0.5 Weak Rise"
        slope_tier_pct = f"~{0.3:.0f}%~{1.5:.0f}%"
    else:
        slope_tier_raw = "<0.1 Very Weak"
        slope_tier_pct = f"~<{0.3:.0f}%"

    result["b_condition"]["slope_tier_raw"] = slope_tier_raw

    # Strong B vs Weak B vs No Trigger
    if price_below_ma20 and slope_negative:
        # Strong B: price below MA20 + MA20 turning down
        if consecutive_info["should_wait"]:
            result["b_condition"]["level"] = "Pending"
            result["b_condition"]["detail"] += (
                f"Close {close:.2f} < MA20 {ma20:.2f} (dev {deviation:.1f}%) | "
                f"MA20 declining (slope {ma20_slope:.2f}%) | "
                f"{consecutive_info['detail']}"
            )
        else:
            result["b_condition"]["triggered"] = True
            result["b_condition"]["level"] = "Strong B"
            result["b_condition"]["detail"] += (
                f"Close {close:.2f} < MA20 {ma20:.2f} (dev {deviation:.1f}%) | "
                f"MA20 declining (slope {ma20_slope:.2f}%) | "
                f"{consecutive_info['detail']}"
            )

    elif price_below_ma20 and not slope_negative:
        # Weak B: price below MA20 + MA20 still rising
        if consecutive_info["should_wait"]:
            result["b_condition"]["level"] = "Pending"
            result["b_condition"]["detail"] += (
                f"Close {close:.2f} < MA20 {ma20:.2f} (dev {deviation:.1f}%) | "
                f"MA20 still rising (slope +{ma20_slope:.2f}%) -> {slope_tier_raw}({slope_tier_pct}) | "
                f"Vol={vol_cat} | "
                f"{consecutive_info['detail']}"
            )
        else:
            result["b_condition"]["triggered"] = True
            result["b_condition"]["level"] = "Weak B"
            result["b_condition"]["detail"] += (
                f"Close {close:.2f} < MA20 {ma20:.2f} (dev {deviation:.1f}%) | "
                f"MA20 still rising (slope +{ma20_slope:.2f}%) -> {slope_tier_raw}({slope_tier_pct}) | "
                f"Vol={vol_cat} | "
                f"{consecutive_info['detail']}"
            )
    else:
        result["b_condition"]["detail"] = (
            f"Close {close:.2f} {'>=' if not price_below_ma20 else '<'} MA20 {ma20:.2f}"
            f" (dev {deviation:+.1f}%) | MA20 slope={ma20_slope:+.2f}%"
        )

    # -- Panic Modifier v1.1: dual condition --
    ww = step_a.get("ww")
    ww_panic = isinstance(ww, (int, float)) and ww < 20

    csi300_panic = False
    if csi300_data:
        csi300_panic = abs(csi300_data.get("chg_pct", 0)) > 3.0

    panic = ww_panic or csi300_panic
    result["panic_modifier"] = {
        "triggered": panic,
        "ww_condition": ww_panic,
        "ww_value": ww if isinstance(ww, (int, float)) else None,
        "csi300_condition": csi300_panic,
        "csi300_chg_pct": csi300_data.get("chg_pct") if csi300_data else None,
        "csi300_note": "" if csi300_data else "CSI300 data fetch failed, using W&W only"
    }

    # -- Unified Decision --
    reasoning_parts = []

    # If B is "Pending" (insufficient consecutive days), handle first
    if result["b_condition"]["level"] == "Pending":
        result["action"] = "Wait Confirm"
        reasoning_parts.append(f"B condition pending: {consecutive_info['detail']}")
        reasoning_parts.append("Wait next closing day to confirm -- v3.3 false breakdown rule")
        result["reasoning"] = " | ".join(reasoning_parts)
        return result

    # Case 1: Strong A -> liquidate
    if a_level == "Strong A":
        result["action"] = "Liquidate All"
        reasoning_parts.append("Strong A: industry cycle ended/sentiment turning down -> any bounce is a sell")
        reasoning_parts.append("Exception: if residual momentum A/B grade -> halve first (needs L4 data)")

    # Case 2: Weak A
    elif a_level == "Weak A":
        if result["b_condition"]["triggered"]:
            result["action"] = "Reduce 50%"
            reasoning_parts.append(f"Weak A + B triggered ({result['b_condition']['level']}) -> reduce to 50% normal")
        else:
            result["action"] = "Reduce Observe"
            reasoning_parts.append("Weak A but B not triggered -> reduce to 50%, keep core to observe")

    # Case 3: A not triggered + B triggered
    elif result["b_condition"]["triggered"]:
        b_level = result["b_condition"]["level"]

        if b_level == "Strong B":
            if panic:
                result["action"] = "Execute Unchanged"
                reasoning_parts.append("Strong B + Panic -> MA20 already declining before panic, trend is bad -> no change")
                reasoning_parts.append("1 day buffer: if bounce to MA20 +/-2% and stalls -> clear; no bounce -> clear same day")
            else:
                result["action"] = "Liquidate All"
                reasoning_parts.append("Strong B: price broken MA20 + MA20 declining -> trend broken -> liquidate all")

        else:  # Weak B
            if panic:
                result["action"] = "Observe"
                panic_detail = []
                if ww_panic:
                    panic_detail.append(f"WW={ww}<20")
                if csi300_panic:
                    panic_detail.append(f"CSI300 drop {csi300_data['chg_pct']:.1f}% >3%")
                reasoning_parts.append(f"Weak B + Panic ({' + '.join(panic_detail)}) -> downgrade to observe, wait next close")
                reasoning_parts.append("Principle: panic day = undifferentiated selling, MA20 break likely beta-driven")

            elif slope_raw >= 1.0:
                result["action"] = "No Action"
                reasoning_parts.append(f"Weak B + {slope_tier_raw} -> historically 93% recovery rate, +3.9% 5d return -> selling likely wrong")

            elif slope_raw >= 0.5:
                if vol_cat == "Shrinking":
                    result["action"] = "Reduce 12.5%"
                    reasoning_parts.append(f"Weak B + {slope_tier_raw} + shrinking vol -> 25% halved")
                else:
                    result["action"] = "Reduce 25%"
                    reasoning_parts.append(f"Weak B + {slope_tier_raw} -> reduce 25%")

            elif slope_raw >= 0.1:
                if vol_cat == "Shrinking":
                    result["action"] = "Reduce 25%"
                    reasoning_parts.append(f"Weak B + {slope_tier_raw} + shrinking vol -> 50% halved")
                else:
                    result["action"] = "Reduce 50%"
                    reasoning_parts.append(f"Weak B + {slope_tier_raw} -> reduce 50%")

            else:  # < 0.1
                result["action"] = "Liquidate All"
                reasoning_parts.append(f"Weak B + {slope_tier_raw} -> MA20 flips in ~2 days -> treat as Strong B")

            reasoning_parts.append("Position type fine-tune: strategic hold may conservatively reduce; cycle reversal strictly execute; Tier2 always liquidate on Strong B")

    # Case 4: Neither triggered
    else:
        result["action"] = "Hold"
        reasoning_parts.append("A+B both not triggered -> normal hold")

    # Volume correction
    if result["b_condition"]["triggered"] and vol_cat == "Expanding":
        reasoning_parts.append("Expanding vol breakdown -> capital exit confirmed, execute as planned")
    elif result["b_condition"]["triggered"] and vol_cat == "Shrinking" and result["action"] not in ("Observe", "No Action"):
        reasoning_parts.append("Shrinking vol breakdown -> false breakdown, downgraded")

    result["reasoning"] = " | ".join(reasoning_parts)
    return result


# ============================================================
# Step 2: Tier 2 Offensive Position Check
# ============================================================

def step2_tier2_check(bridge, current_position_pct=0, cost_basis=None):
    """Check Tier 2 six conditions. Note: [0][1][2][4] need cross-position data."""
    daily = bridge["daily"]

    close = safe_float(daily.get("close"))
    ma20 = safe_float(daily.get("MA20"))
    ma20_slope = safe_float(daily.get("MA20_slope_pct"), 0)
    atr_pct = safe_float(daily.get("ATR_pct"), 3.0)

    conditions = []

    # [0] All positions' core float profit > 10%
    if cost_basis and close:
        pnl_pct = (close - cost_basis) / cost_basis * 100
        cond0_ok = pnl_pct > 10
        conditions.append({
            "id": 0, "label": "Core float >10%",
            "value": f"Float {pnl_pct:+.1f}%",
            "ok": cond0_ok,
            "note": "Single stock check -- cross-position need all satisfied"
        })
    else:
        conditions.append({
            "id": 0, "label": "Core float >10%",
            "value": "No cost provided",
            "ok": None,
            "note": "Need --cost parameter"
        })

    # [1] All positions close > MA20
    cond1_ok = close and ma20 and close > ma20
    conditions.append({
        "id": 1, "label": "Close > MA20",
        "value": f"close={close} vs MA20={ma20}",
        "ok": cond1_ok,
        "note": "Single stock check"
    })

    # [2] All positions MA20 5d slope > 0
    cond2_ok = ma20_slope > 0
    conditions.append({
        "id": 2, "label": "MA20 5d slope > 0",
        "value": f"Slope={ma20_slope:+.2f}%",
        "ok": cond2_ok,
        "note": "Single stock check"
    })

    # [3] Shanghai Composite MA60 10d slope > 0
    conditions.append({
        "id": 3, "label": "SH MA60 10d slope > 0",
        "value": "Need independent SH index fetch",
        "ok": None,
        "note": "Bridge JSON lacks broad-market MA60 data"
    })

    # [4] >= 2 positions' BK sector index MA20 slope > 0
    conditions.append({
        "id": 4, "label": ">=2 positions BK sector MA20 slope > 0",
        "value": "Need cross-position + sector data",
        "ok": None,
        "note": "Single stock mode cannot check"
    })

    # [5] 20d average amplitude < 5.5%
    cond5_ok = atr_pct and atr_pct < 5.5
    conditions.append({
        "id": 5, "label": "20d avg amp < 5.5% (CVaR normal)",
        "value": f"Avg amp={atr_pct:.1f}%",
        "ok": cond5_ok,
        "note": ""
    })

    # Synthesize
    known_oks = [c["ok"] for c in conditions if c["ok"] is not None]

    if all(c["ok"] is not False and c["ok"] is not None for c in conditions[:3]):
        if cond5_ok:
            tier2_action = "Full trigger (+10-15%)"
        else:
            tier2_action = "Half trigger (+5-8%) -- condition 5 (CVaR) not met"
    elif conditions[0]["ok"] is False:
        tier2_action = "Not triggered -- condition 0 failed (core float <10%)"
    elif any(c["ok"] is False for c in conditions[1:5]):
        tier2_action = "Not triggered -- conditions 1-4 have failures"
    else:
        tier2_action = "Pending -- unconfirmed conditions exist"

    return {
        "conditions": conditions,
        "action": tier2_action,
        "ceiling_from_l2": safe_float(bridge["step_e"].get("ceiling_pct"), 50),
        "note": "Tier 2 is not standalone quota, it's the gap from core position to L2 total ceiling"
    }


# ============================================================
# Step 3: Batch Entry Plan
# ============================================================

def step3_batch_entry(bridge, holding_type="Big Dark Horse", planned_position_pct=15):
    """Output batch entry plan based on position type."""
    daily = bridge["daily"]
    close = safe_float(daily.get("close"))
    ma20 = safe_float(daily.get("MA20"))
    ma20_slope = safe_float(daily.get("MA20_slope_pct"), 0)
    ma30 = safe_float(daily.get("MA30"))
    ma60 = safe_float(daily.get("MA60"))
    vol_ratio = safe_float(daily.get("volume_ratio_20d"), 1.0)

    plan = {
        "holding_type": holding_type,
        "planned_position_pct": planned_position_pct,
        "current_status": {},
        "batches": []
    }

    ma_alignment_ok = (ma20 and ma30 and ma60 and ma20 > ma30 > ma60) if all([ma20, ma30, ma60]) else False

    status_parts = []
    if close and ma20:
        status_parts.append(f"{'Above' if close > ma20 else 'Below'} MA20")
    if ma20_slope:
        status_parts.append(f"MA20 {'Rising' if ma20_slope > 0 else 'Declining'}")
    if ma_alignment_ok:
        status_parts.append("Bull Alignment")
    plan["current_status"] = {
        "close": close, "ma20": ma20, "ma20_slope": ma20_slope,
        "ma_alignment": "Bull Alignment" if ma_alignment_ok else "Not Bull Alignment",
        "vol_ratio": vol_ratio,
        "summary": " | ".join(status_parts)
    }

    if holding_type == "Big Dark Horse":
        plan["plan_total_pct"] = min(planned_position_pct, 15)
        plan["batches"] = [
            {
                "name": "A Probe", "pct": round(plan["plan_total_pct"] * 0.40, 1),
                "trigger": "MA20 flattening/turning up + 2 consecutive days close above MA20 + vol > 20d avg",
                "stop": "MA20",
                "logic": "Multiple confirm filters false breakout",
                "ready": bool(close and ma20 and close > ma20 and ma20_slope > 0 and vol_ratio and vol_ratio > 1.0)
            },
            {
                "name": "B Confirm", "pct": round(plan["plan_total_pct"] * 0.30, 1),
                "trigger": "(a) Pullback to MA20 +/-3% bounce with vol OR (b) +5% from A entry + held MA20 5 days",
                "stop": "Breakeven (A+B avg)",
                "logic": "Confirm trend is real",
                "ready": False,
                "ready_note": "Wait for A to complete"
            },
            {
                "name": "C Add-on", "pct": round(plan["plan_total_pct"] * 0.30, 1),
                "trigger": "MA10>MA20>MA60 full bull alignment + new 20d high",
                "stop": "MA10 (C portion) + MA20 (A+B portion)",
                "logic": "Trend acceleration confirmed",
                "ready": False,
                "ready_note": "Wait for bull alignment + new high"
            }
        ]
        plan["rules"] = [
            "Never add to losing positions",
            "A stopped out -> wait for next MA20 reclaim signal",
            "False breakout: can re-probe A within 2 days, but no B/C chasing",
            "At least 3 trading days between A and B"
        ]
    else:  # Cycle Reversal
        plan["plan_total_pct"] = min(planned_position_pct, 5)
        plan["batches"] = [
            {
                "name": "A Probe", "pct": round(plan["plan_total_pct"] * 0.60, 1),
                "trigger": "MA20 reclaimed (close confirm) + vol > 20d avg 1.5x + clear catalyst",
                "stop": "MA20",
                "logic": "Bottom inflection needs vol + catalyst dual confirm",
                "ready": bool(close and ma20 and close > ma20 and vol_ratio and vol_ratio > 1.5)
            },
            {
                "name": "B Confirm", "pct": round(plan["plan_total_pct"] * 0.40, 1),
                "trigger": "MA20 held for 5 trading days + MA20 turning up",
                "stop": "Breakeven (B portion) + MA20 (A portion)",
                "logic": "5-day hold filters false inflection",
                "ready": False,
                "ready_note": "Wait for A to complete, observe 5 days"
            }
        ]
        plan["rules"] = [
            "5% too small for 3 batches",
            "Cycle reversal: tight stop, small tolerance",
            "Catalyst must have clear fundamental logic"
        ]

    return plan


# ============================================================
# Step 5: Trailing Stop
# ============================================================

def step5_trailing_stop(bridge, cost_basis=None, high_since_entry=None):
    """Trailing stop with gradient tightening. Triggers when float >= +20%."""
    daily = bridge["daily"]
    close = safe_float(daily.get("close"))
    ma20 = safe_float(daily.get("MA20"))
    atr_pct = safe_float(daily.get("ATR_pct"), 3.0)
    chg_10d = safe_float(daily.get("chg_5d_pct"), 0)  # bridge has no 10d, use 5d approx

    result = {
        "executed": False,
        "pnl_pct": None,
        "stop_line": None,
        "stop_method": "",
        "acceleration_warn": False
    }

    if not cost_basis or not close:
        result["note"] = "Need cost basis to calculate trailing stop"
        return result

    pnl_pct = (close - cost_basis) / cost_basis * 100
    result["pnl_pct"] = round(pnl_pct, 1)

    if pnl_pct < 20:
        result["executed"] = False
        result["note"] = f"Float {pnl_pct:.1f}% < 20% -> trailing stop not triggered, use MA20 stop"
        result["stop_line"] = ma20
        result["stop_method"] = "MA20"
        return result

    result["executed"] = True

    if high_since_entry is None:
        high_since_entry = max(close, cost_basis)

    if pnl_pct <= 50:
        multiplier = 1.5
        tier = "20-50%"
    elif pnl_pct <= 100:
        multiplier = 1.0
        tier = "50-100%"
    else:
        multiplier = 0.8
        tier = ">100%"

    atr_stop = high_since_entry * (1 - multiplier * atr_pct / 100)
    stop_line = max(ma20, atr_stop) if ma20 else atr_stop

    result["stop_line"] = round(stop_line, 2)
    result["stop_method"] = f"max(MA20, High x [1-{multiplier} x ATR%]) -- tier {tier}"
    result["tier"] = tier
    result["high_since_entry"] = round(high_since_entry, 2)

    if chg_10d and chg_10d > 30:
        result["acceleration_warn"] = True
        result["acceleration_action"] = "10d gain >30% -> sentiment blow-off, active reduce 20-30%, don't wait for stop"

    return result


# ============================================================
# Step 6: CVaR Downside Risk Scaling
# ============================================================

def step6_cvar_scale(bridge):
    """CVaR/amplitude scaling."""
    daily = bridge["daily"]
    fin = bridge["fin"]
    step_e = bridge["step_e"]

    atr_pct = safe_float(daily.get("ATR_pct"), 3.0)
    roe = safe_float(fin.get("ROE"))

    result = {
        "daily_amp_pct": atr_pct,
        "amp_state": "",
        "amp_scale_factor": 1.0,
        "roe_anchor": "",
        "adjusted_ceiling": 50
    }

    # Amplitude state (thresholds from A-share distribution: median 3.4%, 90%ile 6.1%)
    if atr_pct >= 12:
        result["amp_state"] = "Extreme"
        result["amp_scale_factor"] = 0.33
    elif atr_pct >= 8:
        result["amp_state"] = "Violent"
        result["amp_scale_factor"] = 0.45
    elif atr_pct >= 5.5:
        result["amp_state"] = "Bumpy"
        result["amp_scale_factor"] = 0.70
    elif atr_pct >= 4:
        result["amp_state"] = "Normal"
        result["amp_scale_factor"] = 1.0
    else:
        result["amp_state"] = "Calm"
        result["amp_scale_factor"] = 1.0

    amp_factor = 4.0 / max(4.0, atr_pct)
    result["amp_scale_factor_raw"] = round(amp_factor, 3)

    # ROE anchor
    if roe is not None:
        roe_high = roe > 15
        roe_low = roe < 5
        pe_high = False  # bridge lacks PE percentile

        if pe_high and roe_high:
            result["roe_anchor"] = "High PE + High ROE -> expensive but justified, no extra discount"
        elif pe_high and roe_low:
            result["roe_anchor"] = "High PE + Low ROE -> expensive without reason, CVaR extra -0.5 tier"
            result["amp_scale_factor"] *= 0.7
        elif not pe_high and roe_low:
            result["roe_anchor"] = "Low PE + Low ROE -> cheap for a reason, don't add"
        elif not pe_high and roe_high:
            result["roe_anchor"] = "Low PE + High ROE -> possibly mispriced, ideal"
        else:
            result["roe_anchor"] = f"ROE={roe:.1f}% -- PE percentile missing, skip ROE anchor"
    else:
        result["roe_anchor"] = "ROE data missing"

    l2_ceiling = safe_float(step_e.get("ceiling_pct"), 50)
    result["l2_ceiling"] = l2_ceiling
    result["adjusted_ceiling"] = round(l2_ceiling * result["amp_scale_factor"], 1)

    result["vol_energy"] = {
        "20d Avg Amp": f"{atr_pct:.1f}%",
        "Tail Risk State": "High Vol" if atr_pct > 5.5 else "Normal",
        "Position Factor": f"{result['amp_scale_factor']:.2f}x",
        "Adjusted Stock Ceiling": f"{result['adjusted_ceiling']:.1f}%"
    }

    return result


# ============================================================
# Composite action_signal Generation
# ============================================================

def generate_action_signal(bridge, cost=None, position=None, holding_type="Big Dark Horse",
                          kline_bars=None, csi300_data=None):
    """Aggregate all L5 Steps, output final action_signal. v1.1: +daily bars +CSI300."""

    # Step 0
    precheck = step0_precheck(bridge, kline_bars)

    # Step 1 (v1.1: +consecutive days +CSI300 panic)
    sell = step1_sell_dual_confirm(bridge, kline_bars, csi300_data)

    # Step 2
    tier2 = step2_tier2_check(bridge, position or 0, cost)

    # Step 3
    entry_plan = step3_batch_entry(bridge, holding_type, position or 15)

    # Step 5
    high = safe_float(bridge["daily"].get("close")) if cost else None
    trailing = step5_trailing_stop(bridge, cost, high)

    # Step 6
    cvar = step6_cvar_scale(bridge)

    # -- Direction synthesis --
    daily = bridge["daily"]
    close = safe_float(daily.get("close"))
    ma20 = safe_float(daily.get("MA20"))
    ma20_slope = safe_float(daily.get("MA20_slope_pct"), 0)

    # Direction logic (v1.1: +Wait Confirm)
    if sell["action"] in ("Liquidate All", "Reduce 50%"):
        direction = "reduce"
    elif sell["action"] in ("Reduce 25%", "Reduce 12.5%"):
        direction = "trim"
    elif sell["action"] == "Wait Confirm":
        direction = "wait_confirm"
    elif tier2["action"].startswith("Full trigger"):
        direction = "add"
    elif close and ma20 and close > ma20 and ma20_slope > 0:
        direction = "hold_long"
    elif close and ma20 and close < ma20 and ma20_slope < 0:
        direction = "hold_short"
    else:
        direction = "hold_neutral"

    # Suggested position
    if direction == "reduce":
        suggested_pct = 0
    elif direction == "trim":
        suggested_pct = max(0, (position or 0) * 0.5)
    elif direction == "wait_confirm":
        suggested_pct = position
    elif direction == "add":
        suggested_pct = min((position or 0) + 15, cvar["adjusted_ceiling"])
    else:
        suggested_pct = position

    # Entry zone
    if close and ma20 and ma20 > 0:
        entry_low = round(ma20 * 0.97, 2)
        entry_high = round(ma20 * 1.03, 2)
    else:
        entry_low = entry_high = None

    # Stop line
    if trailing.get("executed"):
        stop_loss = trailing["stop_line"]
    else:
        stop_loss = ma20

    # Conditions list
    conditions = []
    if sell["action"] == "Wait Confirm":
        conditions.append(f"B condition pending: {sell['b_condition'].get('consecutive_below', {}).get('detail', 'insufficient consecutive days')}")
    elif sell["action"] not in ("Hold", "No Data"):
        conditions.append(f"Sell trigger: {sell['action']} -- {sell['reasoning'][:80]}")
    if tier2["action"].startswith("Full") or tier2["action"].startswith("Half"):
        conditions.append(f"Tier2: {tier2['action']}")
    if cvar["amp_state"] in ("Bumpy", "Violent", "Extreme"):
        conditions.append(f"CVaR: {cvar['amp_state']} -> position factor {cvar['amp_scale_factor']:.2f}x")
    if trailing.get("acceleration_warn"):
        conditions.append(f"Blow-off: {trailing.get('acceleration_action', '')}")

    if not conditions:
        conditions.append("No special conditions -- normal hold")

    action_signal = {
        "_meta": {
            "code": bridge["code"],
            "name": bridge["name"],
            "date": TODAY,
            "engine_version": "1.1",
            "generated_by": "rule_engine.py (L5)"
        },
        "action_signal": {
            "direction": direction,
            "confidence": None,
            "suggested_position_pct": suggested_pct,
            "entry_zone": [entry_low, entry_high] if entry_low else None,
            "stop_loss": round(stop_loss, 2) if stop_loss else None,
            "conditions": conditions
        },
        "detailed_diagnosis": {
            "step0_precheck": precheck,
            "step1_sell": {k: v for k, v in sell.items() if k != "reasoning"},
            "step1_reasoning": sell["reasoning"],
            "step2_tier2": tier2,
            "step3_entry_plan": entry_plan,
            "step5_trailing_stop": trailing,
            "step6_cvar_scale": cvar
        }
    }

    return action_signal


# ============================================================
# Terminal Diagnosis Report
# ============================================================

def print_report(signal):
    """Terminal-friendly diagnosis report"""
    meta = signal["_meta"]
    a = signal["action_signal"]
    d = signal["detailed_diagnosis"]

    print()
    print("=" * 64)
    print(f"  L5 Trading Execution Engine -- Position Diagnosis")
    print(f"  {meta['name']} ({meta['code']})  |  {meta['date']}")
    print("=" * 64)

    # Market snapshot
    sell_data = d["step1_sell"]
    cvar_data = d["step6_cvar_scale"]

    print(f"\n  [Market Snapshot]")
    print(f"  Direction: {a['direction']}")
    print(f"  Suggested Position: {a['suggested_position_pct']}%")
    if a["entry_zone"]:
        print(f"  Entry Zone: {a['entry_zone'][0]} - {a['entry_zone'][1]}")
    if a["stop_loss"]:
        print(f"  Stop Loss: {a['stop_loss']}")
    print(f"  Amp State: {cvar_data.get('amp_state', '?')} ({cvar_data.get('daily_amp_pct', '?')}%)")
    print(f"  CVaR Ceiling: {cvar_data.get('adjusted_ceiling', '?')}%")

    # A+B Dual Confirm
    print(f"\n  [Step 1: Sell Dual-Confirm]")
    print(f"  A (Framework): {sell_data['a_condition']['level']} -- {sell_data['a_condition']['detail'][:80]}")

    # v1.1: B condition with consecutive days + slope tier
    b = sell_data['b_condition']
    consec = b.get('consecutive_below', {})
    print(f"  B (Technical): {b['level']} | Slope Tier={b.get('slope_tier_raw', '?')} | Vol={b['volume_category']}")
    if consec.get('detail'):
        print(f"  Consecutive Days: {consec['detail']}")

    # v1.1: Panic modifier dual condition
    panic = sell_data.get('panic_modifier', {})
    if isinstance(panic, dict):
        ww_str = f"WW={panic.get('ww_value')}" if panic.get('ww_value') is not None else "WW=?"
        csi_str = f"CSI300={panic.get('csi300_chg_pct'):+.1f}%" if panic.get('csi300_chg_pct') is not None else "CSI300=?"
        print(f"  Panic: {'TRIGGERED' if panic.get('triggered') else 'Not triggered'} (Cond1: {ww_str} {'Y' if panic.get('ww_condition') else 'N'} | Cond2: {csi_str} {'Y' if panic.get('csi300_condition') else 'N'})")
        if panic.get('csi300_note'):
            print(f"  CSI300 Note: {panic['csi300_note']}")
    else:
        print(f"  Panic: {'TRIGGERED' if panic else 'Not triggered'}")

    print(f"  Decision: {sell_data['action']}")
    print(f"  Reasoning: {d['step1_reasoning'][:120]}")

    # Tier 2
    print(f"\n  [Step 2: Tier 2 Offensive]")
    tier2 = d["step2_tier2"]
    print(f"  Decision: {tier2['action']}")
    for c in tier2["conditions"]:
        icon = "OK" if c["ok"] is True else ("NO" if c["ok"] is False else "??")
        print(f"  [{icon}] [{c['id']}] {c['label']}: {c['value']}")

    # Batch Entry
    print(f"\n  [Step 3: Batch Entry Plan ({d['step3_entry_plan']['holding_type']})]")
    curr = d['step3_entry_plan']['current_status']
    print(f"  Current: {curr['summary']}")
    for b in d['step3_entry_plan']['batches']:
        ready_mark = "READY" if b.get('ready') else "wait"
        note = f" -- {b.get('ready_note')}" if b.get('ready_note') else ""
        print(f"  [{ready_mark}] {b['name']}: {b['pct']}% | {b['trigger'][:60]}{note}")
        print(f"     Stop: {b['stop']}")

    # Trailing Stop
    print(f"\n  [Step 5: Trailing Stop]")
    ts = d["step5_trailing_stop"]
    if ts.get("executed"):
        print(f"  Float: {ts['pnl_pct']:.1f}% -> Trailing stop ACTIVE ({ts.get('tier', '?')})")
        print(f"  Stop Line: {ts['stop_line']} ({ts['stop_method'][:60]})")
        if ts.get("acceleration_warn"):
            print(f"  [WARN] {ts.get('acceleration_action', '')}")
    elif ts.get("pnl_pct") is not None:
        print(f"  Float: {ts['pnl_pct']:.1f}% -> Trailing stop not triggered ({ts.get('note', '')})")
    else:
        print(f"  No cost data, skip trailing stop")

    # CVaR
    print(f"\n  [Step 6: CVaR Scaling]")
    ve = cvar_data.get("vol_energy", {})
    for k, v in ve.items():
        print(f"  {k}: {v}")
    if cvar_data.get("roe_anchor"):
        print(f"  ROE Anchor: {cvar_data['roe_anchor']}")

    # Conditions
    print(f"\n  [Current Conditions]")
    for i, cond in enumerate(a["conditions"], 1):
        print(f"  {i}. {cond}")

    print()
    print("=" * 64)
    print()


# ============================================================
# Main Entry Point
# ============================================================

def main():
    if len(sys.argv) < 2:
        print("Usage: python rule_engine.py <code> [--cost=<cost>] [--position=<pct>] [--holding-type=<type>]")
        print()
        print("Examples:")
        print("  python rule_engine.py 688008")
        print("  python rule_engine.py 688008 --cost=254.04 --position=20 --holding-type=Big Dark Horse")
        print()
        print("Holdings config:", HOLDINGS_PATH)
        print("Trigger keywords (say to Claude):")
        print('  "L5 688008"      -> run rule engine')
        print('  "chu xinhao 688008" -> run rule engine')
        sys.exit(1)

    code = sys.argv[1]
    code_6 = code.strip().replace(".SZ","").replace(".SH","").replace(".BJ","").zfill(6)

    # Load holdings config
    holdings = load_holdings()
    h = holdings.get(code_6, {})

    # Parse optional args (CLI priority over config file)
    cost = None
    position = None
    holding_type = h.get("type", "Big Dark Horse")

    for arg in sys.argv[2:]:
        if arg.startswith("--cost="):
            cost = safe_float(arg.split("=", 1)[1])
        elif arg.startswith("--position="):
            position = safe_float(arg.split("=", 1)[1])
        elif arg.startswith("--holding-type="):
            holding_type = arg.split("=", 1)[1]

    # CLI not specified -> read from config
    if cost is None:
        cost = safe_float(h.get("cost"))
    if position is None:
        position = safe_float(h.get("position_pct"))

    # Not in holdings -> quietly skip position-related steps
    # (no warning, no nagging)

    # Load bridge
    bridge = load_bridge(code_6)

    # Override name from holdings.json (manually confirmed)
    if h.get("name"):
        bridge["name"] = h["name"]

    # v1.1: Fetch daily K-line + CSI300 (for consecutive days + panic modifier)
    sina_sym = code_to_sina_symbol(code_6)
    print(f"[Fetch] Daily K-line ({sina_sym}) ...")
    kline_bars, kline_err = fetch_daily_kline(sina_sym, 30)
    if kline_err:
        print(f"        [WARN] K-line fetch failed: {kline_err} -- consecutive day check degraded")

    print(f"[Fetch] CSI300 ...")
    csi300_data = fetch_csi300_daily()
    if csi300_data:
        print(f"        CSI300 today: {csi300_data['chg_pct']:+.2f}%")
    else:
        print(f"        [WARN] CSI300 fetch failed -- panic modifier using W&W only")

    # Generate signal
    signal = generate_action_signal(bridge, cost, position, holding_type,
                                    kline_bars, csi300_data)

    # Print report
    print_report(signal)

    # Save JSON
    out_path = os.path.join(DESKTOP, f"{bridge['code']}_action_signal.json")
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(signal, f, ensure_ascii=False, indent=2, default=str)
    print(f"[SAVED] -> {out_path}")


if __name__ == "__main__":
    main()
