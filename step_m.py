"""
step_m.py — L2.5 市场水温数据脚本
=================================
填补 L2(宏观象限) 和 L3(赛道周期) 之间的"中场镜头"。
五个维度: 宽度/资金/情绪/量能/轮动。

W&W 管中长期情绪位置("该不该做"), L2.5 管短期市场结构("怎么做、在哪做")。

用法:
  python step_m.py
  python step_m.py --waw_score '{"score":73.2,"zone":"Bearish"}'
  python step_m.py --frequency intraday

输出: JSON to stdout (匹配 l2.5_step_m_schema.json)
"""

import json
import sys
import os
import math
import urllib.request
import io
from datetime import date, datetime, timedelta

# === 强制 UTF-8 stdout (Windows GBK 兼容) ===
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# === 配置 ===
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# SW一级行业 → 申万代码
SW1_MAP = {
    "银行": "801780", "公用事业": "801160", "煤炭": "801950",
    "交通运输": "801170", "石油石化": "801960", "电子": "801080",
    "计算机": "801750", "传媒": "801760", "通信": "801770",
    "国防军工": "801740", "房地产": "801180", "食品饮料": "801120",
    "医药生物": "801150", "非银金融": "801790", "汽车": "801880",
    "机械设备": "801890", "纺织服装": "801130", "轻工制造": "801140",
    "商业贸易": "801200", "休闲服务": "801210", "综合": "801230",
    "建筑材料": "801710", "建筑装饰": "801720", "电气设备": "801730",
    "家用电器": "801110", "采掘": "801020", "化工": "801030",
    "钢铁": "801040", "有色金属": "801050", "农林牧渔": "801060",
    "建筑": "801070"
}

DEFENSE_SECTORS = {"银行", "公用事业", "煤炭", "交通运输", "石油石化",
                   "钢铁", "建筑装饰", "建筑材料", "房地产", "建筑",
                   "商业贸易", "休闲服务", "纺织服装", "综合", "农林牧渔",
                   "家用电器", "食品饮料", "医药生物", "轻工制造", "采掘",
                   "非银金融"}
GROWTH_SECTORS  = {"电子", "计算机", "通信", "国防军工", "电气设备",
                   "汽车", "传媒", "机械设备"}


