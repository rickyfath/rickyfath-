"""
hermes_finance.py — 在 Hermes 环境运行（akshare 可用）
拉取一只股票的完整财务数据，输出 JSON 到共享目录

用法（在 Hermes 里跑）:
  python hermes_finance.py 688008
  python hermes_finance.py 300395

输出:
  C:/Users/Administrator/Desktop/股票库/分析记录/{code}_finance.json
  Claude Code 自动读取
"""

import json
import sys
import os
from datetime import date

OUTPUT_DIR = r"C:\Users\Administrator\Desktop\股票库\分析记录"


def fetch_all(code: str):
    """拉取一只股票的全部财务数据"""
    result = {
        "code": code,
        "date": date.today().isoformat(),
        "source": "akshare",
        "data": {}
    }

    # === 利润表（8季度单季拆解）===
    try:
        import akshare as ak
        # THS 按报告期 — 每个报告期为年初至今累积值
        # 列序固定: [0]报告期 [1]归母净利润 [3]扣非净利润 [5]营业总收入 [13]销售毛利率
        pl = ak.stock_financial_abstract_ths(symbol=code, indicator='按报告期')
        if pl is not None and not pl.empty:
            cols = list(pl.columns)
            # 用索引定位，避免中文列名 Unicode 归一化匹配失败
            IDX_PERIOD = 0
            IDX_NP = 1       # 归母净利润
            IDX_DEDUCT = 3   # 扣非净利润
            IDX_REVENUE = 5  # 营业总收入
            IDX_GPM = 13     # 销售毛利率

            def _yi(val):
                """解析 THS 金额 -> float(亿元)"""
                s = str(val)
                if '亿' in s:
                    return float(s.replace('亿', ''))
                if '万' in s:
                    return float(s.replace('万', '')) / 10000
                try:
                    return float(s)
                except ValueError:
                    return 0.0

            def _pct(val):
                """解析百分比 -> float"""
                try:
                    return float(str(val).replace('%', ''))
                except ValueError:
                    return 0.0

            raw_rows = []
            for _, row in pl.iterrows():
                raw_rows.append({
                    "period": str(row.iloc[IDX_PERIOD]),
                    "revenue_cum": _yi(row.iloc[IDX_REVENUE]),
                    "np_cum": _yi(row.iloc[IDX_NP]),
                    "deduct_cum": _yi(row.iloc[IDX_DEDUCT]),
                    "gpm": _pct(row.iloc[IDX_GPM]),
                })

            # 累积值 -> 单季值
            quarterly = []
            prev = None
            for r in raw_rows:
                p = r["period"]
                if len(p) < 7:
                    continue
                month = p[5:7]
                if month not in ('03', '06', '09', '12'):
                    continue
                sq = {"period": p, "gpm": round(r["gpm"], 2)}
                if prev and prev["period"][:4] == p[:4]:
                    sq["revenue"] = round(r["revenue_cum"] - prev["revenue_cum"], 2)
                    sq["net_profit"] = round(r["np_cum"] - prev["np_cum"], 2)
                    sq["deduct_np"] = round(r["deduct_cum"] - prev["deduct_cum"], 2)
                else:
                    sq["revenue"] = round(r["revenue_cum"], 2)
                    sq["net_profit"] = round(r["np_cum"], 2)
                    sq["deduct_np"] = round(r["deduct_cum"], 2)
                quarterly.append(sq)
                prev = r

            result["data"]["quarters"] = quarterly[-8:]
            result["data"]["quarters_count"] = len(result["data"]["quarters"])
    except Exception as e:
        result["data"]["quarters_error"] = str(e)[:200]

    # === 资产负债表 ===
    try:
        import akshare as ak
        bs = ak.stock_balance_sheet_by_report_em(symbol=code)
        if bs is not None and not bs.empty:
            latest = bs.iloc[-1]
            result["data"]["balance"] = {
                "period": str(latest.get("REPORT_DATE", "")),
                "total_assets": str(latest.get("TOTAL_ASSETS", "")),
                "total_equity": str(latest.get("TOTAL_EQUITY", "")),
                "goodwill": str(latest.get("GOODWILL", "")),
                "inventory": str(latest.get("INVENTORY", "")),
                "accounts_receivable": str(latest.get("ACCOUNTS_RECEIVABLE", "")),
                "accounts_payable": str(latest.get("ACCOUNTS_PAYABLE", "")),
                "short_borrow": str(latest.get("SHORT_BORROW", "")),
                "long_borrow": str(latest.get("LONG_BORROW", "")),
                "bonds_payable": str(latest.get("BONDS_PAYABLE", "")),
                "contract_liability": str(latest.get("CONTRACT_LIABILITY", "")),
                "fixed_assets": str(latest.get("FIXED_ASSETS", "")),
                "construction_in_progress": str(latest.get("CONSTRUCTION_IN_PROGRESS", "")),
            }
    except Exception as e:
        result["data"]["balance_error"] = str(e)[:200]

    # === 现金流表 ===
    try:
        import akshare as ak
        cf = ak.stock_cash_flow_sheet_by_report_em(symbol=code)
        if cf is not None and not cf.empty:
            latest = cf.iloc[-1]
            result["data"]["cashflow"] = {
                "period": str(latest.get("REPORT_DATE", "")),
                "cfo": str(latest.get("NETCASH_OPERATE", "")),
                "capex": str(latest.get("PURCHASE_FIXED_ASSETS", "")),
            }
    except Exception as e:
        result["data"]["cashflow_error"] = str(e)[:200]

    # === 关键指标计算 ===
    try:
        calc_metrics(result)
    except Exception as e:
        result["data"]["metrics_error"] = str(e)[:200]

    return result


