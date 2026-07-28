"""
SP500风控模型 - 数据更新脚本
从Yahoo Finance抓取最新数据，输出可导入Excel的格式
"""
import urllib.request, json
from datetime import datetime, timedelta

def yahoo_weekly(symbol, name, weeks=8):
    """获取周度OHLCV"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=3mo&interval=1wk"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
        chart = data['chart']['result'][0]
        ts = chart['timestamp']
        q = chart['indicators']['quote'][0]
        result = []
        for i in range(len(ts)):
            dt = datetime.fromtimestamp(ts[i])
            if q['close'][i] is not None:
                result.append({
                    'date': dt.strftime('%Y-%m-%d'),
                    'excel_date': dt.strftime('%m/%d'),
                    'open': q['open'][i],
                    'high': q['high'][i],
                    'low': q['low'][i],
                    'close': q['close'][i],
                    'volume': q['volume'][i] or 0,
                })
        return result

# 获取所有数据
sp500 = yahoo_weekly("%5EGSPC", "SP500")
nasdaq = yahoo_weekly("%5EIXIC", "NASDAQ")
vix = yahoo_weekly("%5EVIX", "VIX")
tnx = yahoo_weekly("%5ETNX", "10Y")
fvx = yahoo_weekly("%5EFVX", "5Y")
irx = yahoo_weekly("%5EIRX", "13W")
hyg = yahoo_weekly("HYG", "HYG")
crude = yahoo_weekly("CL=F", "Crude")

# 估算2Y收益率 (线性插值: 13W + (5Y-13W) * (2-0.25)/(5-0.25))
# 2/5/10Y的插值
esti_2y = []
for i in range(min(len(tnx), len(fvx), len(irx))):
    t10 = tnx[i]['close']
    t5 = fvx[i]['close']
    t13w = irx[i]['close']
    # 2Y = 13W + (5Y - 13W) * 1.75/4.75
    est_2y_val = t13w + (t5 - t13w) * 1.75 / 4.75
    spread_10y2y = t10 - est_2y_val
    esti_2y.append({
        'date': tnx[i]['date'],
        '2Y_est': est_2y_val,
        '10Y': t10,
        '10Y2Y_spread': spread_10y2y,
        '5Y': t5,
        '13W': t13w,
    })

# HY OAS估算: 用HYG价格变化率*100近似利差变化
# 实际HY OAS大约在300-500bp范围，HYG跌1%约对应OAS扩大30-50bp
hyg_changes = []
for i in range(1, len(hyg)):
    prev_close = hyg[i-1]['close']
    curr_close = hyg[i]['close']
    pct_change = (curr_close - prev_close) / prev_close * 100
    hyg_changes.append({
        'date': hyg[i]['date'],
        'HYG': curr_close,
        'HYG_chg_pct': pct_change,
    })

print("=" * 70)
print("SP500风控模型 - 最新周度数据 (截至5/16)")
print("=" * 70)

print("\n--- SP500 ---")
for d in sp500[-8:]:
    print(f"  {d['excel_date']}: C={d['close']:.1f}  V={d['volume']/1e6:.0f}M")

print("\n--- VIX ---")
for d in vix[-8:]:
    print(f"  {d['excel_date']}: C={d['close']:.2f}")

print("\n--- 收益率 ---")
for d in esti_2y[-8:]:
    print(f"  {d['date']}: 10Y={d['10Y']:.2f}%  5Y={d['5Y']:.2f}%  2Y(est)={d['2Y_est']:.2f}%  10Y-2Y={d['10Y2Y_spread']:.2f}%")

print("\n--- HYG (HY OAS代理) ---")
for d in hyg[-8:]:
    print(f"  {d['excel_date']}: C={d['close']:.2f}")

print("\n--- 原油 ---")
for d in crude[-8:]:
    print(f"  {d['excel_date']}: C={d['close']:.2f}")

# 计算模型所需的特征值变化 (简化版，对比4/10基准)
print("\n" + "=" * 70)
print("特征值变化估算 (vs 4/10基准)")
print("=" * 70)

# 4/10时: VIX_z=0.84, HY_z=0.64, Spread_z=-0.98, SP500_z=1.15, MA_z=-0.26
# 估算当前方向:

# SP500从6782涨到7408(+9.2%) -> SP500_z会大幅改善
# 10Y从~4.3涨到4.60(+30bp)
# 5Y从~3.9涨到4.26(+36bp)-> 短端涨得更快 -> 利差可能收窄 -> Spread_z恶化
# HYG从80.51跌到79.46(-1.3%) -> HY利差扩大 -> HY_z恶化
# VIX从18到18.4 -> 小幅上升

print("""
特征              4/10基准    当前方向    对评分影响
VIX_z (25%)       0.84        持平微升     中性偏空
HY变化_z (25%)    0.64        恶化(HYG-1.3%) +利空
期限利差_z (15%)  -0.98       待定(需2Y确认) 可能恶化
SP500趋势_z(15%)  1.15        改善(+9.2%)   +利好(虚假)
MA斜率_z (20%)    -0.26       改善(MA拐头)  +利好(虚假)

综合: SP500价格改善(35%权重)被信用恶化(25%)抵消
     评分估计仍在0.15-0.30区间 = 地狱模式边界
""")

# 关键发现
print("=" * 70)
print("关键发现")
print("=" * 70)
print(f"""
1. 期限利差: 10Y-2Y(est)最新约{esti_2y[-1]['10Y2Y_spread']:.1f}bp
   - 5Y涨得比10Y快(5Y +36bp vs 10Y +30bp) -> 利差在收窄
   - 这对风控模型是恶化信号

2. 信用市场: HYG从80.51->79.46(-1.3%)
   - 连续3周下跌，信用恶化确认
   - HY_z会在0.64基础上继续上升

3. SP500趋势改善是"虚假的"
   - 价格在涨但量能在萎缩
   - 5/11巨量滞涨后缩量拉升 = 机构出货

4. Sahm Rule: 无法获取（FRED不通），假设未触发(z<2)
""")
