"""
Hermes 持仓巡检脚本 — MA20 每日检查
用法: python hermes_holdings_check.py
输出: 澜起科技 MA20 状态 → stdout → Hermes cron 推送
"""
import json, urllib.request, sys
from datetime import date

# 当前持仓
HOLDINGS = [
    {"name": "澜起科技", "code": "688008", "sina": "sh688008", "cost": 254.04},
]

def fetch_kline(sina_sym, count=60):
    url = f"https://quotes.sina.cn/cn/api/jsonp_v2.php/data/CN_MarketDataService.getKLineData?symbol={sina_sym}&scale=240&ma=no&datalen={count}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
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

def check_ma(bars):
    closes = [float(b["close"]) for b in bars]
    if len(closes) < 30:
        return None

    ma20 = sum(closes[-20:]) / 20
    ma30 = sum(closes[-30:]) / 30

    # MA20 5日斜率
    ma20_5d_ago = sum(closes[-25:-5]) / 20 if len(closes) >= 25 else ma20
    slope = (ma20 - ma20_5d_ago) / ma20_5d_ago * 100 if ma20_5d_ago else 0

    price = closes[-1]
    dev = (price - ma20) / ma20 * 100

    # 趋势
    if slope < -0.5:
        trend = "MA20下行"
    elif slope > 0.5:
        trend = "MA20上升"
    else:
        trend = "MA20走平"

    # 警报
    alerts = []
    if price < ma20:
        alerts.append(f"价破MA20({dev:.1f}%)")
    if slope < 0:
        alerts.append(f"MA20斜率转负({slope:.1f}%)")

    return {
        "price": price, "ma20": round(ma20, 2), "ma30": round(ma30, 2),
        "slope_pct": round(slope, 2), "trend": trend, "alerts": alerts
    }

print(f"=== 持仓巡检 {date.today()} ===\n")

for h in HOLDINGS:
    bars, err = fetch_kline(h["sina"])
    if err:
        print(f"{h['name']}: 数据拉取失败 — {err}")
        continue

    result = check_ma(bars)
    if not result:
        print(f"{h['name']}: K线不足")
        continue

    pnl_pct = (result["price"] - h["cost"]) / h["cost"] * 100

    print(f"{h['name']} ({h['code']})")
    print(f"  价: {result['price']:.2f}  MA20: {result['ma20']}  MA30: {result['ma30']}")
    print(f"  趋势: {result['trend']}  斜率: {result['slope_pct']:.2f}%")
    print(f"  成本: {h['cost']}  浮盈/亏: {pnl_pct:+.1f}%")

    if result["alerts"]:
        for a in result["alerts"]:
            print(f"  [ALERT] {a}")
    else:
        print(f"  [OK] 无警报")

    # 止损检查
    if pnl_pct <= -8:
        print(f"  [STOP] 触发 -8% 止损线！当前 {pnl_pct:.1f}%")
    elif pnl_pct <= -5:
        print(f"  [WARN] 接近止损线: {pnl_pct:.1f}%")

    print()