def calc_metrics(r):
    """从原始数据计算关键指标"""
    d = r["data"]

    # NonRec% — 从利润表最近4季算
    if "quarters" in d and len(d["quarters"]) >= 4:
        recent4 = d["quarters"][-4:]
        total_np = sum(float(q["net_profit"]) for q in recent4 if q.get("net_profit") is not None)
        total_deduct = sum(float(q["deduct_np"]) for q in recent4 if q.get("deduct_np") is not None)
        if total_np != 0:
            nonrec = total_np - total_deduct
            d["nonrec_pct"] = round(abs(nonrec) / abs(total_np) * 100, 1)
            d["nonrec_detail"] = f"归母{total_np:.2f}亿 - 扣非{total_deduct:.2f}亿 = NonRec {nonrec:.2f}亿"
        else:
            d["nonrec_pct"] = None

    # ROE — 从净利润/净资产
    if "balance" in d and "quarters" in d:
        try:
            equity = float(d["balance"].get("total_equity", 0))
            recent4_np = sum(float(q["net_profit"]) for q in d["quarters"][-4:] if q.get("net_profit") is not None)
            if equity > 0:
                d["roe_annualized_pct"] = round(recent4_np / equity * 100, 1)
        except (ValueError, TypeError):
            pass

    # 商誉/净资产
    if "balance" in d:
        try:
            equity = float(d["balance"].get("total_equity", 0))
            goodwill = float(d["balance"].get("goodwill", 0))
            if equity > 0:
                d["goodwill_to_equity_pct"] = round(goodwill / equity * 100, 1)
        except (ValueError, TypeError):
            pass

    # 有息负债/净资产
    if "balance" in d:
        try:
            equity = float(d["balance"].get("total_equity", 0))
            short = float(d["balance"].get("short_borrow", 0))
            long_b = float(d["balance"].get("long_borrow", 0))
            bonds = float(d["balance"].get("bonds_payable", 0))
            debt = short + long_b + bonds
            if equity > 0:
                d["debt_to_equity_pct"] = round(debt / equity * 100, 1)
        except (ValueError, TypeError):
            pass

    # CFO/NP
    if "cashflow" in d and "quarters" in d:
        try:
            cfo = float(d["cashflow"].get("cfo", 0))
            recent4_np = sum(float(q["net_profit"]) for q in d["quarters"][-4:] if q.get("net_profit") is not None)
            if recent4_np != 0:
                d["cfo_to_np"] = round(cfo / recent4_np, 2)
        except (ValueError, TypeError):
            pass

    # 营收增速 YoY
    if "quarters" in d and len(d["quarters"]) >= 5:
        try:
            curr = float(d["quarters"][-1]["revenue"])
            prev = float(d["quarters"][-5]["revenue"])
            if prev and prev != 0:
                d["revenue_yoy_pct"] = round((curr - prev) / prev * 100, 1)
        except (ValueError, TypeError, ZeroDivisionError):
            pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python hermes_finance.py <股票代码>")
        sys.exit(1)

    code = sys.argv[1].strip().replace(".SZ", "").replace(".SH", "").replace(".BJ", "")
    code = code.zfill(6)

    print(f"正在拉取 {code} 财务数据...")
    report = fetch_all(code)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, f"{code}_finance.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    print(f"完成 -> {out_path}")

    d = report.get("data", {})
    print(f"\n===== 摘要 =====")
    if "quarters" in d:
        print(f"季度数据: {d['quarters_count']} 季")
    n = d.get("nonrec_pct")
    if n is not None:
        tag = ">80% ELIM" if n > 80 else ("10-30% WARN" if n > 10 else "OK")
        print(f"NonRec%: {n}% {tag}")
    g = d.get("goodwill_to_equity_pct")
    if g is not None:
        print(f"Goodwill/Equity: {g}% {'>30%!' if g > 30 else 'OK'}")
    dt = d.get("debt_to_equity_pct")
    if dt is not None:
        print(f"Debt/Equity: {dt}%")
    roe = d.get("roe_annualized_pct")
    if roe is not None:
        print(f"ROE(annualized): {roe}%")
    cn = d.get("cfo_to_np")
    if cn is not None:
        print(f"CFO/NP: {cn} {'<0.5!' if cn < 0.5 else 'OK'}")
    ry = d.get("revenue_yoy_pct")
    if ry is not None:
        print(f"Revenue YoY: {ry}%")
    if "balance" in d:
        b = d["balance"]
        print(f"AR: {b.get('accounts_receivable','?')} | AP: {b.get('accounts_payable','?')}")
        print(f"Contract Liab: {b.get('contract_liability','?')}")