def http_get(url, timeout=15):
    """带 UA 的 HTTP GET, 返回 (text, error)"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://finance.sina.com.cn/"
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace"), None
    except Exception as e:
        return None, str(e)


def is_trading_day():
    """15分钟内快速判断今天是不是交易日"""
    now = datetime.now()
    # 周末直接返回 False
    if now.weekday() >= 5:
        return False
    # 盘前时段(早于9:25)和收盘后(晚于15:05)不做实时判断, 默认True
    # 用最近3天腾讯上证指数数据检查是否有今天日期
    try:
        url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/data/"
               "CN_MarketDataService.getKLineData?symbol=sh000001&scale=240&ma=no&datalen=3")
        raw, err = http_get(url)
        if err:
            return True  # 数据源不可用时不阻断, 默认尝试拉
        start = raw.find("[")
        end = raw.rfind("]")
        if start == -1 or end == -1:
            return True
        bars = json.loads(raw[start:end+1])
        if not bars:
            return True
        last_bar_date = bars[-1].get("day", "")
        today_str = now.strftime("%Y-%m-%d")
        # 最近K线日期>=今天 → 今天有交易数据 → 交易日
        return last_bar_date >= today_str
    except Exception:
        return True


def pct_rank(values, target):
    """计算 target 在 values 中的分位 (0-100)"""
    if not values or len(values) < 2:
        return None
    count_below = sum(1 for v in values if v < target)
    count_eq = sum(1 for v in values if v == target)
    return round((count_below + 0.5 * count_eq) / len(values) * 100, 1)


def annualize_hv(daily_returns, trading_days=252):
    """年化波动率"""
    if len(daily_returns) < 5:
        return None
    std = math.sqrt(sum((r - sum(daily_returns)/len(daily_returns))**2
                        for r in daily_returns) / (len(daily_returns) - 1))
    return round(std * math.sqrt(trading_days) * 100, 2)


# ==============================================================
# 1. breadth — 市场宽度
# ==============================================================

def compute_breadth():
    """计算全市场涨跌比、新高新低比、MA50站上比例"""
    result = {
        "desc": "全市场有多少股票在涨",
        "advance_decline_ratio_5d": None,
        "advance_decline_note": "上涨家数/下跌家数,5日均",
        "new_high_low_ratio_20d": None,
        "new_high_low_note": "20日新高家数/新低家数",
        "csi300_above_ma50_pct": None,
        "csi300_above_ma50_note": "沪深300成分股中收盘>MA50的比例(%)",
        "verdict": None,
        "verdict_rule": "涨跌比<0.8 + 新高新低比<0.3 + MA50站上比<40% → 偏空",
        "error": None
    }

    errors = []

    # --- 涨跌比: 多个数据源 ---
    try:
        # 源1: 腾讯 qt.gtimg.cn 上证和深证快照 (涨跌家数)
        adv_nums = []
        decl_nums = []
        for code in ["sh000001", "sz399001"]:
            url = f"http://qt.gtimg.cn/q={code}"
            raw, err = http_get(url, timeout=5)
            if err:
                continue
            parts = raw.split("~")
            # 不同腾讯版本字段位置不同，尝试常见位置
            # 新格式: parts[8]=涨家, parts[9]=跌家 (仅部分版本)
            try:
                up = int(parts[8]) if len(parts) > 8 and parts[8].strip() else 0
                dn = int(parts[9]) if len(parts) > 9 and parts[9].strip() else 0
                if up + dn > 0:
                    adv_nums.append(up)
                    decl_nums.append(dn)
            except (ValueError, IndexError):
                pass

        if adv_nums and decl_nums:
            total_up = sum(adv_nums)
            total_dn = sum(decl_nums)
            if total_dn > 0:
                result["advance_decline_ratio_5d"] = round(total_up / total_dn, 2)
            elif total_up > 0:
                result["advance_decline_ratio_5d"] = 2.0  # 全涨
            result["advance_decline_note"] = "上涨家数/下跌家数(当日快照,非5日均)" if adv_nums else result["advance_decline_note"]
    except Exception as e:
        errors.append(f"涨跌比-腾讯:{e}")

    # 源2: fallback — Sina全A涨跌统计 (上证+深证合计)
    if result["advance_decline_ratio_5d"] is None:
        try:
            url = "http://hq.sinajs.cn/list=s_sh000001,s_sz399001"
            raw, err = http_get(url, timeout=5)
            if not err and raw:
                # 尝试解析Sina格式的新高新低字段来反推涨跌
                # Sina格式: var hq_str_s_sh000001="上证指数,3342.68,..."
                # 不需要akshare, 直接用腾讯的新尝试
                pass
        except Exception:
            pass

    # 最终fallback: 沪深300在MA的位置 + 新高新低比互推涨跌比
    # 涨跌比 ≈ 市场位置比 * 2 (粗近似)
    if result["advance_decline_ratio_5d"] is None:
        try:
            # 用新高新低比反推
            url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/data/"
                   "CN_MarketDataService.getKLineData?symbol=sh000001&scale=240&ma=no&datalen=5")
            raw, err = http_get(url, timeout=5)
            if not err:
                start = raw.find("[")
                end = raw.rfind("]")
                bars = json.loads(raw[start:end+1]) if start >= 0 else []
                if len(bars) >= 2:
                    closes = [float(b['close']) for b in bars]
                    chg_1d = (closes[-1] - closes[-2]) / closes[-2] * 100
                    # 从指数涨跌+新高新低近似涨跌比
                    if chg_1d > 0.5:
                        result["advance_decline_ratio_5d"] = 1.5
                    elif chg_1d < -0.5:
                        result["advance_decline_ratio_5d"] = 0.6
                    else:
                        result["advance_decline_ratio_5d"] = 1.0
                    result["advance_decline_note"] = "上涨家数/下跌家数(指数涨跌近似,非精确统计)"
        except Exception as e:
            errors.append(f"涨跌比-fallback:{e}")

    # --- 新高新低比: 用沪深300成分股20日新高/新低 ---
    try:
        # 用Sina沪深300 K线 + 腾讯快照的新高新低字段
        url = "http://qt.gtimg.cn/q=s_sh000001,s_sz399001"
        raw, err = http_get(url, timeout=5)
        if not err and raw:
            # 尝试解析腾讯快照中的新高新低
            high_20d = low_20d = 0
            for line in raw.split("\n"):
                pts = line.split("~")
                # 新高新低字段在parts数组较后位置(约13-15)
                if len(pts) > 14:
                    try:
                        h = int(pts[13]) if pts[13].strip() else 0
                        l = int(pts[14]) if pts[14].strip() else 0
                        high_20d += h
                        low_20d += l
                    except (ValueError, IndexError):
                        pass
            if low_20d > 0 and high_20d + low_20d > 10:
                result["new_high_low_ratio_20d"] = round(high_20d / low_20d, 2)
    except Exception:
        pass

    # fallback: 新高新低不可用, 从指数K线接近程度近似
    if result["new_high_low_ratio_20d"] is None:
        try:
            url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/data/"
                   "CN_MarketDataService.getKLineData?symbol=sh000001&scale=240&ma=no&datalen=30")
            raw, err = http_get(url)
            if not err:
                start = raw.find("[")
                end = raw.rfind("]")
                bars = json.loads(raw[start:end+1]) if start >= 0 else []
                if len(bars) >= 20:
                    closes = [float(b['close']) for b in bars]
                    highs = [float(b.get('high', c)) for b, c in zip(bars, closes)]
                    high_20d = max(highs)
                    # 当前位置接近20日高点的程度
                    proximity = (closes[-1] - min(closes[-20:])) / (high_20d - min(closes[-20:])) if high_20d > min(closes[-20:]) else 0.5
                    # 反转成新高新低比的形式: 1/0.001 ≈ 1000 全在顶部; 0.001 ≈ 全在底部
                    result["new_high_low_ratio_20d"] = round(proximity / (1 - proximity + 0.001), 2)
                    result["new_high_low_note"] += "(指数20日位置比近似)"
        except Exception:
            pass

    # --- CSI300 MA50站上比 ---
    try:
        url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/data/"
               "CN_MarketDataService.getKLineData?symbol=sh000300&scale=240&ma=no&datalen=65")
        raw, err = http_get(url)
        if not err:
            start = raw.find("[")
            end = raw.rfind("]")
            bars = json.loads(raw[start:end+1]) if start >= 0 else []
            if len(bars) >= 51:
                closes = [float(b['close']) for b in bars]
                ma50 = sum(closes[-50:]) / 50
                # 指数在MA50之上 → 用最近5日收盘>MA50的比例
                above_count = sum(1 for i in range(max(0, len(closes)-5), len(closes)) if closes[i] > ma50)
                result["csi300_above_ma50_pct"] = round(above_count / 5 * 100, 1)
                result["csi300_above_ma50_note"] = (
                    "沪深300指数近5日收盘高于MA50的比例(%,指数级别代理,非精确成分股统计)"
                )
    except Exception as e:
        errors.append(f"CSI300 MA50:{e}")

    # --- verdict ---
    if errors and result["advance_decline_ratio_5d"] is None:
        result["error"] = "; ".join(errors)
    else:
        ad = result["advance_decline_ratio_5d"]
        hl = result["new_high_low_ratio_20d"]
        ma = result["csi300_above_ma50_pct"]

        bearish = 0
        bullish = 0
        if ad is not None:
            if ad < 0.8: bearish += 1
            elif ad > 1.2: bullish += 1
        if hl is not None:
            if hl < 0.3: bearish += 1
            elif hl > 3.0: bullish += 1
        if ma is not None:
            if ma < 40: bearish += 1
            elif ma > 60: bullish += 1

        if bearish >= 2:
            result["verdict"] = "偏空"
        elif bullish >= 2:
            result["verdict"] = "偏多"
        else:
            result["verdict"] = "中性"

    return result


# ==============================================================
# 2. flow — 资金流向
# ==============================================================

def compute_flow():
    """北向资金、融资余额变化、主力资金方向"""
    result = {
        "desc": "谁在买、谁在卖",
        "northbound_5d_net": None,
        "northbound_note": "北向资金5日净流入(亿元)",
        "margin_wow_change_pct": None,
        "margin_note": "融资余额周环比变化率(%)",
        "main_force_direction": None,
        "main_force_note": "主力资金(超大单+大单)净方向",
        "verdict": None,
        "verdict_rule": "",
        "error": None
    }
    errors = []

    # --- 北向资金: 尝试多种数据源 ---
    try:
        # 方法1: East Money 资金流向汇总(当日快照) — 拼5天
        import akshare as ak
        try:
            url = ("https://push2.eastmoney.com/api/qt/kamt.kline/get?"
                   "fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54&klt=101&lmt=10&"
                   "ut=fa5fd1943c7b386f172d6893dbfba10b")
            raw, err = http_get(url, timeout=8)
            if not err and raw:
                data = json.loads(raw)
                d = data.get("data", {})
                # 北向总净流入 = hk2sh_net + hk2sz_net
                # 字段: date, daily_buy(万), daily_sell(万), cumulative(万)
                hk2sh = d.get("hk2sh") or d.get("north2sh") or []
                hk2sz = d.get("hk2sz") or d.get("north2sz") or []

                # 尝试用sh2hk/hk2sh计算净流向
                # southbound累计差 → 反推北向
                sh2hk = d.get("sh2hk") or []
                sz2hk = d.get("sz2hk") or []

                # 最可靠的方案: 用累计余额差计算5日净流入
                all_series = hk2sh or hk2sz or sh2hk or sz2hk
                if all_series and len(all_series) >= 6:
                    # 取最后两个有差异的累积值
                    parts_new = all_series[-1].split(",")
                    parts_old = all_series[-6].split(",")
                    if len(parts_new) >= 4 and len(parts_old) >= 4:
                        cum_new = float(parts_new[3])
                        cum_old = float(parts_old[3])
                        net_5d = cum_new - cum_old  # 万元
                        result["northbound_5d_net"] = round(net_5d / 10000, 2)  # 万元→亿元
        except Exception:
            pass

        # 方法2: 如果方法1失败, 用 stock_hsgt_fund_flow_summary_em 今日快照
        if result["northbound_5d_net"] is None:
            try:
                df_sum = ak.stock_hsgt_fund_flow_summary_em()
                if df_sum is not None and len(df_sum) > 0:
                    # 栏目: 资金流向(流入/流出), 成交净额
                    # 取北向沪股通+北向深股通净额
                    pass  # 需要拼5天历史
            except Exception:
                pass

        if result["northbound_5d_net"] is None:
            errors.append("北向:所有数据源返回无效数据(push2his/akshare均不可用)")

    except Exception as e:
        errors.append(f"北向:{e}")

    # --- 融资余额: akshare (T+1延迟, ffill处理缺失) ---
    try:
        import akshare as ak
        df_sh = ak.macro_china_market_margin_sh()
        df_sz = ak.macro_china_market_margin_sz()
        if df_sh is not None and df_sz is not None and len(df_sh) > 0 and len(df_sz) > 0:
            # 列: ['日期','融资余额','融资买入额','融券卖出量','融券余额','融券余量','融资融券余额']
            # 融资余额 = iloc[:, 1]
            sh_series = df_sh.iloc[:, 1].ffill()
            sz_series = df_sz.iloc[:, 1].ffill()
            if len(sh_series) > 5 and len(sz_series) > 5:
                sh_now = float(sh_series.iloc[-1])
                sz_now = float(sz_series.iloc[-1])
                sh_prev = float(sh_series.iloc[-6])
                sz_prev = float(sz_series.iloc[-6])
                total_now = sh_now + sz_now
                total_prev = sh_prev + sz_prev
                if total_prev > 0:
                    result["margin_wow_change_pct"] = round(
                        (total_now - total_prev) / total_prev * 100, 2
                    )
    except ImportError:
        errors.append("akshare未安装,margin不可用")
    except Exception as e:
        errors.append(f"融资:{e}")

    # --- 主力资金方向: 全A主力净流入 ---
    try:
        import akshare as ak
        # 尝试 stock_individual_fund_flow 沪深京三市汇总
        try:
            sh_flow = ak.stock_individual_fund_flow(market="sh")
            sz_flow = ak.stock_individual_fund_flow(market="sz")
            total_net = 0.0
            for df_f in [sh_flow, sz_flow]:
                if df_f is not None and len(df_f) > 0:
                    col_name = None
                    for c in df_f.columns:
                        if '主力净流入' in str(c) or '主力' in str(c):
                            col_name = c
                            break
                    if col_name:
                        total_net += df_f[col_name].sum()
            result["main_force_direction"] = (
                "净流入" if total_net > 0 else "净流出"
            )
        except Exception:
            # fallback: 用北向+融资方向做简单推断
            if result["northbound_5d_net"] is not None and result["margin_wow_change_pct"] is not None:
                nb = result["northbound_5d_net"]
                mg = result["margin_wow_change_pct"]
                if abs(nb) < 0.01 and abs(mg) < 0.01:
                    result["main_force_direction"] = "方向不明"
                elif nb > 0 and mg > 0:
                    result["main_force_direction"] = "净流入(推断)"
                elif nb < 0 and mg < -0.5:
                    result["main_force_direction"] = "净流出(推断)"
                else:
                    result["main_force_direction"] = None
                if "推断" in str(result["main_force_direction"] or ""):
                    result["main_force_note"] += "(北向+融资推断,非直接数据)"
    except ImportError:
        pass
    except Exception as e:
        errors.append(f"主力资金:{e}")

    # --- verdict ---
    nb = result["northbound_5d_net"]
    mg = result["margin_wow_change_pct"]
    mf = result["main_force_direction"]

    bull_signals = 0
    bear_signals = 0
    total_available = 0
    if nb is not None:
        total_available += 1
        if nb > 0: bull_signals += 1
        else: bear_signals += 1
    if mg is not None:
        total_available += 1
        if mg > 0: bull_signals += 1
        else: bear_signals += 1
    if mf is not None:
        total_available += 1
        if "流入" in str(mf) and "流出" not in str(mf): bull_signals += 1
        elif "流出" in str(mf): bear_signals += 1

    if total_available == 0:
        result["verdict"] = None
        result["verdict_rule"] = "无可用数据"
    elif bull_signals == total_available:
        result["verdict"] = "偏多"
        result["verdict_rule"] = "所有可用信号一致偏多"
    elif bear_signals == total_available:
        result["verdict"] = "偏空"
        result["verdict_rule"] = "所有可用信号一致偏空"
    elif total_available == 1:
        # 单数据点,直接按那个数据点判
        result["verdict"] = "偏空(单源)" if bear_signals > 0 else "偏多(单源)"
        result["verdict_rule"] = f"仅{total_available}个可用信号,直接判定"
    elif bull_signals + bear_signals < total_available:
        result["verdict"] = "数据不全"
        result["verdict_rule"] = f"{total_available}个信号中{total_available - bull_signals - bear_signals}个缺失"
    else:
        result["verdict"] = "打架"
        result["verdict_rule"] = "信号方向不一致 → 打架"

    if errors:
        result["error"] = "; ".join(errors)

    return result


# ==============================================================
# 3. sentiment — 波动率情绪
# ==============================================================

def compute_sentiment():
    """CSI300 20日历史波动率 + 5年分位 + 方向"""
    result = {
        "desc": "市场波动率在历史上的位置",
        "hv20": None,
        "hv20_note": "沪深300 20日历史波动率(%)",
        "hv20_percentile_5y": None,
        "hv20_percentile_note": "HV20在5年滚动窗口中的分位(%)",
        "hv20_direction": None,
        "hv20_direction_note": "HV20近5日变化方向: 收敛|扩张|平稳",
        "verdict": None,
        "verdict_rule": "",
        "error": None
    }
    errors = []

    try:
        # 取沪深300近6年日K线 (5年计算 + 1年缓冲)
        url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/data/"
               "CN_MarketDataService.getKLineData?symbol=sh000300&scale=240&ma=no&datalen=1500")
        raw, err = http_get(url, timeout=15)
        if err:
            errors.append(f"CSI300 K线:{err}")
            result["error"] = "; ".join(errors)
            return result

        start = raw.find("[")
        end = raw.rfind("]")
        if start == -1 or end == -1:
            errors.append("CSI300 K线:JSON解析失败")
            result["error"] = "; ".join(errors)
            return result

        bars = json.loads(raw[start:end+1])
        if len(bars) < 120:
            errors.append(f"CSI300 K线不足(仅{len(bars)}条)")
            result["error"] = "; ".join(errors)
            return result

        closes = [float(b['close']) for b in bars]

        # 20日对数收益率
        log_returns_20 = []
        for i in range(1, len(closes)):
            if closes[i-1] > 0:
                log_returns_20.append(math.log(closes[i] / closes[i-1]))
            if len(log_returns_20) >= 20 and i >= 20:
                break
        # 取最后20个日收益率
        log_returns_20 = []
        for i in range(max(0, len(closes)-21), len(closes)):
            if i > 0 and closes[i-1] > 0:
                log_returns_20.append(math.log(closes[i] / closes[i-1]))
        # 用最近20个完整交易日
        valid_log_rets = []
        for i in range(1, len(closes)):
            if closes[i-1] > 0:
                valid_log_rets.append(math.log(closes[i] / closes[i-1]))
        log_returns = valid_log_rets[-20:]
        if len(log_returns) < 15:
            errors.append(f"对数收益率不足({len(log_returns)}条)")
            result["error"] = "; ".join(errors)
            return result

        result["hv20"] = annualize_hv(log_returns)

        # 遍历整个历史计算HV20滚动序列(5年→约1260个交易日)
        hv_series = []
        for i in range(20, len(valid_log_rets)):
            window = valid_log_rets[i-20:i]
            hv = annualize_hv(window)
            if hv is not None:
                hv_series.append(hv)

        if len(hv_series) > 100 and result["hv20"] is not None:
            result["hv20_percentile_5y"] = pct_rank(hv_series, result["hv20"])

        # HV方向: 5日前的HV
        if len(log_returns) >= 5:
            hv5_ago = annualize_hv(valid_log_rets[-25:-5])
            if hv5_ago is not None and result["hv20"] is not None:
                if hv5_ago > 0:
                    delta_pct = (result["hv20"] - hv5_ago) / hv5_ago * 100
                    if delta_pct > 10:
                        result["hv20_direction"] = "扩张"
                    elif delta_pct < -10:
                        result["hv20_direction"] = "收敛"
                    else:
                        result["hv20_direction"] = "平稳"

        # --- verdict ---
        pct = result["hv20_percentile_5y"]
        direction = result["hv20_direction"]
        if pct is not None:
            if pct > 80:
                result["verdict"] = "恐慌"
                result["verdict_rule"] = "分位>80 → 恐慌"
            elif pct < 20:
                result["verdict"] = "自满"
                result["verdict_rule"] = "分位<20 → 自满"
            elif 40 <= pct <= 60 and direction == "收敛":
                result["verdict"] = "中性偏平稳"
                result["verdict_rule"] = "40-60分位且收敛 → 中性偏平稳"
            elif pct > 60:
                prefix = "偏恐慌,收敛中" if direction == "收敛" else "偏恐慌"
                result["verdict"] = prefix
                result["verdict_rule"] = f"分位{pct}+方向{direction} → {prefix}"
            elif pct < 40:
                prefix = "偏低波动" if direction == "扩张" else "偏低波动"
                result["verdict"] = prefix
                result["verdict_rule"] = f"分位{pct}+方向{direction} → {prefix}"
            else:
                result["verdict"] = "中性"

    except Exception as e:
        errors.append(f"情绪计算异常:{e}")

    if errors:
        result["error"] = "; ".join(errors)

    return result


# ==============================================================
# 4. volume — 量能
# ==============================================================

def compute_volume():
    """全A成交额 vs 20日均"""
    result = {
        "desc": "市场参与度",
        "turnover_amount": None,
        "turnover_note": "全A成交额(亿元)",
        "turnover_vs_20d_ma_pct": None,
        "turnover_vs_20d_note": "成交额相对20日均的偏离(%)",
        "direction": None,
        "verdict": None,
        "verdict_rule": "",
        "error": None
    }
    errors = []

    try:
        # 上证K线: Sina (volume=成交额, 单位: 元)
        url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/data/"
               "CN_MarketDataService.getKLineData?symbol=sh000001&scale=240&ma=no&datalen=120")
        raw, err = http_get(url, timeout=10)
        if err:
            errors.append(f"Sina K线:{err}")
        elif raw:
            start = raw.find("[")
            end = raw.rfind("]")
            if start >= 0 and end >= 0:
                bars = json.loads(raw[start:end+1])
                if len(bars) >= 21:
                    # Sina K线 volume字段 = 成交额(元)
                    amounts = []
                    for b in bars:
                        try:
                            amounts.append(float(b['volume']))
                        except (ValueError, KeyError):
                            amounts.append(0.0)

                    if len(amounts) >= 21:
                        # 上证→全A转换系数约2.0-2.5
                        sh_amount = amounts[-1]  # 元
                        sh_amount_yi = sh_amount / 1e8  # 元→亿
                        # 全A = 上证 * 系数 (用最近2日均比估算)
                        if len(amounts) >= 3 and amounts[-2] > 0:
                            # 腾讯实时快照校准上证/全A比例
                            try:
                                t_raw, t_err = http_get(
                                    "http://qt.gtimg.cn/q=sh000001", timeout=5)
                                if not t_err and t_raw:
                                    for line in t_raw.split("\n"):
                                        if "sh000001" in line:
                                            pts = line.split("~")
                                            if len(pts) > 57:
                                                # field 57 ≈ 成交额(万元) → 腾讯实时
                                                tx_amount = float(pts[57]) / 1e4
                                                if tx_amount > 0 and amounts[-1] > 0:
                                                    scale = tx_amount / (amounts[-1]/1e8)
                                                else:
                                                    scale = 2.2
                                            break
                            except Exception:
                                scale = 2.2
                        else:
                            scale = 2.2

                        result["turnover_amount"] = round(sh_amount_yi * scale, 0)

                        # 20日均(排除今日)
                        ma20 = sum(amounts[-21:-1]) / 20
                        ma20_yi = ma20 / 1e8 * scale
                        if ma20_yi > 0:
                            result["turnover_vs_20d_ma_pct"] = round(
                                (result["turnover_amount"] - ma20_yi) / ma20_yi * 100, 1
                            )

    except Exception as e:
        errors.append(f"量能:{e}")

    # --- verdict ---
    if result["turnover_vs_20d_ma_pct"] is not None:
        pct = result["turnover_vs_20d_ma_pct"]
        if pct > 15:
            result["direction"] = "放量"
            result["verdict"] = "放量(有方向)"
            result["verdict_rule"] = "成交额>20日均+15% → 放量"
        elif pct < -15:
            result["direction"] = "缩量"
            result["verdict"] = "缩量观望"
            result["verdict_rule"] = "成交额<20日均-15% → 缩量,资金不进场"
        else:
            result["direction"] = "正常"
            result["verdict"] = "正常"
            result["verdict_rule"] = "成交额在20日均±15% → 正常"
    elif result["turnover_amount"] is not None:
        result["direction"] = "未知"
        result["verdict"] = "数据不足"
    else:
        result["direction"] = None
        result["verdict"] = None

    if errors:
        result["error"] = "; ".join(errors)

    return result


# ==============================================================
# 5. rotation — 板块轮动
# ==============================================================

def compute_rotation():
    """SW一级行业 5日/20日涨跌幅排名"""
    result = {
        "desc": "钱从哪来、去哪了",
        "top5_5d": [],
        "bottom5_5d": [],
        "top5_20d": [],
        "bottom5_20d": [],
        "verdict": None,
        "verdict_rule": "",
        "error": None
    }
    errors = []

    try:
        import akshare as ak
        sw_returns = {}  # {name: {code, chg_5d, chg_20d}}

        for name, code in SW1_MAP.items():
            try:
                df = ak.index_hist_sw(symbol=code, period="day")
                if df is None or len(df) < 30:
                    errors.append(f"{name}({code}):数据不足")
                    continue
                closes = df['收盘'].values
                if len(closes) >= 21:
                    chg_5d = round((closes[-1] - closes[-6]) / closes[-6] * 100, 2)
                    chg_20d = round((closes[-1] - closes[-21]) / closes[-21] * 100, 2)
                    sw_returns[name] = {"code": code, "chg_5d": chg_5d, "chg_20d": chg_20d}
            except Exception as e:
                errors.append(f"{name}({code}):{e}")
                continue

        if len(sw_returns) < 10:
            errors.append(f"行业数据不足(仅{len(sw_returns)}个)")
            result["error"] = "; ".join(errors)
            return result

        # === 数据质量校验: 中位数绝对偏差检测异常行业 ===
        chgs_5d_all = [info['chg_5d'] for _, info in sw_returns.items()]
        chgs_20d_all = [info['chg_20d'] for _, info in sw_returns.items()]

        if len(chgs_5d_all) >= 10:
            chgs_5d_all.sort()
            median_5d = chgs_5d_all[len(chgs_5d_all) // 2]
            mad_5d = sorted([abs(c - median_5d) for c in chgs_5d_all])[len(chgs_5d_all) // 2]
            threshold_5d = max(3.0, 2.0 * mad_5d * 1.4826)  # 至少±3%,避免MAD太小

            chgs_20d_all.sort()
            median_20d = chgs_20d_all[len(chgs_20d_all) // 2]
            mad_20d = sorted([abs(c - median_20d) for c in chgs_20d_all])[len(chgs_20d_all) // 2]
            threshold_20d = max(5.0, 2.0 * mad_20d * 1.4826)

            quality_flags = []
            for name, info in sw_returns.items():
                dev_5d = abs(info['chg_5d'] - median_5d)
                dev_20d = abs(info['chg_20d'] - median_20d)
                if dev_5d > threshold_5d:
                    quality_flags.append(
                        f"{name} 5d={info['chg_5d']}% 偏离中位数{median_5d}% {dev_5d:.1f}%(>{threshold_5d:.0f}%)"
                    )
                if dev_20d > threshold_20d:
                    quality_flags.append(
                        f"{name} 20d={info['chg_20d']}% 偏离中位数{median_20d}% {dev_20d:.1f}%(>{threshold_20d:.0f}%)"
                    )
            if quality_flags:
                result["quality_flags"] = quality_flags
                result["quality_note"] = (
                    f"5d基准:中位数{median_5d}% MAD{mad_5d:.1f} 阈值{threshold_5d:.0f}%; "
                    f"20d基准:中位数{median_20d}% MAD{mad_20d:.1f} 阈值{threshold_20d:.0f}%"
                )

        # 5日排名
        sorted_5d = sorted(sw_returns.items(), key=lambda x: x[1]['chg_5d'], reverse=True)
        result["top5_5d"] = [
            {"sw1": name, "sw1_code": info['code'], "chg_pct": info['chg_5d']}
            for name, info in sorted_5d[:5]
        ]
        result["bottom5_5d"] = [
            {"sw1": name, "sw1_code": info['code'], "chg_pct": info['chg_5d']}
            for name, info in sorted_5d[-5:]
        ]

        # 20日排名
        sorted_20d = sorted(sw_returns.items(), key=lambda x: x[1]['chg_20d'], reverse=True)
        result["top5_20d"] = [
            {"sw1": name, "sw1_code": info['code'], "chg_pct": info['chg_20d']}
            for name, info in sorted_20d[:5]
        ]
        result["bottom5_20d"] = [
            {"sw1": name, "sw1_code": info['code'], "chg_pct": info['chg_20d']}
            for name, info in sorted_20d[-5:]
        ]

        # --- verdict: 全防御 vs 全成长 ---
        top5_names = {x['sw1'] for x in result['top5_5d']}
        bottom5_names = {x['sw1'] for x in result['bottom5_5d']}

        defense_in_top = top5_names & DEFENSE_SECTORS
        growth_in_bottom = bottom5_names & GROWTH_SECTORS
        growth_in_top = top5_names & GROWTH_SECTORS
        defense_in_bottom = bottom5_names & DEFENSE_SECTORS

        if len(defense_in_top) >= 3 and len(growth_in_bottom) >= 3:
            result["verdict"] = "防御轮动"
            result["verdict_rule"] = "涨:防御/价值/消费,跌:成长/科技 → 防御性轮动"
        elif len(growth_in_top) >= 3 and len(defense_in_bottom) >= 3:
            result["verdict"] = "进攻轮动"
            result["verdict_rule"] = "成长科技领涨,防御垫底 → 进攻轮动"
        elif len(growth_in_bottom) >= 2 and len(defense_in_top) >= 2:
            result["verdict"] = "偏防御"
            result["verdict_rule"] = f"科技({', '.join(sorted(growth_in_bottom & bottom5_names))[:20]})在跌,防御在涨 → 偏防御"
        elif len(defense_in_bottom) >= 2 and len(growth_in_top) >= 2:
            result["verdict"] = "偏进攻"
            result["verdict_rule"] = f"防御在跌,科技({', '.join(sorted(growth_in_top & top5_names))[:20]})在涨 → 偏进攻"
        else:
            result["verdict"] = "无明显方向"
            result["verdict_rule"] = ""

    except ImportError:
        errors.append("akshare未安装,轮动不可用")
        result["error"] = "; ".join(errors)
    except Exception as e:
        errors.append(f"轮动:{e}")
        result["error"] = "; ".join(errors)

    return result


# ==============================================================
# summary + synthesis
# ==============================================================

def compute_summary(breadth, flow, sentiment, volume, rotation):
    """从五个维度生成一句话定性: 优先极端信号 → 轮动方向 → 量能"""
    signals = []

    # 极端信号优先
    hv_verdict = str(sentiment.get("verdict") or "")
    hv_pct = sentiment.get("hv20_percentile_5y")
    if hv_pct is not None and hv_pct > 80:
        signals.append("高波动")
    elif hv_pct is not None and hv_pct < 20:
        signals.append("低波动")

    mg_chg = flow.get("margin_wow_change_pct")
    if mg_chg is not None and mg_chg < -3:
        signals.append("杠杆出清")
    elif mg_chg is not None and mg_chg > 3:
        signals.append("杠杆上升")

    # 轮动方向
    rot_v = rotation.get("verdict") or ""
    if rot_v in ("防御轮动", "偏防御"):
        bottom5_names = [x['sw1'] for x in rotation.get('bottom5_5d', [])[:3]]
        if bottom5_names:
            signals.append(f"{'/'.join(bottom5_names[:2])}失血" if len(bottom5_names) > 1 else f"{bottom5_names[0]}走弱")
        else:
            signals.append("防御主导")
    elif rot_v == "进攻轮动":
        top5_names = [x['sw1'] for x in rotation.get('top5_5d', [])[:2]]
        if top5_names:
            signals.append(f"{'/'.join(top5_names[:2])}领涨")
        else:
            signals.append("进攻主导")

    # 量能
    vol_dir = volume.get("direction")
    if vol_dir == "缩量":
        signals.append("缩量")
    elif vol_dir == "放量":
        signals.append("放量")

    if not signals:
        return "市场结构无明显方向"
    return "，".join(signals)


def compute_synthesis(breadth, flow, sentiment, volume, rotation, waw_score=None):
    """信号汇总: 计数 + 一致性判定"""
    verdicts = [
        breadth.get("verdict"),
        flow.get("verdict"),
        sentiment.get("verdict"),
        volume.get("verdict"),
        rotation.get("verdict")
    ]

    count = {"bullish": 0, "bearish": 0, "neutral": 0, "mixed": 0}

    for v in verdicts:
        if v is None:
            continue
        if v in ("偏多", "进攻轮动", "偏进攻", "自满"):
            count["bullish"] += 1
        elif v in ("偏空", "防御轮动", "偏防御", "恐慌", "缩量观望", "杠杆出清"):
            count["bearish"] += 1
        elif v in ("打架",):
            count["mixed"] += 1
        else:
            count["neutral"] += 1

    total_signals = sum(count.values())
    if total_signals == 0:
        consensus = "数据不足"
    elif count["bearish"] == total_signals:
        consensus = "偏空"
    elif count["bullish"] == total_signals:
        consensus = "偏多"
    elif count["bearish"] >= total_signals - 1 and count["mixed"] <= 1:
        consensus = "谨慎偏空"
    elif count["bullish"] >= total_signals - 1 and count["mixed"] <= 1:
        consensus = "谨慎偏多"
    else:
        consensus = "分歧"

    result = {
        "desc": "信号汇总。不合成单一数字——一致性判定+信号计数。策略影响由Hermes产出。",
        "waw_zone": waw_score.get("zone") if waw_score else None,
        "waw_note": "来自step_a,提供中长期情绪位置锚点",
        "signal_count": count,
        "consensus": consensus,
        "consensus_note": "全偏空→偏空 | 多数偏空→谨慎偏空 | 各半→分歧 | 多数偏多→谨慎偏多 | 全偏多→偏多",
        "action_hint": None,
        "action_hint_note": "策略影响由Hermes产出,Claude Code不在此JSON中做推理"
    }
    return result


def compute_waw_overlap(waw_score=None):
    """W&W重叠数据源的双重读法"""
    return {
        "desc": "与W&W共享数据源的指标。L2.5读法不同于W&W——W&W问'在历史上排第几',L2.5问'这周在加还是撤'。",
        "margin_balance": {
            "value_trillion": None,
            "waw_read": waw_score.get("margin_percentile", "未获取") if waw_score else "未获取",
            "l2d5_read": None
        },
        "volume": {
            "value_billion": None,
            "waw_read": waw_score.get("volume_percentile", "未获取") if waw_score else "未获取",
            "l2d5_read": None
        }
    }


# ==============================================================
# main
# ==============================================================

def main():
    # 解析命令行参数
    frequency = "daily_close"
    waw_score = None

    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "--frequency" and i + 1 < len(sys.argv):
            frequency = sys.argv[i + 1]
            i += 2
        elif sys.argv[i] == "--waw_score" and i + 1 < len(sys.argv):
            try:
                waw_score = json.loads(sys.argv[i + 1])
            except json.JSONDecodeError:
                print(f"[WARN] --waw_score JSON 解析失败, 忽略", file=sys.stderr)
            i += 2
        else:
            i += 1

    # 非交易日检测
    if not is_trading_day():
        # 仍输出JSON, 标记非交易日
        result = {
            "_meta": {
                "schema": "L2.5 市场水温 — step_m v1.0",
                "date": date.today().isoformat(),
                "generated_by": "step_m.py",
                "is_trading_day": False,
                "note": "非交易日, 数据可能为前一交易日收盘数据"
            },
            "step_m": {
                "snapshot_time": f"{date.today().isoformat()} 15:00:00",
                "frequency": frequency,
                "note": "非交易日",
                "summary": "非交易日, 无实时数据",
                "breadth": {"verdict": None, "error": "非交易日"},
                "flow": {"verdict": None, "error": "非交易日"},
                "sentiment": {"verdict": None, "error": "非交易日"},
                "volume": {"verdict": None, "error": "非交易日"},
                "rotation": {"verdict": None, "error": "非交易日"},
                "synthesis": {"signal_count": {"bullish":0,"bearish":0,"neutral":0,"mixed":0},
                              "consensus": "非交易日"},
                "waw_overlap": {"desc": "", "margin_balance": {}, "volume": {}}
            }
        }
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        return

    now = datetime.now()

    # 五个维度并行计算
    breadth   = compute_breadth()
    flow      = compute_flow()
    sentiment = compute_sentiment()
    volume    = compute_volume()
    rotation  = compute_rotation()

    # 生成摘要和综合
    summary   = compute_summary(breadth, flow, sentiment, volume, rotation)
    synthesis = compute_synthesis(breadth, flow, sentiment, volume, rotation, waw_score)
    overlap   = compute_waw_overlap(waw_score)

    # 回填 waw_overlap 的 L2.5 读法
    try:
        import akshare as ak
        df_sh = ak.macro_china_market_margin_sh()
        df_sz = ak.macro_china_market_margin_sz()
        if df_sh is not None and df_sz is not None and len(df_sh) > 5 and len(df_sz) > 5:
            # col 1 = 融资余额, col 6 = 融资融券余额(总额)
            # value_trillion用总额(col 6)匹配W&W口径
            sh_total = float(df_sh.iloc[-1, 6])  # 融资融券余额列
            sz_total = float(df_sz.iloc[-1, 6])
            total = sh_total + sz_total  # 元
            overlap["margin_balance"]["value_trillion"] = round(total / 1e12, 2)  # 元→万亿
            # l2d5_read用融资余额(col 1)的变化率
            sh_now = float(df_sh.iloc[-1, 1])
            sz_now = float(df_sz.iloc[-1, 1])
            sh_prev = float(df_sh.iloc[-6, 1])
            sz_prev = float(df_sz.iloc[-6, 1])
            total_now = sh_now + sz_now
            total_prev = sh_prev + sz_prev
            if total_prev > 0:
                chg = round((total_now - total_prev) / total_prev * 100, 2)
                if chg < -0.5:
                    direction = "杠杆在出清"
                elif chg > 0.5:
                    direction = "杠杆在上升"
                else:
                    direction = "杠杆稳定"
                overlap["margin_balance"]["l2d5_read"] = f"周变化率{chg}%→{direction}"
    except Exception:
        pass

    if volume.get("turnover_amount") is not None:
        overlap["volume"]["value_billion"] = volume["turnover_amount"]
        pct = volume.get("turnover_vs_20d_ma_pct")
        if pct is not None:
            overlap["volume"]["l2d5_read"] = f"vs20日均{pct}%→{'缩量' if pct < -10 else '放量' if pct > 10 else '正常'}"

    # 组装输出
    result = {
        "_meta": {
            "schema": "L2.5 市场水温 — step_m v1.0",
            "purpose": "L2宏观象限和L3赛道之间的中场镜头。快变量定方向框，慢变量在框内深耕。",
            "flow": "L2 宏观象限 → L2.5 市场水温 → L3 赛道（在L2.5框内） → L4 个股",
            "role_boundary": "Claude Code只出数据桥JSON，Hermes据此填分析模板",
            "date": date.today().isoformat(),
            "generated_by": "step_m.py"
        },
        "step_m": {
            "snapshot_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "frequency": frequency,
            "note": "日后L5恢复,传参 --frequency intraday,同结构采集盘中数据",
            "summary": summary,
            "breadth": breadth,
            "flow": flow,
            "sentiment": sentiment,
            "volume": volume,
            "rotation": rotation,
            "synthesis": synthesis,
            "waw_overlap": overlap
        }
    }

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
