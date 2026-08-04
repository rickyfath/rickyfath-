"""
debate_engine.py — Bull vs Bear 对抗辩论引擎
=============================================
读 data_bridge JSON → 多方空方交替辩论 → 输出分歧总结

用法:
  python debate_engine.py <代码>
  python debate_engine.py 688019
  python debate_engine.py 688019 --rounds 2

依赖: openai, DEEPSEEK_API_KEY 环境变量
输出: C:/Users/Administrator/Desktop/<代码>_debate.json + <代码>_debate.md
"""

import json, sys, os
from datetime import date
from openai import OpenAI

DESKTOP = r"C:\Users\Administrator\Desktop"
TODAY = date.today().isoformat()

BULL_SYSTEM = """你是一位**多方分析师**，任务是找到这只股票所有值得买入的理由。

规则:
1. 基于提供的数据，找到最强的3-5个看多论点
2. 每个论点必须引用具体数据（数字、趋势、比率）
3. 不要假装客观——你是多头，你的本职工作就是找买入理由
4. 如果有对方（空方）说过的话，针对性反驳
5. A股特有的多看：政策顺风、北向资金、产业趋势、国产替代"""

BEAR_SYSTEM = """你是一位**空方分析师**，任务是找到这只股票所有应该卖出的理由。

规则:
1. 基于提供的数据，找到最强的3-5个看空论点
2. 每个论点必须引用具体数据（数字、趋势、比率）
3. 不要假装客观——你是空头，你的本职工作就是找做空理由
4. 必须针对性攻击多方刚说的每一个核心论点
5. A股特有的多看：政策逆风、解禁减持、游资撤退、估值泡沫、T+1陷阱"""

JUDGE_SYSTEM = """你是**研究经理**。刚才多方和空方进行了辩论，现在请你做总结。

输出三个部分:

### 共识
多方和空方都同意的点是什么？

### 核心分歧
最关键的争执是什么？双方各用什么数据支撑？

### 证据强度
谁的论点更有说服力？为什么？如果数据有缺陷，指出来。"""


def load_bridge(code):
    path = os.path.join(DESKTOP, f"{code}_data_bridge.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Bridge not found: {path}\n请先运行: python data_bridge.py {code} --quick")
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def build_context(bridge):
    """从bridge提取辩论所需的所有数据"""
    meta = bridge.get("_meta", {})
    daily = bridge.get("preflight", {}).get("step_c", {}).get("daily", {})
    monthly = bridge.get("preflight", {}).get("step_c", {}).get("monthly", {})
    fin = bridge.get("preflight", {}).get("step_c", {}).get("financials", {})
    step_d = bridge.get("preflight", {}).get("step_d", {})
    quarters = bridge.get("quarterly", {}).get("quarters", [])
    step_a = bridge.get("preflight", {}).get("step_a", {})

    ctx = f"""股票: {meta.get('stock_name','')} ({meta.get('code','')})
行业: {meta.get('SW1','')} / {meta.get('SW2','')} / {meta.get('SW3','')}
L3判定: {step_d.get('L3_判定','')} — {step_d.get('L3_来源','')}
概念标签: {step_d.get('概念标签','')}

【技术面】
收盘: {daily.get('close')} | MA20: {daily.get('MA20')} | MA60: {daily.get('MA60')} | MA200: {daily.get('MA200')}
MA20斜率(5日): {daily.get('MA20_slope_pct')}% | 偏离MA20: {daily.get('deviation_from_MA20_pct')}%
ATR: {daily.get('ATR14')} ({daily.get('ATR_pct')}%)
5日涨跌: {daily.get('chg_5d_pct')}% | 20日涨跌: {daily.get('chg_20d_pct')}%
量比: {daily.get('volume_ratio_20d')} | 排列: {daily.get('alignment')} | 趋势: {daily.get('trend')}
月线ATH: {monthly.get('ATH')} | 距ATH: {monthly.get('distance_from_ATH_pct')}%

【财务】
GPM: {fin.get('GPM')}% | ROE: {fin.get('ROE')}% ({fin.get('ROE_note','')})
有息负债/权益: {fin.get('debt_to_equity_pct')}%
CFO/NP: {fin.get('cfo_to_np')} | NonRec%: {fin.get('nonrec_pct')}%
商誉/净资产: {fin.get('goodwill_to_equity_pct')}%
大股东质押: {fin.get('pledge_ratio_pct','未知')}%
审计: {fin.get('audit_opinion','需核实')}

【8季趋势】"""
    for q in quarters[-8:]:
        ctx += f"\n  {q.get('q','?')}: 营收{q.get('revenue','?')}亿 净利{q.get('net_profit','?')}亿 GPM{q.get('gpm','?')}%"

    ctx += f"""

【宏观】
W&W: {step_a.get('ww','?')} ({step_a.get('zone','?')})"""

    return ctx

