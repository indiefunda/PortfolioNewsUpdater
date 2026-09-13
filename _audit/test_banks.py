#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Confirm the macro/global/ticker ROUTER puts each headline in one section."""
import importlib.util
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location(
    "nu", r"F:\MyRepository\PortfolioNewsUpdater\news_updater.py")
nu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nu)

CASES = [
    # (text, expected tier, label)
    ("ECB raises rates again by 25bp", "global", "ECB rate hike"),
    # Regression: this exact headline reached the CHINA MACRO section because
    # it matched the generic rate-move vocabulary twice.
    ("ECB hikes 25bp, Lagarde flags sticky inflation and continued tightening",
     "global", "ECB (real headline)"),
    ("欧洲央行宣布加息25个基点", "global", "ECB (zh)"),
    ("日本央行维持利率不变", "global", "BoJ"),
    ("英国央行加息预期升温", "global", "BoE"),
    ("美联储理事沃勒：如果通胀数据偏热 我会考虑支持加息", "global", "Fed Waller"),
    ("8月非农就业远超预期 现货白银跌幅扩大至3%", "global", "US payrolls"),
    ("【美股盘前要闻速递】美股三大股指期货齐跌", "global", "US premarket"),
    ("央行：继续实施好适度宽松的货币政策 完善利率体系", "macro", "PBoC policy"),
    ("六大行下调存款利率 降息预期升温", "macro", "China rate cut"),
    ("助贷新规落地 消费金融公司承压", "macro", "assisted-loan reg"),
    ("财政部将发行3000亿元特别国债", "macro", "special T-bonds"),
    # These reached CHINA MACRO in the live run before the subject-based veto.
    ("美国财政部8周期国库券中标利率3.845% 4周期国库券中标利率3.775%",
     "global", "US Treasury auction"),
    ("美国财政部确认今日长债回购金额最高为60亿美元。", "global", "US Treasury buyback"),
    ("【华尔街警告美国AI债务扩张对美国债形成压力】", "global", "Wall Street / US debt"),
    ("【纳斯达克中国金龙指数收跌1.75%】", "macro", "China ADR index (China story)"),
    ("央行降息 美联储加息预期降温 人民币汇率走强", "macro", "China cut + Fed mention"),
    ("中国央行下调LPR 应对美联储加息", "macro", "China LPR + Fed"),
    ("LexinFintech Holdings Ltd.: Citigroup lowers rating to Neutral", "ticker", "LX analyst"),
    ("Huize (HUIZ) Q2 2026 Earnings Call", "ticker", "HUIZ earnings"),
]
fail = 0
for text, want, label in CASES:
    got = nu.news_tier(text)
    ok = got == want
    if not ok:
        fail += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {got:7s} (want {want:7s}) {label:24s} {text[:38]}")
print(f"\nRESULT: {'ALL PASSED' if fail == 0 else str(fail) + ' FAILED'}")
sys.exit(1 if fail else 0)
