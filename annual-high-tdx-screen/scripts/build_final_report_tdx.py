# -*- coding: utf-8 -*-
"""今日最终筛选报告：TDX 供数的形态筛选 + baostock 公司质地 两层合并。

与 build_final_report.py 的区别：
  · 输入是 daily_tdx.py 出品的 xlsx（列名更精简），不再依赖全市场数据湖跑批
  · 机构一致预期/点评改为脚本内当日快照，缺项留空不影响排名
  · 综合分口径保持一致：形态 40% + 质地 60%

用法：
  python scripts/build_final_report_tdx.py \
      --pool out/日常筛选_TDX供数_YYYYMMDD_HHMMSS.xlsx \
      --fund out/公司层面分析_YYYYMMDD_tdx.csv
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT_DIR = ROOT / "out"

W_FORM, W_QUALITY = 0.4, 0.6

# ------------------------------------------------- 机构一致预期 / 点评 / 结论（可选外部覆盖）
# 便携版：不再内置个股快照。若需注入机构一致预期与定性点评，
# 在 skill 根目录或当前工作目录放 analyst_notes.json，结构：
#   {"consensus": {"002415.SZ": {"rating": 4.7, "target": 46.1, "eps26": 1.97,
#                                   "eps27": 2.23, "eps28": 2.51, "rev20d": 0.66}},
#    "note":      {"002415.SZ": "...点评..."},
#    "conclusion": {"002415.SZ": "核心"}}
# 缺省为空，不影响排名（综合分=形态40%+质地60% 不依赖这些字段）。
import json as _json

def _load_notes():
    for cand in (ROOT / 'analyst_notes.json',
                 Path.cwd() / 'analyst_notes.json',
                 HERE / 'analyst_notes.json'):
        try:
            if cand.exists():
                return _json.loads(cand.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}

_NOTES = _load_notes()
CONSENSUS = _NOTES.get('consensus', {})
NOTE = _NOTES.get('note', {})
CONCLUSION = _NOTES.get('conclusion', {})


def build(pool_xlsx: Path, fund_csv: Path, trade_date: str) -> Path:
    pool = pd.read_excel(pool_xlsx, engine="openpyxl")
    pool = pool[pool["本地形态通过"]].copy()
    pool["股票代码"] = pool["symbol"]
    fund = pd.read_csv(fund_csv)

    df = pool.merge(fund, on="股票代码", how="left", suffixes=("", "_y"))
    df["名称"] = df["名称"].fillna(df["股票代码"])
    df["行业"] = df.get("行业", "").fillna("")

    for col, key in [("机构评级", "rating"), ("机构目标价", "target"),
                     ("26E_EPS", "eps26"), ("27E_EPS", "eps27"),
                     ("28E_EPS", "eps28"), ("近20日EPS预期变动%", "rev20d")]:
        df[col] = df["股票代码"].map(lambda s: CONSENSUS.get(s, {}).get(key))
    df["目标价空间%"] = (df["机构目标价"] / df["最新价"] - 1) * 100
    df["26E_PE"] = df["最新价"] / df["26E_EPS"]
    df["27E增速%"] = (df["27E_EPS"] / df["26E_EPS"] - 1) * 100

    df["综合总分"] = (df["总分"] * W_FORM + df["质地总分"] * W_QUALITY).round(1)
    df = df.sort_values("综合总分", ascending=False).reset_index(drop=True)
    df["排名"] = range(1, len(df) + 1)
    df["结论"] = df["股票代码"].map(CONCLUSION).fillna("观察")
    df["公司层面点评"] = df["股票代码"].map(NOTE).fillna("")

    ren = {"总分": "形态总分", "30日箱宽": "30日箱宽", "现价/年高": "现价/年高",
           "振幅CV": "振幅变异系数", "日均成交(亿)": "日均成交(亿)"}
    df = df.rename(columns=ren)

    t1 = df[["排名", "股票代码", "名称", "行业", "结论", "综合总分", "形态总分", "质地总分",
             "最新价", "目标价空间%", "30日箱宽", "现价/年高", "公司层面点评"]]

    cols2 = ["排名", "股票代码", "名称", "质地总分", "评分口径", "最新报告期", "营收同比%", "净利同比%",
             "净利连续正增长期数", "ROE(杜邦)%", "净利率%", "现金流/净利润", "资产负债率%",
             "PE_TTM", "26E_PE", "27E增速%", "PB", "股息率%", "总市值(亿)",
             "机构评级", "机构目标价", "目标价空间%", "近20日EPS预期变动%"]
    cols2 = [c for c in cols2 if c in df.columns]
    t2 = df[cols2].rename(columns={"ROE(杜邦)%": "ROE(年化)%",
                                   "现金流/净利润": "经营现金流/净利润"})

    cols3 = ["排名", "股票代码", "名称", "形态总分", "等级", "日均成交(亿)", "30日箱宽",
             "振幅变异系数", "现价/年高", "起伏次数", "最新价"]
    cols3 = [c for c in cols3 if c in df.columns]
    t3 = df[cols3]

    t4 = pd.DataFrame([
        ["综合总分", f"形态总分×{W_FORM} + 质地总分×{W_QUALITY}",
         "形态通过的清一色 A 级，区分度主要在质地，故质地权重更高"],
        ["形态总分", "jianchashuju_local.py 现行硬指标（0-100），与全市场跑批同口径",
         "平滑度+年高位置+箱宽+量能+卫生过滤（连续趋势/异常振幅/趋势斜率）"],
        ["质地总分（通用口径）", "ROE年化25 / 营收增速20 / 净利增速18 / 成长连续性8 / 利润含金量12 / 净利率9 / 估值8",
         "适用于非银行；PE 为负（TTM 亏损）估值项直接记 0 分"],
        ["质地总分（银行口径）", "ROE年化25 / 净利增速22 / 营收增速10 / PB低估18 / 股息率15 / 杠杆克制10",
         "银行的资产负债率、毛利率、经营现金流无意义，必须换口径"],
        ["ROE年化", "baostock dupontROE 为年初至今累计值，按报告期年化",
         "一季报×4 / 半年报×2 / 三季报×4÷3 / 年报×1"],
        ["PIT 约束", "只采信 pubDate <= 基准日 的报表", "杜绝未来函数"],
        ["机构预期", "TDX F10 研报一致预期，取数日 2026-10-28", "覆盖 5/10 只，其余未取不影响排名"],
        ["数据基准", f"行情与技术形态截至 {trade_date} 收盘（TDX 前复权日线 300 根）",
         "财务为最新已披露期（多为 2026 中报/三季报）"],
        ["已知口径差", "TDX 前复权与本地数据湖前复权的除权基准不同",
         "有分红除权的个股 prox 会偏移，超阈需人工看图确认"],
    ], columns=["项目", "口径", "说明"])

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUT_DIR / f"最终筛选结果_{ts}.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        t1.to_excel(w, sheet_name="最终结论", index=False)
        t2.to_excel(w, sheet_name="公司层面", index=False)
        t3.to_excel(w, sheet_name="技术形态", index=False)
        t4.to_excel(w, sheet_name="评分口径", index=False)
        _beautify(w, {"最终结论": [6, 12, 12, 22, 20, 10, 10, 10, 10, 12, 10, 10, 96],
                      "公司层面": None, "技术形态": None, "评分口径": [22, 66, 56]})

    print("\n最终排名：")
    print(t1[["排名", "股票代码", "名称", "行业", "结论", "综合总分", "形态总分", "质地总分",
              "最新价", "目标价空间%"]].round(2).to_string(index=False))
    print(f"\n保存: {path}")
    return path


def _beautify(writer, widths: dict):
    head_fill = PatternFill("solid", fgColor="DDEBF7")
    for name, ws in writer.sheets.items():
        ws.freeze_panes = "A2"
        for c in ws[1]:
            c.font = Font(bold=True)
            c.fill = head_fill
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.row_dimensions[1].height = 30
        wl = widths.get(name)
        for i, col in enumerate(ws.iter_cols(min_row=1, max_row=1), start=1):
            letter = get_column_letter(i)
            if wl and i <= len(wl) and wl[i - 1]:
                ws.column_dimensions[letter].width = wl[i - 1]
            else:
                ws.column_dimensions[letter].width = max(11, min(22, len(str(col[0].value or "")) * 2.2))
        for row in ws.iter_rows(min_row=2):
            for c in row:
                if isinstance(c.value, str) and len(c.value) > 40:
                    c.alignment = Alignment(wrap_text=True, vertical="top")
            if name == "最终结论":
                ws.row_dimensions[row[0].row].height = 110


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", required=True)
    ap.add_argument("--fund", required=True)
    ap.add_argument("--trade-date", default=dt.date.today().strftime("%Y-%m-%d"))
    a = ap.parse_args()
    pool = Path(a.pool)
    if not pool.is_absolute():
        pool = ROOT / pool
    fund = Path(a.fund)
    if not fund.is_absolute():
        fund = ROOT / fund
    build(pool, fund, a.trade_date)


if __name__ == "__main__":
    main()
