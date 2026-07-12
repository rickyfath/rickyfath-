---
name: l1-l4-framework
description: A股投资决策流水线 v3.1(2026.7.4) — 5步标准流程+自动前置L1-L3。每步对应一个核心Skill，不可跳步。
triggers:
  - 框架分析
  - 完整分析
  - 能不能买
  - 选股
  - 择股
  - 怎么看
version: 3.1.0
---

# A股投资决策流水线 v3.1

**总控角色：不自己做计算，只做路由。确保用户按 L1→L2→L3→L4→L5 的顺序走，不可跳步。**

每次触发"能不能买"/"框架分析"时，先跑 L1-L3 获取仓位上限、方向约束、时机信号，再把过滤后的范围送入 L4 小齐股市框架。L2 说不碰的东西，L4 不能扫。L3 说 T4 回避的赛道，L4 不能买。

```
  [1] 仓位上限  →  sp500-risk-control
      回答: 现在最多放多少仓位？
      输出: L1风险级别 + 仓位上限%

  [2] 宏观象限  →  six-quadrant-model
      回答: 宏观支持什么方向？不碰什么？
      输出: 滞胀/通缩/扩张 + 有效仓位上限 + 不碰清单

  [3] 产业择时  →  l3-industry-cycle-timing
      回答: 赛道在产业周期什么位置？该不该买？
      输出: T1-T4信号 + 分歧 + 9-Bucket

  [4] 选股执行  →  小齐股市框架 (v3.3)
      回答: 赛道里具体买哪只？怎么验证？
      含: Q1报告5级分数→生命周期标签→产业链元数据→关键词初筛→盈利传导性→
          全量扫描→7步法→PE陷阱→残差标签→RS/量价/ATR→质量闸门

  [5] 交易执行  →  a-share-trading-plan
      回答: 什么价位买？多少仓位？怎么止损？
      含: MA20择时 + CVaR仓位 + 卖出双确认v3.0 + 分批建仓
```

## 使用规则

1. 不可跳步。L1→L2→L3→L4→L5。
2. 框架是风险预算分配器，不是买卖信号。
3. 每层必须先拉最新数据，不能用记忆旧数据。
4. L2否定判断(不碰什么) > 肯定判断(该买什么)。
5. L2给方向、L3给时机，打架时信L3。
6. **L4→L5仓位传导：L4出战略上限，L5做环境防守缩放，L5实操仓位 ≤ L4战略上限。**
7. **Q1分数 ≠ price-in判断。** 详见小齐股市框架 Stage 6A-prime。
8. **v4.4 自动前置规则（2026.7.4）：** 用户说"能不能买XX""分析XX""扫描XX"时，默认先执行 L1(sp500-risk-control)→L2(six-quadrant-model)→L3(l3-industry-cycle-timing) 获取仓位上限+宏观方向+赛道择时约束，再进入L4。跳过L1-L3需用户明确说"跳过L1-L3直接看XX"。L1-L3短时间结论相同是常态，仍每次拉最新数据做前置校验。

```
score >= 0  AND  PE分位 < 80%   →  可买入（双确认：基本面支持+未price-in）
score >= 0  AND  PE分位 > 90%   →  持有不加仓（基本面OK，市场抢跑了）
score >= 0  AND  PE分位 80-90%  →  警惕，缩小仓位上限
score < 0   AND  PE分位 > 90%   →  回避（过热+透支）
```

Q1报告分数基于Q1季报（滞后约3个月），每次择股前须拉取实际PE分位交叉验证。详见 sector-full-sweep Stage 6A-prime。

**v3.0 新增维度（6.30）：** 详见 小齐股市框架 v3.0。

## 快速参考：用户意图 → Skill

```
"仓位上限多少"       → sp500-risk-control
"宏观支持什么"       → six-quadrant-model
"XX赛道该不该买"     → l3-industry-cycle-timing
"择股/帮我选股"      → 小齐股市框架
"明天怎么操作"       → a-share-trading-plan
"拉数据"             → a-share-data (基础设施)
"分析研报"           → pdf-analysis (基础设施)
```

## 参考Skill（不参与流程，查阅用）

bofa-fms-framework, factor-investing-theory, manufacturing-capacity-cycle,
**参考文件:** `references/a-share-hell-time-catalog.md` — A股22轮地狱时段完整分类(S/A/B/X)。用于仓位决策时对照历史崩盘模式。
ww-indicator, ubs-china-ai-crowdedness, morgan-stanley-midyear-2026,
q1-2026-earnings, sellside-research-digest, gayed-bilello-leverage-ma,
industry-cycle-framework