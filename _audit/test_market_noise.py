#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Does the macro quality gate drop tape chatter but keep real macro news?"""
import importlib.util
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location(
    "nu", r"F:\MyRepository\PortfolioNewsUpdater\news_updater.py")
nu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nu)

# (headline, True = should be DROPPED as noise)
CASES = [
    # --- the user's examples and the rows that run actually stored ---
    ("US three major stock index futures extend gains, all up", True),
    ("US 10-year Treasury yield retreats after spiking.", True),
    ("Nasdaq 100 futures extend gains to 1%.", True),
    ("US stock fear index VIX falls 1.49 points.", True),
    ("Short-term Treasuries lead losses; traders raise Fed rate hike bets", True),
    ("Spot silver rises 1.34%, hitting a new intraday high.", True),
    ("Market fully expects two Fed rate hikes by year-end.", True),
    ("Traders expect about 90% odds of a Fed rate hike next week.", True),
    ("US nuclear power stock Oklo falls 4.1% premarket.", True),
    ("SK Hynix up over 2% in U.S. premarket, last at $192.12", True),
    ("After inflation data, euro extends decline against dollar, last at $1.1576, down 0.32%; dollar index extends gains after inflation data, last up 0.19% at 99.27", True),
    ("【美股收盘：三大股指集体收跌】道指跌0.60%，标普500指数跌0.58%，纳指跌0.65%。", True),
    ("纳斯达克：8 月下旬空头头寸较 8 月中旬上升 2.4%。", True),
    ("美国财政部8周期国库券中标利率3.845%", True),
    ("美国财政部表示：在9月10日进行了价值51.87亿美元的10至20年期美国国债流动性回购操作", True),
    ("【两年期美债收益率涨超13个基点，10年期美债收益率创2023年以来新高】", True),
    ("【美元指数10日上涨】衡量美元对六种主要货币的美元指数当天上涨0.23%", True),
    ("美联储隔夜逆回购协议（RRP）周四使用规模为47.36亿美元", True),
    ("新兴市场外汇指数因美联储加息预期，出现 7 月以来最大单日跌幅。", True),
    # --- MUST BE KEPT: real macro / policy news ---
    ("US August CPI rises 3.4% YoY; traders raise Fed rate-hike odds", False),
    ("Fed whisperer Nick Timiraos: Higher-than-expected inflation means...", False),
    ("央行：继续实施好适度宽松的货币政策 完善利率体系", False),
    ("六大行下调存款利率 降息预期升温", False),
    ("助贷新规施行超9个月，民营银行助贷缩表进入深水区", False),
    ("9部门联合发布意见促进县域消费 支持个人消费贷款享受财政贴息", False),
    ("财政部将发行3000亿元特别国债", False),
    ("CSRC releases Futures Company Supervision Measures", False),
    ("CSRC seriously investigates *ST Zhuoran financial fraud", False),
    ("State Council: allocate education resources based on school-age population", False),
    ("美国财政部宣布对伊朗相关实体实施新制裁", False),
    ("中国央行下调LPR 应对美联储加息", False),
    ("【纳斯达克中国金龙指数收跌1.75%】", False),
]
fail = 0
for text, want_drop in CASES:
    got = nu.market_noise(text)
    ok = got == want_drop
    if not ok:
        fail += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {'DROP' if got else 'KEEP':4s} "
          f"(want {'DROP' if want_drop else 'KEEP':4s}) {text[:60]}")
print()
print(f"RESULT: {'ALL PASSED' if fail == 0 else str(fail) + ' FAILED'}")
sys.exit(1 if fail else 0)
