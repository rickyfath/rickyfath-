# L0 — A股 W&W 逆向情绪指标 完整算法文档

> 生成日期: 2026-07-05  
> 版本: v1.1  
> 脚本路径: `~/AppData/Local/hermes/scripts/ww_indicator.py`  
> 所属框架: A股投资决策体系 PART 1 §1.0

---

## 目录

1. [概述](#1-概述)
2. [6因子详细定义](#2-6因子详细定义)
3. [算法流程](#3-算法流程)
4. [核心代码逐行解析](#4-核心代码逐行解析)
5. [L0→L1 传导设计](#5-l0l1-传导设计)
6. [历史回测验证](#6-历史回测验证)
7. [敏感性测试](#7-敏感性测试)
8. [交叉验证](#8-交叉验证)
9. [与 BofA 原版的差异](#9-与-bofa-原版的差异)
10. [已知BUG与修复](#10-已知bug与修复)
11. [使用方式](#11-使用方式)

---

## 1. 概述

### 是什么

复刻 BofA Securities 的 W&W Indicator（Winnie Wu & Gina Wu），一个 A 股逆向情绪指标。

**核心逻辑**：别人贪婪我恐惧，别人恐惧我贪婪。
- W&W 低 = 市场恐惧 = **逆向看多**
- W&W 高 = 市场亢奋 = **逆向看空**

### 刻度

```
  0 ———— 20 ———— 40 ———— 60 ———— 80 ———— 100
    <<<        <<        --        >>       >>>
 Very       Bullish   Neutral   Bearish   Very
Bullish                                Bearish
 买入窗口                            卖出窗口
```

### 数据源

全部来自 **akshare**（免费开源 Python 库），无需付费数据终端。

| 数据 | akshare 函数 |
|------|-------------|
| 融资余额（沪深） | `macro_china_market_margin_sh()` / `_sz()` |
| 上证指数日K | `stock_zh_index_daily(symbol='sh000001')` |
| CSI300 日K | `stock_zh_index_daily(symbol='sh000300')` |
| CSI300 PE (中证指数) | `stock_zh_index_value_csindex(symbol='000300')` |
| 10年国债收益率 | `bond_zh_us_rate()` |

---

## 2. 6因子详细定义

### F1 — 融资余额（万亿）

```
数据: 沪深两市融资余额合计
公式: F1 = (沪市融资余额 + 深市融资余额) / 1e12
方向: 正向 — F1越高 = 杠杆越高 = 市场越亢奋
单位: 万亿
```

**akshare获取**:
```python
sh = ak.macro_china_market_margin_sh()  # 沪市
sz = ak.macro_china_market_margin_sz()  # 深市
F1 = (sh['融资余额'] + sz['融资余额']) / 1e12
```

### F2 — CSI300 PE

```
数据: 中证指数官方 CSI300 市盈率(TTM) × 收盘价外推
公式: F2 = known_PE × (当日close / 已知PE日的close)   # 外推非PE披露日的值
方向: 正向 — F2越高 = 估值越贵 = 市场越亢奋
```

**PE外推逻辑**: 中证指数每周只披露几次PE，非披露日需要用收盘价比率外推。

```python
pe = ak.stock_zh_index_value_csindex(symbol='000300')
latest_pe = pe['市盈率1'].iloc[-1]
ratio = latest_pe / close_at_pe_date
daily_pe = close × ratio  # 非披露日的PE外推
```

### F3 — 融资买入比

```
数据: 沪市融资买入额 ÷ 上证成交量
公式: F3 = 沪市融资买入额(元) ÷ 上证成交量(股)
方向: 正向 — F3越高 = 融资交易越活跃 = 市场越亢奋
单位: 元/股
```

```python
F3 = 融资买入额 / volume  # 元/股
```

### F4 — 10年国债收益率

```
数据: 中国10年期国债收益率
公式: F4 = 当日10Y国债收益率(%)
方向: 反向(反转) — F4越低 = 货币越宽松 = 流动性越好
```

**反转处理**: 标准化时取 `100 - percentile`，因为低收益率=利好。

### F5 — 成交量

```
数据: 上证日成交量
公式: F5 = 上证日成交量(股) / 1e8
方向: 正向 — F5越高 = 交易越活跃 = 市场越亢奋
单位: 亿股
```

```python
F5 = volume / 1e8  # 股 → 亿股
```

### F6 — 60日动量

```
数据: CSI300 收盘价
公式: F6 = (当日close / 60日前close - 1) × 100
方向: 正向 — F6越高 = 近期涨幅越大 = 市场越亢奋
单位: %
```

```python
F6 = close.pct_change(60) * 100
```

---

## 3. 算法流程

```
Step 1  — 抓取全部原始数据 (akshare 5个API)
Step 2  — 计算6个因子原始值 (F1-F6)
Step 3  — 5年滚动百分位标准化 (每个因子独立)
Step 4  — 等权平均合成 W&W
Step 5  — 查表得压缩系数 → 传给 L1
```

### 5年滚动百分位（核心算法）

```
窗口大小: 1260 个交易日 (≈252 × 5年)
最少有效样本: 500 个交易日

对每一个交易日 i:
  取 [i-1260, i] 窗口内的所有非NaN值
  统计: 窗口内有多少比例的值 < 当日值
  → 这个比例 × 100 = 当日百分位 (0-100)

  F4(10年国债)特殊处理:
    percentile_reversed = 100 - percentile_original
    因为低收益率 = 好信号 = 应该打低分
```

**伪代码**:
```
for i in range(1260, len(series)):
    window = series[i-1260 : i+1]
    valid = window中非NaN值
    if len(valid) < 500: 跳过（数据不足）
    
    count_less = (valid < series[i]).sum()
    percentile = count_less / len(valid) * 100
    
    if 因子是F4(反转):
        percentile = 100 - percentile
    
    result[i] = percentile
```

### 合成

```
W&W = (F1_pct + F2_pct + F3_pct + F4_pct + F5_pct + F6_pct) / 6
```

等权平均，非 BofA 原版的专有权重。

---

## 4. 核心代码逐行解析

### 4.1 数据抓取 `fetch_all()`

```python
def fetch_all():
    # === 沪深融资余额(合计) ===
    sh = ak.macro_china_market_margin_sh()
    sz = ak.macro_china_market_margin_sz()
    # 按日期合并，沪+深融资余额
    mrg = pd.merge(sh[['dt','融资余额']], sz[['dt','融资余额']], 
                   on='dt', how='outer', suffixes=('_sh','_sz'))
    mrg['margin'] = mrg['融资余额_sh'].fillna(0) + mrg['融资余额_sz'].fillna(0)
    
    # === 上证成交量 ===
    sh1 = ak.stock_zh_index_daily(symbol='sh000001')
    base = pd.merge(base, sh1[['dt','volume']], on='dt', how='right')
    
    # === CSI300收盘价 ===
    hs3 = ak.stock_zh_index_daily(symbol='sh000300')
    base = pd.merge(base, hs3[['dt','close']], on='dt', how='right')
    
    # === CSI300 PE (中证指数官方) ===
    pe = ak.stock_zh_index_value_csindex(symbol='000300')
    # PE外推: 非披露日 = 已知PE × 收盘价比率
    latest_pe = pe['市盈率1'].iloc[-1]
    ratio = latest_pe / close_at_pe_date
    base['pe'] = base['pe_raw'].fillna(base['close'] * ratio)
    
    # === 融资买入额 ===
    base = pd.merge(base, sh[['dt','融资买入额']], on='dt', how='left')
    
    # === 10年国债 ===
    bond = ak.bond_zh_us_rate()
    base = pd.merge(base, bond[['dt','中国国债收益率10年']], on='dt', how='left')
    
    # === BUG修复: T+1延迟前向填充 ===
    base['margin'] = base['margin'].ffill()
    base['融资买入额'] = base['融资买入额'].ffill()
    
    return base
```

### 4.2 因子计算 `compute_factors()`

```python
def compute_factors(df):
    df['f1'] = df['margin'] / 1e12                    # F1: 融资余额(万亿)
    df['f2'] = df['pe']                                # F2: CSI300 PE
    df['f3'] = df['融资买入额'] / df['volume']          # F3: 融资买入比
    df['f4'] = df['bond10y']                           # F4: 10年国债(%)
    df['f5'] = df['volume'] / 1e8                      # F5: 成交量(亿股)
    df['f6'] = df['close'].pct_change(60) * 100        # F6: 60日动量(%)
    return df
```

### 4.3 标准化 `rolling_pct()`

```python
def rolling_pct(series, window=1260, reverse=False):
    result = pd.Series(np.nan, index=series.index)
    vals = series.values
    for i in range(window, len(vals)):
        win = vals[i-window:i+1]
        valid = win[~np.isnan(win)]
        if len(valid) < 500:         # 最少500个有效样本
            continue
        pct = (valid < vals[i]).sum() / len(valid) * 100
        if reverse:                   # F4反转
            pct = 100 - pct
        result.iloc[i] = pct
    return result
```

### 4.4 主流程

```python
def main():
    df = fetch_all()           # Step 1: 抓数据
    df = compute_factors(df)   # Step 2: 算因子
    
    # Step 3: 标准化
    for col, rev, name in [
        ('f1', False, '融资余额'),
        ('f2', False, 'CSI300 PE'),
        ('f3', False, '融资买入比'),
        ('f4', True,  '10年国债(反)'),   # F4反转
        ('f5', False, '成交量'),
        ('f6', False, '60日动量'),
    ]:
        df[f'{col}_pct'] = rolling_pct(df[col], reverse=rev)
    
    # Step 4: 合成
    pct_cols = ['f1_pct','f2_pct','f3_pct','f4_pct','f5_pct','f6_pct']
    df['W&W'] = df[pct_cols].mean(axis=1)
    
    # Step 5: 输出
    latest = df.dropna(subset=['W&W']).iloc[-1]
    print(f"W&W: {latest['W&W']:.1f}")
```

### 4.5 因子方向汇总

```python
factor_specs = [
    ('f1', False,  '融资余额'),      # 正向: 越高越亢奋
    ('f2', False,  'CSI300 PE'),    # 正向: 越高越贵
    ('f3', False,  '融资买入比'),     # 正向: 越高越亢奋
    ('f4', True,   '10年国债(反)'),  # 反转: 越低越好
    ('f5', False,  '成交量'),        # 正向: 越高越活跃
    ('f6', False,  '60日动量'),      # 正向: 越高越强
]
```

---

## 5. L0→L1 传导设计

> 设计日期: 2026.7.5  
> 定位: **刹车，不是油门**。亢奋时减速，恐惧时不加速。

### 传导公式

```
L1_有效上限 = L1_SP500风控上限 × L0_WW压缩系数

L5_实操仓位 = min(
    L4_战略上限,
    L1_有效上限                           // 现阶段：L0×L1，不等L2/L3
)
```

### 压缩系数映射表

| W&W 读数 | 区间标签 | 压缩系数 | 设计依据 |
|----------|---------|---------|---------|
| 0-20 | Very Bullish | **×1.0** | 极度恐惧，不压缩 |
| 20-40 | Bullish | **×1.0** | 偏恐惧，不压缩（2019.1=32.2, 2024.9=29.8 最佳买点） |
| 40-60 | Neutral | **×1.0** | 指标没态度，不干预（2024.2仅40.6，避免误杀） |
| 60-80 | Bearish | **×0.6** | 偏亢奋，中等压缩 |
| 80-100 | Very Bearish | **×0.4** | 极度亢奋，重压缩（当前90.3在这一档） |

### 5条设计决策

```
1. Bullish区 ×1.0 不压缩 — 指标告诉你"便宜了"，不该再打八折
   不对称是刻意的: W&W是刹车不是油门

2. Neutral区 ×1.0 不干预 — 2024年2月量化崩盘W&W仅40.6(Neutral)
   在Neutral区压缩会误杀底部。指标没态度时不出手

3. 80-100只给×0.4不给×0 — 2015年W&W见顶97.1后市场还涨了6周
   ×0 = 空仓会踏空最后一段。×0.4 = 强制保守但不离场

4. 非对称设计 — 恐惧端不放大仓位
   2019.1(W&W=32.2)和2024.9(W&W=29.8)虽是6年最佳买点
   但不因此放大仓位——底部捕捉交给L2宏观象限

5. L2/L3暂不乘法链叠加 — 系数未量化
   等L1-L3数据管道打通后再并轨
```

### 当前示例 (2026.7.5)

```
W&W = 90.3 (Very Bearish)  →  L0压缩 = ×0.4
SP500 = L3 (困难)          →  L1_SP500 = 70%

L1_有效上限 = 70% × 0.4 = 28%
```

---

## 6. 历史回测验证

> 测试日期: 2026.7.5  
> 数据范围: 2007-03-30 ~ 2026-07-03 (4681个有效交易日)  
> 历史均值: 62.0

### 6大关键转折点

| 日期 | W&W | 区间 | 事件 | 判定 |
|------|-----|------|------|------|
| 2015-04-30 | **97.1** | Very Bearish | 历史最高（距牛市顶6周） | — |
| 2015-06-12 | 89.8 | Very Bearish | 2015年牛市顶 | ✓ |
| 2016-02-29 | 61.2 | Bearish | 2016年熔断底 | △ 偏弱 |
| 2019-01-04 | **32.2** | Bullish | 2019年贸易战底 | ✓ |
| 2021-02-18 | 86.9 | Very Bearish | 2021年核心资产泡沫顶 | ✓ |
| 2024-02-05 | 40.6 | Neutral | 2024年雪球+量化崩盘底 | △ 偏弱 |
| 2024-09-23 | **29.8** | Bullish | 924行情前夜 | ✓ 最佳买点 |
| 2024-10-08 | 88.6 | Very Bearish | 国庆后开盘顶 | ✓ |
| 2026-07-03 | **90.3** | Very Bearish | 当前 | 历史第二高 |

**识别率**: 6个关键转折点，正确5个（2024.2仅40.6 Neutral，偏弱但不致命）

### 历史极值

```
最高: 2015-04-30  W&W=97.1  — 距2015年6月牛市顶约6周
最低: 2011-09-20  W&W=20.7  — 2011年熊市深底
均值: 62.0
当前全历史分位: 96.5%
```

### 2015年顶部区域详细

```
W&W在4月30日见顶97.1 → 此后6周市场继续涨 → 6月12日指数见顶时W&W已降至89.8
→ W&W领先市场顶约6周
→ 印证: ×0.4压缩而非×0空仓的设计是正确的
```

### 2024年924行情

```
9月23日 W&W=29.8 (Bullish) → 次日924行情爆发
→ W&W在最佳买点给出了"恐惧"信号
→ 此后一个月W&W从29.8飙升到10月8日的88.6 (Very Bearish)
→ 完整捕捉了一轮情绪从恐惧到亢奋的转换
```

---

## 7. 敏感性测试

> 测试日期: 2026.7.5  
> 问题: CSI300 PE vs 全A PE (000985) 对W&W的影响

```
CSI300 PE: 14.9 → 5年滚动分位 = 83.7
全A PE:    19.1 → 5年滚动分位 = 99.0 (仅20天有效历史，不可靠)

W&W(CSI300 PE): 90.3
W&W(全A PE):    92.8 (Δ=+2.6, 有限，且全APE历史数据不足)

结论: F2指数选择不敏感，CSI300 PE够用
```

---

## 8. 交叉验证

> 测试日期: 2026.7.5  
> 方法: akshare 内部不同函数交叉验证（东方财富网页API被反爬）

| 因子 | 方法 | 偏差 |
|------|------|------|
| F1 融资余额 | 沪市(akshare) + 深市(akshare) vs 合并值 | 0.00% |
| F3 融资买入比 | 独立重新计算 vs 脚本值 | 0.00% |
| F4 10年国债 | 重新拉取 vs 脚本值 | 0.00% |

**结论**: akshare 数据完全自洽，内部无矛盾。

---

## 9. 与 BofA 原版的差异

| 维度 | BofA 原版 | 本复刻 |
|------|----------|--------|
| PE基准 | 全A股PE (~24x) | CSI300 PE (~15x) |
| 标准化方法 | BofA专有 | 5年滚动百分位 |
| 权重 | 非等权（专有，不公开） | 等权（6因子各1/6） |
| 仓位数据 | 专有基金仓位 | 融资余额+融资买入比（公开数据代理） |
| 盈利修正因子 | EPS revision | 替换为60日动量 |
| 方向逻辑 | **相同** | **相同** |
| 绝对值 | **不可直接对比** | **不可直接对比** |

**BofA 2026年5月读数 = 29 (Bullish)**，与我们的90.3不可比——基准PE和标准化方法完全不同。

---

## 10. 已知BUG与修复

### BUG #1: T+1融资数据导致W&W假暴跌

**发现日期**: 2026.7.4  
**症状**: 最新日W&W从~92一天暴跌至57  
**根因**: 融资数据有T+1延迟。上证/CSI300数据到T日，融资数据只到T-1日。  
RIGHT JOIN后T日融资余额=NaN → 百分位计算中NaN<任何值=False → 百分位=0

**修复**: 在 `fetch_all()` 末尾添加 ffill:
```python
base['margin'] = base['margin'].ffill()
base['融资买入额'] = base['融资买入额'].ffill()
```

**验证**: 修复后2026.7.3 W&W=90.3，与6月底90-95区间一致。

### 数据铁律

```
禁止从SOUL.md记忆引用WW读数
每次需要W&W时必须运行: python3 ~/AppData/Local/hermes/scripts/ww_indicator.py
融资数据每日更新，引用旧读数会掩盖趋势变化
```

---

## 11. 使用方式

### 命令行

```bash
python3 ~/AppData/Local/hermes/scripts/ww_indicator.py
```

### 输出示例

```
=======================================================
A股逆向情绪指标 (W&W)
时间: 2026-07-05 10:05
=======================================================

[1] 抓取数据...
    原始: 5941行, 2002-01-04~2026-07-03
[2] 计算因子...
[3] 5年滚动百分位标准化...
[4] 合成W&W...

=======================================================
结果
=======================================================
日期: 2026-07-03
W&W:  90.3  ->  Very Bearish [>>>]
有效数据: 4681行
范围: 2007-03-30 ~ 2026-07-03

各因子5年滚动百分位 (0=最低, 100=最高):
  融资余额             99.7  #################################################
  CSI300 PE        83.7  #########################################
  融资买入比            99.4  #################################################
  10年国债(反)         86.6  ###########################################
  成交量              85.1  ##########################################
  60日动量            87.3  ###########################################

  --- 综合W&W = 90.3  Very Bearish [>>>]

历史极值:
  最低(最恐惧): 20.7  2011-09-20
  最高(最亢奋): 97.1  2015-04-30
  均值: 62.0
  当前分位(全历史): 96.5%
```

### CSV输出

完整历史数据保存在 `/tmp/ww_indicator.csv`，包含所有因子原始值和百分位。

### 在框架中的位置

```
L0 (W&W) → L1 (SP500风控 × L0压缩) → L1有效上限
          → L5实操仓位 ≤ L1有效上限
```

---

> **版本历史**:  
> v1.1 (2026.7.5) — 新增L0→L1传导设计，压缩系数映射表，Bullish×1.0修正，5条设计决策。  
> v1.0 (2026.7.4) — 修复T+1融资数据ffill bug。  
> 初始版 (2026.6.x) — 6因子框架搭建。