def run_debate(code, max_rounds=1):
    bridge = load_bridge(code)
    ctx = build_context(bridge)
    stock_name = bridge.get("_meta", {}).get("stock_name", code)

    api_key = os.environ.get("DEEPSEEK_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY 未设置")
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
    model = "deepseek-v4-pro"

    print(f"  {stock_name} Bull vs Bear 辩论 (max {max_rounds}轮)")

    debate_log = []

    for rnd in range(max_rounds):
        # --- Bull ---
        bull_messages = [{"role": "system", "content": BULL_SYSTEM}]
        if debate_log:
            bear_last = debate_log[-1]["bear"]
            bull_messages.append({"role": "user", "content": f"空方刚说了:\n{bear_last}\n\n请反驳。\n\n数据:\n{ctx}"})
        else:
            bull_messages.append({"role": "user", "content": f"请分析这只股票的看多理由:\n\n{ctx}"})

        resp = client.chat.completions.create(model=model, messages=bull_messages, temperature=0.5)
        bull_arg = resp.choices[0].message.content
        print(f"  [Round {rnd+1}] Bull OK")

        # --- Bear ---
        bear_messages = [
            {"role": "system", "content": BEAR_SYSTEM},
            {"role": "user", "content": f"多方刚说了:\n{bull_arg}\n\n请逐一反驳每个核心论点。\n\n数据:\n{ctx}"}
        ]
        resp = client.chat.completions.create(model=model, messages=bear_messages, temperature=0.5)
        bear_arg = resp.choices[0].message.content
        print(f"  [Round {rnd+1}] Bear OK")

        debate_log.append({"round": rnd+1, "bull": bull_arg, "bear": bear_arg})

    # --- Judge ---
    debate_text = "\n\n".join(
        f"## 第{r['round']}轮\n### 多方:\n{r['bull']}\n### 空方:\n{r['bear']}"
        for r in debate_log
    )
    judge_resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": f"以下是多方和空方的完整辩论。请做总结:\n\n{debate_text}"}
        ],
        temperature=0.3
    )
    summary = judge_resp.choices[0].message.content
    print(f"  Judge OK")

    # Output
    output = {
        "_meta": {"code": code, "stock_name": stock_name, "date": TODAY, "rounds": max_rounds},
        "debate": debate_log,
        "summary": summary
    }

    json_path = os.path.join(DESKTOP, f"{code}_debate.json")
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    md_path = os.path.join(DESKTOP, f"{code}_debate.md")
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(f"# {stock_name} Bull vs Bear 辩论\n\n")
        f.write(f"日期: {TODAY} | 轮数: {max_rounds}\n\n---\n\n")
        for r in debate_log:
            f.write(f"## 第{r['round']}轮\n\n### 🔴 多方\n{r['bull']}\n\n### 🔵 空方\n{r['bear']}\n\n---\n\n")
        f.write(f"## ⚖️ 研究经理总结\n\n{summary}\n")

    print(f"  → {os.path.basename(json_path)}")
    print(f"  → {os.path.basename(md_path)}")

    return output

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python debate_engine.py <代码> [--rounds N]")
        sys.exit(1)

    code = sys.argv[1].strip().zfill(6)
    rounds = 1
    if "--rounds" in sys.argv:
        idx = sys.argv.index("--rounds")
        if idx+1 < len(sys.argv):
            rounds = int(sys.argv[idx+1])

    run_debate(code, rounds)
