# -*- coding: utf-8 -*-
"""公司层面分析：在形态筛选之上叠加基本面质地评估。

与 screen_3tier.py 只做「营收/净利/ROE 门槛判断」不同，本脚本做**分层打分**：

  A. 成长质量   近 8 期营收同比、净利同比、连续正增长期数、增速趋势（是加速还是减速）
  B. 盈利能力   ROE(杜邦)、净利率、毛利率、ROA
  C. 盈利含金量 经营现金流/净利润（CFO/NP）、现金流/营收
  D. 财务稳健   资产负债率、权益乘数、流动比率
  E. 估值       PE(TTM)、PB、股息率、市值

关键设计：**银行单独一套口径**。
  银行的 CFO/净利、资产负债率、毛利率毫无意义（负债经营、无毛利概念），
  用通用模板会把 4 家银行全判成垃圾。银行改看 ROE / 净利增速 / PB / 股息率 /
  权益乘数（杠杆是否克制）。

数据严格 PIT：只采信 pubDate <= as_of 的报表，杜绝未来函数。

用法：
  python scripts/company_analysis.py --symbols 600926.SH,600919.SH --as-of 2026-10-28
  python scripts/company_analysis.py --from-pool out/波动率25%以下的股票池_xxx.xlsx
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT_DIR = ROOT / "out"

BANK_HINT = ("货币金融", "银行")


def is_bank(industry: str | None, name: str | None = "") -> bool:
    s = f"{industry or ''}{name or ''}"
    return any(h in s for h in BANK_HINT)


# ---------------------------------------------------------------- baostock 取数
def _rows(rs):
    out = []
    while rs.next():
        out.append(rs.get_row_data())
    return rs.fields, out


def _f(fields, row, key, cast=float):
    if key not in fields:
        return np.nan
    v = row[fields.index(key)]
    if v in ("", None, "--"):
        return np.nan
    try:
        return cast(v)
    except (TypeError, ValueError):
        return np.nan


def bs_last(bs, qfunc, code: str, quarters, as_of: str, keys: dict):
    """取满足 PIT 的最新一期，返回 (statDate, {别名: 值})。"""
    for (y, q) in quarters:
        f, rows = _rows(qfunc(code=code, year=y, quarter=q))
        if not rows:
            continue
        row = rows[-1]
        pub = row[f.index("pubDate")] if "pubDate" in f else ""
        if pub and str(pub) > as_of:
            continue
        stat = row[f.index("statDate")] if "statDate" in f else f"{y}-{q}"
        vals = {alias: _f(f, row, src) for alias, src in keys.items()}
        return stat, vals
    return None, {}


def bs_series(bs, qfunc, code: str, quarters, as_of: str, key: str):
    """取多期序列（由近及远），返回 [(statDate, value)]，PIT 过滤。"""
    out = []
    for (y, q) in quarters:
        f, rows = _rows(qfunc(code=code, year=y, quarter=q))
        if not rows:
            continue
        row = rows[-1]
        pub = row[f.index("pubDate")] if "pubDate" in f else ""
        if pub and str(pub) > as_of:
            continue
        stat = row[f.index("statDate")] if "statDate" in f else f"{y}Q{q}"
        out.append((stat, _f(f, row, key)))
    return out


def annualize_factor(stat_date: str | None) -> float:
    """把「年初至今累计」的 ROE/周转率换算成年度口径。

    一季报×4 / 半年报×2 / 三季报×4÷3 / 年报×1。缺失按年报处理（保守，不放大）。
    """
    if not stat_date:
        return 1.0
    m = re.search(r"-(\d{2})-(\d{2})", str(stat_date))
    if not m:
        return 1.0
    month = int(m.group(1))
    return {3: 4.0, 6: 2.0, 9: 4.0 / 3, 12: 1.0}.get(month, 1.0)


def recent_quarters(as_of: str, n: int = 8):
    y, m = int(as_of[:4]), int(as_of[5:7])
    q = (m - 1) // 3 + 1
    out = []
    for _ in range(n):
        out.append((y, q))
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return out


def fetch_one(bs, symbol: str, as_of: str, name: str = "", industry: str = "") -> dict:
    code, mkt = symbol.split(".")
    bs_code = ("sh." if mkt == "SH" else "sz.") + code
    qs = recent_quarters(as_of, 8)
    rec = {"股票代码": symbol, "名称": name, "行业": industry,
           "是否银行": is_bank(industry, name)}

    # ---- A. 成长：净利同比（baostock growth 接口，每期都有）
    g = bs_series(bs, bs.query_growth_data, bs_code, qs, as_of, "YOYNI")
    rec["净利同比%"] = g[0][1] * 100 if g and not pd.isna(g[0][1]) else np.nan
    rec["净利同比_上期%"] = g[1][1] * 100 if len(g) > 1 and not pd.isna(g[1][1]) else np.nan

    # 净利绝对值序列 → 连续正增长期数（季报也齐全，比营收可靠）
    nps = bs_series(bs, bs.query_profit_data, bs_code, qs, as_of, "netProfit")
    rec["最新报告期"] = nps[0][0] if nps else None
    nps_yoy = []
    for i in range(len(nps) - 4):
        _, cur = nps[i]
        _, prev = nps[i + 4]
        if prev is not None and not pd.isna(prev) and prev > 0 and not pd.isna(cur):
            nps_yoy.append((cur / prev - 1) * 100)
    streak_np = 0
    for v in nps_yoy:
        if v > 0:
            streak_np += 1
        else:
            break
    rec["净利连续正增长期数"] = streak_np

    # 营收同比：baostock 的季报(1/3季度)不披露 MBRevenue，只能用半年报/年报序列
    revs = bs_series(bs, bs.query_profit_data, bs_code, qs, as_of, "MBRevenue")
    revs = [(s, v) for s, v in revs if not pd.isna(v) and v > 0]
    yoys = []
    for i in range(len(revs) - 2):
        cur_stat, cur = revs[i]
        _, prev = revs[i + 2]          # 同为半年报/年报，间隔 2 个条目 = 1 年
        if not pd.isna(prev) and prev > 0:
            yoys.append((cur / prev - 1) * 100)
    rec["营收同比%"] = yoys[0] if yoys else np.nan
    rec["营收同比_上期%"] = yoys[1] if len(yoys) > 1 else np.nan
    streak = 0
    for v in yoys:
        if v > 0:
            streak += 1
        else:
            break
    rec["营收连续正增长期数"] = streak
    rec["营收口径"] = "半年/年报" if yoys else "无数据"

    # ---- B. 盈利：杜邦 + 净利率/毛利率
    st, dup = bs_last(bs, bs.query_dupont_data, bs_code, qs, as_of, {
        "ROE(杜邦)%": "dupontROE", "权益乘数": "dupontAssetStoEquity",
        "资产周转率": "dupontAssetTurn", "杜邦净利率": "dupontNitogr",
        "税负比率": "dupontTaxBurden", "利息负担": "dupontIntburden",
    })
    # baostock 的 dupontROE 是「年初至今累计」口径，必须按报告期年化才可比
    ann = annualize_factor(st)
    rec["年化系数"] = ann
    for k, v in dup.items():
        if pd.isna(v):
            rec[k] = np.nan
        elif k == "ROE(杜邦)%":
            rec[k] = v * ann * 100
        elif k == "杜邦净利率":
            rec[k] = v * 100          # 净利率是比率，不年化
        elif k == "资产周转率":
            rec[k] = v * ann          # 周转率也是累计口径，年化
        else:
            rec[k] = v

    stp, prof = bs_last(bs, bs.query_profit_data, bs_code, qs, as_of, {
        "净利率%": "npMargin", "毛利率%": "gpMargin",
        "净资产收益率%": "roeAvg", "EPS_TTM": "epsTTM",
        "流通股本": "liqaShare", "总股本": "totalShare",
    })
    for k, v in prof.items():
        if k in ("净利率%", "毛利率%", "净资产收益率%"):
            rec[k] = v * 100 if not pd.isna(v) else np.nan
        else:
            rec[k] = v

    # ---- C. 含金量 & D. 稳健
    _, cf = bs_last(bs, bs.query_cash_flow_data, bs_code, qs, as_of, {
        "现金流/净利润": "CFOToNP", "现金流/营收": "CFOToOR", "现金流/毛利": "CFOToGr",
    })
    rec.update(cf)
    _, bal = bs_last(bs, bs.query_balance_data, bs_code, qs, as_of, {
        "资产负债率%": "liabilityToAsset", "流动比率": "currentRatio",
        "速动比率": "quickRatio",
    })
    for k, v in bal.items():
        rec[k] = v * 100 if k == "资产负债率%" and not pd.isna(v) else v

    # ---- E. 估值：PE/PB（baostock 日线）
    rs = bs.query_history_k_data_plus(
        bs_code, "date,close,peTTM,pbMRQ,psTTM,turn",
        start_date=f"{int(as_of[:4]) - 1}-01-01", end_date=as_of,
        frequency="d", adjustflag="3")
    f, rows = _rows(rs)
    if rows:
        last = rows[-1]
        rec["最新价"] = _f(f, last, "close")
        rec["PE_TTM"] = _f(f, last, "peTTM")
        rec["PB"] = _f(f, last, "pbMRQ")
        rec["PS_TTM"] = _f(f, last, "psTTM")
        rec["换手率%"] = _f(f, last, "turn")
        # 一年 PE 分位（越低越便宜）
        pes = [_f(f, r, "peTTM") for r in rows]
        pes = [p for p in pes if not pd.isna(p) and p > 0]
        if pes and not pd.isna(rec.get("PE_TTM")) and rec["PE_TTM"] > 0:
            rec["PE一年分位%"] = float(np.mean([p <= rec["PE_TTM"] for p in pes]) * 100)

    # 股息率：近 12 个月现金分红 / 现价
    try:
        rs = bs.query_dividend_data(code=bs_code, year=as_of[:4])
        f, rows = _rows(rs)
        per_share = 0.0
        if rows and "dividCashPsBeforeTax" in f:     # 注意：baostock 是 divid 不是 dividend
            for r in rows:
                v = _f(f, r, "dividCashPsBeforeTax")
                if not pd.isna(v):
                    per_share += v
        rec["近一年每股分红"] = per_share
        rec["股息率%"] = (per_share / rec["最新价"] * 100
                          ) if per_share and rec.get("最新价") and not pd.isna(rec["最新价"]) and rec["最新价"] > 0 else np.nan
    except Exception:
        rec["近一年每股分红"] = np.nan
        rec["股息率%"] = np.nan

    # 市值（亿元）
    if not pd.isna(rec.get("总股本")) and not pd.isna(rec.get("最新价")):
        rec["总市值(亿)"] = rec["总股本"] * rec["最新价"] / 1e8
    if not pd.isna(rec.get("流通股本")) and not pd.isna(rec.get("最新价")):
        rec["流通市值(亿)"] = rec["流通股本"] * rec["最新价"] / 1e8
    return rec


# ---------------------------------------------------------------- 打分
def _score(v, good, bad, weight, zero_below: float | None = None):
    """线性打分：>=good 满分，<=bad 0 分，中间线性。

    zero_below：低于该值直接判 0 分（用于「PE 为负=亏损」这类不能线性外推的情形，
    否则亏损股的负 PE 会被算成「极度低估」拿满分）。
    """
    if v is None or pd.isna(v):
        return weight * 0.4          # 缺失给中性偏下的 4 成，不冤枉也不奖励
    if zero_below is not None and v < zero_below:
        return 0.0
    if good >= bad:
        r = (v - bad) / (good - bad)
    else:
        r = (bad - v) / (bad - good)
    return weight * float(np.clip(r, 0, 1))


def quality_score(r: pd.Series) -> tuple[float, str]:
    """分层质地打分（0~100）。银行与非银行两套权重。"""
    bank = bool(r.get("是否银行"))
    parts = []
    if bank:
        # 银行：ROE / 净利增速 / PB(越低越好) / 股息率 / 杠杆克制 / 营收增速
        parts.append(("ROE", _score(r.get("ROE(杜邦)%"), 14, 8, 25)))
        parts.append(("净利增速", _score(r.get("净利同比%"), 15, 0, 22)))
        parts.append(("营收增速", _score(r.get("营收同比%"), 10, -5, 10)))
        parts.append(("PB低估", _score(r.get("PB"), 0.5, 1.2, 18)))
        parts.append(("股息率", _score(r.get("股息率%"), 5.5, 1.5, 15)))
        parts.append(("杠杆克制", _score(r.get("权益乘数"), 11, 18, 10)))
    else:
        parts.append(("ROE", _score(r.get("ROE(杜邦)%"), 18, 8, 25)))
        parts.append(("营收增速", _score(r.get("营收同比%"), 30, 0, 20)))
        parts.append(("净利增速", _score(r.get("净利同比%"), 30, 0, 18)))
        # 连续性用净利序列：季报齐全，比只有半年/年报口径的营收更稳
        parts.append(("成长连续性", _score(r.get("净利连续正增长期数"), 4, 0, 8)))
        parts.append(("利润含金量", _score(r.get("现金流/净利润"), 1.0, 0.2, 12)))
        parts.append(("净利率", _score(r.get("净利率%"), 15, 3, 9, zero_below=-5)))
        # PE 为负 = TTM 亏损，直接 0 分（否则负数会被当成"极便宜"拿满分）
        parts.append(("估值", _score(r.get("PE_TTM"), 15, 60, 8, zero_below=0)))
    # 两套权重各自合计 100，total 即百分制
    total = sum(p[1] for p in parts)
    detail = "；".join(f"{k}{v:.0f}" for k, v in parts)
    return round(total, 1), ("银行口径：" if bank else "通用口径：") + detail


def build(symbols: list[str], as_of: str, meta: dict | None = None) -> pd.DataFrame:
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
    recs = []
    try:
        for s in symbols:
            m = (meta or {}).get(s, {})
            try:
                recs.append(fetch_one(bs, s, as_of, m.get("名称", ""), m.get("行业", "")))
                print(f"  {s} OK", flush=True)
            except Exception as e:
                print(f"  {s} 失败: {e}", flush=True)
                recs.append({"股票代码": s, "名称": m.get("名称", ""), "行业": m.get("行业", "")})
            time.sleep(0.05)
    finally:
        bs.logout()
    df = pd.DataFrame(recs)
    if len(df):
        sc = df.apply(quality_score, axis=1, result_type="expand")
        df["质地总分"] = sc[0]
        df["评分口径"] = sc[1]
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="逗号分隔，如 600926.SH,002439.SZ")
    ap.add_argument("--from-pool", help="从股票池 xlsx 读取股票代码列")
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    import datetime as dt
    as_of = args.as_of or dt.date.today().strftime("%Y-%m-%d")

    if args.from_pool:
        df = pd.read_excel(args.from_pool, engine="openpyxl")
        symbols = df["股票代码"].astype(str).tolist()
        meta = {}
        for c in ("股票代码", "行业(证监会)"):
            if c in df.columns:
                for _, r in df.iterrows():
                    meta.setdefault(str(r["股票代码"]), {})
                    if c == "行业(证监会)":
                        meta[str(r["股票代码"])]["行业"] = r[c]
    else:
        symbols = [s.strip() for s in (args.symbols or "").split(",") if s.strip()]
        meta = None

    if not symbols:
        raise SystemExit("未指定股票")

    # 名称：从基本面缓存补（含中文名）
    try:
        cache = pd.read_parquet(OUT_DIR / "fundamental_cache.parquet")
        nm = dict(zip(cache["symbol"], cache["name"]))
        ind = dict(zip(cache["symbol"], cache["csrc_industry"]))
        meta = meta or {}
        for s in symbols:
            meta.setdefault(s, {})
            meta[s]["名称"] = meta[s].get("名称") or nm.get(s, "")
            meta[s]["行业"] = meta[s].get("行业") or ind.get(s, "")
    except Exception as e:
        print(f"（名称缓存不可用: {e}）")

    print(f"公司层面分析：{len(symbols)} 只，基准日 {as_of}")
    out = build(symbols, as_of, meta)
    out = out.sort_values("质地总分", ascending=False)
    path = Path(args.out) if args.out else OUT_DIR / f"公司层面分析_{as_of.replace('-','')}.csv"
    out.to_csv(path, index=False, encoding="utf-8-sig")

    show = ["股票代码", "名称", "质地总分", "评分口径", "最新报告期", "营收同比%", "净利同比%",
            "营收连续正增长期数", "ROE(杜邦)%", "净利率%", "现金流/净利润", "资产负债率%",
            "PE_TTM", "PB", "股息率%", "总市值(亿)"]
    show = [c for c in show if c in out.columns]
    print(out[show].to_string(index=False))
    print(f"\n保存: {path}")


if __name__ == "__main__":
    main()
