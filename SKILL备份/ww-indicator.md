---
name: ww-indicator
description: A股逆向情绪指标 v1.2.0 — 6因子5年滚动百分位，0-100刻度，越低越恐惧(逆向看多)。v1.2.0新增三合一压力测试(交叉验证+敏感性+历史回看)+历史顶底验证表
version: 1.2.0
---

# A股W&W逆向情绪指标

## 概述
基于BofA W&W Indicator逻辑复刻的A股逆向情绪指标。6个因子、5年滚动窗口百分位标准化、等权合成。

## 刻度
- **0-20**: Very Bullish — 极度恐惧,逆向买入窗口
- **20-40**: Bullish — 偏恐惧,偏多
- **40-60**: Neutral — 中性
- **60-80**: Bearish — 偏亢奋,偏空
- **80-100**: Very Bearish — 极度亢奋,逆向卖出窗口

## 六个因子
| 因子 | 数据源 | 方向 |
|------|--------|------|
| F1 融资余额(万亿) | akshare macro_china_market_margin | 越高=越亢奋 |
| F2 CSI300 PE | akshare stock_zh_index_value_csindex + 收盘价外推 | 越高=越贵 |
| F3 融资买入/成交量 | akshare | 越高=越亢奋 |
| F4 10年国债收益率 | akshare bond_zh_us_rate | 越低越好(反转) |
| F5 成交量(亿股) | akshare stock_zh_index_daily | 越高=越活跃 |
| F6 CSI300 60日收益率 | akshare | 越高=越强 |

## 已知陷阱

### T+1延迟导致融资因子归零（2026.7.4发现并修复）

**症状**：最新日期的F1(融资余额)和F3(融资买入比)百分位显示为0.0，W&W从~92一天暴跌至57。

**根因**：`akshare.macro_china_market_margin_sh/sz()` 返回的融资数据有T+1延迟。当上证/CSI300数据更新到T日但融资数据只到T-1日时，RIGHT join合并后T日的margin和融资买入额为NaN。滚动百分位计算中`NaN < 任何值`返回False → 百分位=0。

**修复**（已写入脚本）：在`fetch_all()`末尾对margin和融资买入额做`ffill()`前向填充。
```python
base['margin'] = base['margin'].ffill()
base['融资买入额'] = base['融资买入额'].ffill()
```

**验证**：修复后2026.7.3的W&W=90.3（融资余额99.7分位、融资买入比99.4分位），与6月底90-95区间一致。

### 数据铁律：WW读数必须每次跑脚本

禁止从记忆/SOUL.md引用WW读数。融资数据每日更新，引用旧读数会掩盖趋势变化。每次需要W&W时执行：
```bash
python3 ~/AppData/Local/hermes/scripts/ww_indicator.py
```

## 压力测试与验证 (2026.7.5)

### 数据可靠性
akshare 内部多源交叉验证通过（F1/F3/F4 偏差均为 0.00%）。东方财富网页 push API 有反爬，无法实时对比，但 akshare 数据完全自洽。

### 敏感性：F2 换全A PE
CSI300 PE → 全A PE (000985, 19.1 vs 14.9)，W&W 从 90.3 → 92.8 (Δ=+2.6)。影响有限。CSI300 PE 是可接受的 F2 选择。

### 历史验证：6个关键顶底
顶部识别完美（4/4 均在 Very Bearish），底部识别良好（2/3，2024.2 仅到 Neutral 偏弱）。
W&W 峰值领先市场顶约 6 周（2015年：4.30 见峰 97.1 → 6.12 市场顶）。
当前 90.3 = 全历史分位 96.5%，类比 2015 年 4 月位置。

详见 `references/stress-test-2026-07-05.md`

### 已知限制
- 东方财富 push2/push2his API 不可访问（反爬），无法做跨源交叉验证
- akshare 全A PE (000985) 历史仅 20 个交易日，无法做真正 5 年滚动百分位
- 成交量单位差异：东方财富 K 线用"手"，akshare 用"股"

## 参考文档
- `references/bofa-methodology-2026-05.md`: BofA 2026年5月原始报告方法论详解
- `references/stress-test-2026-07-05.md`: 2026.7.5 三合一压力测试完整报告 — 交叉验证 + 敏感性 + 历史回看
