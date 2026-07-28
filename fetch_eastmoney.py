import requests, json

headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}

apis = {
    "CPI": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_ECONOMY_CPI&columns=REPORT_DATE,TIME,NATIONAL_SAME,NATIONAL_BASE,NATIONAL_SEQUENTIAL&pageSize=5&sortColumns=REPORT_DATE&sortTypes=-1&source=WEB&client=WEB",
    "PPI": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_ECONOMY_PPI&columns=REPORT_DATE,TIME,NATIONAL_SAME,NATIONAL_BASE&pageSize=5&sortColumns=REPORT_DATE&sortTypes=-1&source=WEB&client=WEB",
    "PMI": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_ECONOMY_PMI&columns=REPORT_DATE,TIME,MAKE_INDEX,NMAKE_INDEX&pageSize=5&sortColumns=REPORT_DATE&sortTypes=-1&source=WEB&client=WEB",
    "M2": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_ECONOMY_MONEYSUPPLY&columns=REPORT_DATE,TIME,M2,M1,M0&pageSize=5&sortColumns=REPORT_DATE&sortTypes=-1&source=WEB&client=WEB",
    "社融": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_ECONOMY_SOCIALFINANCING&columns=REPORT_DATE,TIME,SOCIAL_FINANCING&pageSize=5&sortColumns=REPORT_DATE&sortTypes=-1&source=WEB&client=WEB",
    "工业增加值": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_ECONOMY_INDUSTRIAL_ADDEDVALUE&columns=REPORT_DATE,TIME,NATIONAL_SAME&pageSize=5&sortColumns=REPORT_DATE&sortTypes=-1&source=WEB&client=WEB",
}

for name, url in apis.items():
    try:
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        if data.get('success') and data.get('result') and data['result'].get('data'):
            print(f"\n--- {name} ---")
            for item in data['result']['data'][:4]:
                vals = []
                for k, v in item.items():
                    if k != 'REPORT_DATE' and v is not None:
                        vals.append(f"{k}={v}")
                print(f"  {item.get('REPORT_DATE','?')} | {item.get('TIME','?')} | {'  '.join(vals)}")
        else:
            print(f"\n--- {name}: no data ---")
            print(f"  Response: {str(data)[:200]}")
    except Exception as e:
        print(f"\n--- {name} ERROR: {e} ---")
