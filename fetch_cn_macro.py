import akshare as ak
import pandas as pd

print("="*60)
print("中国4月宏观数据 - 六象限模型更新")
print("="*60)

# 1. CPI
try:
    cpi = ak.macro_china_cpi_monthly()
    cpi_last = cpi.tail(3)
    print("\n--- CPI ---")
    for _, row in cpi_last.iterrows():
        print(f"  {row.iloc[0]}: 同比={row.iloc[1]:.1f}%  环比={row.iloc[2]:.1f}%")
except Exception as e:
    print(f"CPI ERROR: {e}")

# 2. PPI
try:
    ppi = ak.macro_china_ppi_yearly()
    ppi_last = ppi.tail(3)
    print("\n--- PPI ---")
    for _, row in ppi_last.iterrows():
        print(f"  {row.iloc[0]}: {row.iloc[1]:.1f}%")
except Exception as e:
    print(f"PPI ERROR: {e}")

# 3. PMI
try:
    pmi = ak.macro_china_pmi()
    pmi_last = pmi.tail(3)
    print("\n--- PMI ---")
    for _, row in pmi_last.iterrows():
        print(f"  {row.iloc[0]}: 制造业={row.iloc[1]:.1f}  非制造业={row.iloc[2]:.1f}")
except Exception as e:
    print(f"PMI ERROR: {e}")

# 4. M2 & 社融
try:
    m2 = ak.macro_china_money_supply()
    m2_last = m2.tail(3)
    print("\n--- 货币供应 ---")
    for _, row in m2_last.iterrows():
        print(f"  {row.iloc[0]}: M2同比={row.iloc[1]:.1f}%  M1同比={row.iloc[2]:.1f}%")
except Exception as e:
    print(f"M2 ERROR: {e}")

# 5. 社融
try:
    sf = ak.macro_china_shrzgm()
    sf_last = sf.tail(3)
    print("\n--- 社会融资规模 ---")
    for _, row in sf_last.iterrows():
        print(f"  {row.iloc[0]}: {row.iloc[1]:.1f}亿元")
except Exception as e:
    print(f"社融 ERROR: {e}")

# 6. PMI购进价格 (成本冲击代理)
try:
    pmi_detail = ak.macro_china_pmi_detail()
    print("\n--- PMI分项(最新) ---")
    last = pmi_detail.tail(1)
    for _, row in last.iterrows():
        for col in pmi_detail.columns:
            print(f"  {col}: {row[col]}")
except Exception as e:
    print(f"PMI Detail ERROR: {e}")
