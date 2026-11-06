# -*- coding: utf-8 -*-
"""不依赖数据湖的日常筛选入口（TDX MCP 供数）

解决的问题：本地数据湖全市场日更快则数十分钟、慢则数小时且易超时失败，
            而每天的选股其实只需要「候选池」那几十只的日线。
            实测 cne daily：墙钟 351 分钟且 daily_bars 在 825s 处被判 failed
            （根因 config line 14 `workers = 1`，Windows 上 cne config init 的默认值；
             官方注释写明 543ms/只 × 5400 只 ≈ 49 分钟，Linux workers=8 才几分钟）。

每天的完整操作（收盘后 15:10 之后跑，避免当日 bar 未收完污染形态）：
  ── 第 1 步，在对话里说一句话 ─────────────────────────────
     「跑今天的年度新高筛选」
  ── 第 2 步（由 agent 执行，不需要人操作）────────────────
  1) tdx_screener 自然语言拿候选池（实测一句话把全市场压到 60 只，秒级）
       成交额大于4亿元，且收盘价在250日最高价的84.5%以上，且换手率小于6%
     ⚠ 条件别超过 4 条，6 条会把服务端打到 503
  2) 对候选池逐只并行调 tdx_kline(code, setcode, period="4", wantNum=300, tqFlag="1")
     返回约 132KB/只 → 自动落盘到 tool-results/*.txt，不进上下文，不占 token
     实测并行 4 只约 12 秒返回，并行拉整池分钟级完成
  3) 跑本脚本：--drop-today 剔除当日未完成 bar → 跑形态判定 → 出图 → 出 Excel
  4) agent 看图做 AI 视觉终审（每次约 4 张，几十秒）

调用指北：
  python scripts/daily_tdx.py --candidates out/tdx_candidates_xxx.json --drop-today
  python scripts/daily_tdx.py                       # 不传候选池则处理全部已落盘 K 线

⚠ 已知口径差：TDX 前复权与数据湖前复权的除权基准不同，有分红除权的股票
  prox_now 会偏移（实测中远海控 0.939 vs 0.963）。非除权股完全一致
  （江苏银行、美的集团小数点后三位相同）。判定等级通常不变，但建议
  两条路径交叉验证，prox 偏差 >0.02 的重点人工看图确认。

关键设计：形态逻辑零重写。直接构造 screener._by_symbol，让 jianchashuju_local
          的 analyze_one 跑与全市场流程完全一致的判定口径。
"""
from __future__ import annotations

# ============================================================
# 官方初筛 query（tdx_screener 唯一权威口径，2026-10-29 修正）
# 修正记录：成交额条件原为「今日成交额>4亿」（当日口径），
#   会把"当日缩量但30日均达标"的票误杀（如华海药业600521引发的排查），
#   且与形态层硬过滤 MIN_AVG_AMOUNT(30日均≥4亿) 口径不一致。
#   2026-10-29 用户确认：统一为 30 日日均成交额 > 4亿。
# 2026-10-30 再修正：换手率同步为 30 日均口径。注意 NLP 措辞——
#   「30日日均换手率」解析失败(NLPParse)，必须写「近30日平均换手率」。
# ⚠️ 成交额/换手率是硬指标，任何情况下不得放宽。
# ============================================================
SCREENER_QUERY = "30日日均成交额大于4亿且近30日平均换手率小于6%且收盘价大于250日最高价的80%"
# 注：位置条件在初筛放宽到 80%（粗筛）。screener 的「250日最高价」是日内最高价口径，
# 比引擎 PROX_NOW_GATE=0.845 的收盘口径更严，等值使用会在门口误杀
# 「收盘已回年高附近但年内有长上影」的票（2026-11-03 实测：84.5% 挡掉 21 只，
# 含启明星辰/牧原/迈瑞/顺丰）。精确位置拦截由形态引擎 0.845（收盘口径）完成，
# 最终门槛不变。成交额/换手率两条为硬指标，与引擎同口径，不放宽。

# ============================================================
# 行业剔除（2026-11-03 用户拍板：把所有银行剔除出股票池）
# 依据：证监会分类 J66「货币金融服务」。银行形态普遍极平（箱宽小、
# 转折点少→Q级静默），大量占用候选池名额且与「平滑爬升进攻」的
# 策略定位不符；说明书回测亦显示银行业为负 alpha 行业。
# 数据：out/fundamental_cache.parquet（baostock 证监会分类）；
# 缓存不可用时退化为名称含「银行」的兜底匹配。
# ============================================================
EXCLUDE_BANKS = True

import argparse
import datetime as dt
import importlib.util
import json
import sys
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
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "out"
CHART_DIR = OUT_DIR / "charts_ai_tdx"

def _detect_toolresults() -> Path:
    """便携版：自动探测当前用户 tool-results 目录（取最新修改者）。
    可用环境变量 AHS_KCACHE 显式指定。找不到时退回本目录（由 collect 报缺数据）。"""
    import os
    env = os.environ.get("AHS_KCACHE")
    if env and Path(env).is_dir():
        return Path(env)
    base = Path.home() / ".workbuddy" / "projects"
    best, best_mt = None, -1.0
    try:
        for p in base.glob("*/*/tool-results"):
            try:
                mt = p.stat().st_mtime
                if mt > best_mt:
                    best, best_mt = p, mt
            except OSError:
                continue
    except Exception:
        pass
    return best if best else HERE


DEFAULT_TOOLRESULTS = _detect_toolresults()


class _DummyPM:
    def __init__(self, *a, **k):
        self.positions = {}

    def load_positions(self):
        pass

    def save_positions(self):
        pass

    def add_position(self, *a, **k):
        pass

    def remove_position(self, *a, **k):
        pass

    def check_buy_trigger(self, *a, **k):
        return False, None

    def check_removal_conditions(self, *a, **k):
        return False, ""


def load_screener_module():
    spec = importlib.util.spec_from_file_location(
        "jianchashuju_local", ROOT / "scripts" / "jianchashuju_local.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["jianchashuju_local"] = mod
    spec.loader.exec_module(mod)
    return mod


def parse_mcp_file(path: Path) -> tuple[str, pd.DataFrame] | None:
    """解析单个 MCP 落盘文件 → (symbol, OHLCV DataFrame)。"""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        i = raw.find("{")
        if i < 0:
            return None
        obj, _ = json.JSONDecoder().raw_decode(raw[i:])
        rows = obj.get("Rows") or []
        if not rows:
            return None
        code = str(obj.get("Code", "")).strip()
        setcode = str(obj.get("Setcode", ""))
        if not code:
            return None
        sym = f"{int(code):06d}.{'SH' if setcode == '1' else 'SZ'}"
        df = pd.DataFrame(rows)
        df = df.rename(columns={"Data": "trade_date", "Open": "open", "High": "high",
                                "Low": "low", "Close": "close", "Volume": "volume",
                                "Amount": "amount"})
        keep = ["trade_date", "open", "high", "low", "close", "volume", "amount"]
        df = df[[c for c in keep if c in df.columns]].copy()
        for c in df.columns:
            if c != "trade_date":
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df["trade_date"] = df["trade_date"].astype(str)
        df = df[df["close"] > 0].sort_values("trade_date").reset_index(drop=True)
        # 盘中数据会污染形态判定（当日 bar 未收完），除非当日已收盘否则剔除
        return sym, df
    except Exception:  # noqa: BLE001
        return None


def collect(scan_dir: Path, want: set[str] | None) -> tuple[dict, dict]:
    """扫描目录里所有 mcp-tdx-connector-tdx_kline-*.txt → {symbol: df}"""
    by_symbol: dict[str, pd.DataFrame] = {}
    seen_files: dict[str, Path] = {}
    files = sorted(scan_dir.glob("mcp-tdx-connector-tdx_kline-*.txt"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        got = parse_mcp_file(f)
        if not got:
            continue
        sym, df = got
        if want and sym not in want:
            continue
        if sym in by_symbol:          # 同名保留最新
            continue
        by_symbol[sym] = df
        seen_files[sym] = f
    return by_symbol, seen_files


def load_bank_symbols() -> set[str]:
    """返回应剔除的银行 symbol 集合（证监会 J66）。缓存缺失时用名称兜底。"""
    banks: set[str] = set()
    fc_path = OUT_DIR / "fundamental_cache.parquet"
    try:
        if fc_path.exists():
            fc = pd.read_parquet(fc_path, columns=["symbol", "csrc_code", "name"])
            hit = fc[fc["csrc_code"].astype(str).str.startswith("J66", na=False)]
            banks = {str(s) for s in hit["symbol"]}
    except Exception as e:
        print(f"[警告] 银行清单加载失败，改用名称兜底: {e}")
    return banks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan-dir", default=str(DEFAULT_TOOLRESULTS))
    ap.add_argument("--candidates", help="候选池 json；不传则用全部已落盘 K 线")
    ap.add_argument("--no-chart", action="store_true")
    ap.add_argument("--drop-today", action="store_true",
                    help="剔除当日未收盘 bar（盘中运行必加，否则形态判定被污染）")
    args = ap.parse_args()

    scan_dir = Path(args.scan_dir)
    if not scan_dir.is_dir():
        print(f"目录不存在: {scan_dir}")
        return 1

    names: dict[str, str] = {}
    want: set[str] | None = None
    if args.candidates:
        cp = Path(args.candidates)
        if not cp.is_absolute():
            cp = ROOT / cp
        meta = json.loads(cp.read_text(encoding="utf-8"))
        want = {f"{it['code']}.{'SH' if int(it['market']) == 1 else 'SZ'}"
                for it in meta["items"]}
        names = {f"{it['code']}.{'SH' if int(it['market']) == 1 else 'SZ'}": it["name"]
                 for it in meta["items"]}
        print(f"候选池 {len(want)} 只")
        if EXCLUDE_BANKS and want:
            banks = load_bank_symbols()
            before = len(want)
            kicked = sorted(s for s in want
                            if s in banks or "银行" in str(names.get(s, "")))
            want -= set(kicked)
            for s in kicked:
                names.pop(s, None)
            if kicked:
                print(f"银行剔除（J66）：{len(kicked)} 只 -> "
                      f"{', '.join(kicked)}  | 剩余 {len(want)} 只")

    by_symbol, files = collect(scan_dir, want)
    print(f"从 {scan_dir.name} 解析到 {len(by_symbol)} 只股票的 K 线")
    if want:
        missing = sorted(want - set(by_symbol))
        if missing:
            print(f"尚缺 {len(missing)} 只的 K 线: {', '.join(missing[:15])}"
                  f"{' ...' if len(missing) > 15 else ''}")

    if not by_symbol:
        print("没有可用的 K 线数据，请先调用 tdx_kline 拉取候选池日线")
        return 1

    if args.drop_today:
        today = dt.datetime.now().strftime("%Y%m%d")
        dropped = 0
        for s in list(by_symbol):
            d = by_symbol[s]
            if len(d) > 1 and d["trade_date"].iloc[-1] == today:
                by_symbol[s] = d.iloc[:-1].reset_index(drop=True)
                dropped += 1
        if dropped:
            print(f"已剔除 {dropped} 只的当日未完成 bar → 以 {today} 前一交易日收盘为准")

    # 复用全市场流程的形态逻辑
    mod = load_screener_module()
    mod.PositionManager = _DummyPM
    screener = mod.MarketScreener()
    screener._by_symbol = by_symbol
    screener._bars = pd.concat(
        [d.assign(symbol=s) for s, d in by_symbol.items()], ignore_index=True)

    rows = []
    for sym, df in by_symbol.items():
        try:
            r = screener.analyze_one(sym)
        except Exception as e:  # noqa: BLE001
            r = {"passed": False, "fail_reason": f"异常:{type(e).__name__} {e}"}
        rows.append({
            "symbol": sym,
            "名称": names.get(sym, ""),
            "K线根数": len(df),
            "本地形态通过": bool(r.get("passed", False)),
            "静默通过": bool(r.get("quiet_pass", False)),
            "等级": r.get("grade", ""),
            "总分": r.get("total_score", np.nan),
            "最新价": r.get("latest_price", np.nan),
            "现价/年高": round(float(r.get("prox_now", 0) or 0), 4),
            "30日箱宽": round(float(r.get("volatility_50d", 0) or 0), 4),
            "中枢宽度": (round(float(r["central_zone_width"]), 4)
                         if r.get("central_zone_width") is not None else None),
            "中枢ZG": r.get("central_zone_zg"),
            "中枢ZD": r.get("central_zone_zd"),
            "中枢段数": r.get("central_zone_legs", 0),
            "中枢偏离度": (round(float(r["dev_from_zg"]), 4)
                         if r.get("dev_from_zg") is not None else None),
            "最大单段涨幅": round(float(r.get("max_leg_up", 0) or 0), 4),
            "最大单段跌幅": round(float(r.get("max_leg_down", 0) or 0), 4),
            "深跌修复度": (round(float(r["drop_repair"]), 3)
                         if r.get("drop_repair") is not None else None),
            "v2振幅判定": ("" if r.get("amp_v2_pass") is None
                         else ("过" if r.get("amp_v2_pass") else f"拒:{r.get('amp_v2_reason','')}")),
            "振幅CV": round(float(r.get("amplitude_cv", 0) or 0), 3),
            "起伏次数": r.get("n_count", np.nan),
            "日均成交(亿)": round(float(r.get("avg_amount_yi", 0) or 0), 2),
            "淘汰原因": r.get("fail_reason", ""),
        })

    df_out = pd.DataFrame(rows)
    passed = df_out[df_out["本地形态通过"]].sort_values("总分", ascending=False)

    print("\n" + "=" * 74)
    print(f"TDX 供数 → 形态精算：{len(df_out)} 只 → 通过 {len(passed)} 只")
    print("=" * 74)
    if len(passed):
        for _, r in passed.iterrows():
            print(f"  {r['symbol']} {r['名称']:<8} {r['等级']}级{r['总分']:.0f}分 | "
                  f"现价/年高={r['现价/年高']:.3f} 箱宽={r['30日箱宽']:.1%} "
                  f"CV={r['振幅CV']:.2f} 起伏={r['起伏次数']}次")
    if len(df_out) - len(passed):
        print(f"\n淘汰 {(~df_out['本地形态通过']).sum()} 只，原因 Top：")
        for reason, cnt in df_out[~df_out["本地形态通过"]]["淘汰原因"].value_counts().head(8).items():
            print(f"  {cnt:>3} 只  {reason}")

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    xlsx = OUT_DIR / f"日常筛选_TDX供数_{ts}.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        df_out.sort_values(["本地形态通过", "总分"], ascending=[False, False]).to_excel(
            w, sheet_name="全候选", index=False)
        if len(passed):
            passed.to_excel(w, sheet_name="形态通过", index=False)
    print(f"\n已导出: {xlsx}")

    if not args.no_chart and len(passed):
        CHART_DIR.mkdir(parents=True, exist_ok=True)
        print(f"出图 → {CHART_DIR}")
        for s in passed["symbol"].tolist():
            if render_from_df(by_symbol[s], s, CHART_DIR):
                print(f"  {s} ok")

    return 0


def render_from_df(df: pd.DataFrame, symbol: str, outdir: Path) -> bool:
    """用已有 DataFrame 画 K 线图（不回查数据源）。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import mplfinance as mpf
    except Exception as e:  # noqa: BLE001
        print(f"  绘图依赖缺失: {e}")
        return False

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial"]
    plt.rcParams["axes.unicode_minus"] = False

    d = df.tail(120).copy().rename(columns={
        "trade_date": "Date", "open": "Open", "high": "High",
        "low": "Low", "close": "Close", "volume": "Volume"})
    d["Date"] = pd.to_datetime(d["Date"])
    d = d.set_index("Date")

    full = df.set_index(pd.to_datetime(df["trade_date"]))["close"]
    hi250, hi30, lo30 = full.tail(250).max(), full.tail(30).max(), full.tail(30).min()
    n = len(d)
    mc = mpf.make_marketcolors(up="#E24B4A", down="#3B6D11", edge="inherit",
                               wick="inherit", volume="in")
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle=":", gridcolor="#D3D1C7",
                               facecolor="white", figcolor="white",
                               rc={"font.sans-serif": ["Microsoft YaHei", "SimHei", "Arial"],
                                   "axes.unicode_minus": False})
    apds = [mpf.make_addplot([hi250] * n, color="#888780", linestyle="--", width=1.2),
            mpf.make_addplot([hi30] * n, color="#0F6E56", linestyle="--", width=1.0),
            mpf.make_addplot([lo30] * n, color="#0F6E56", linestyle="--", width=1.0)]
    box30 = (hi30 - lo30) / lo30 if lo30 else float("nan")
    prox = full.iloc[-1] / hi250 if hi250 else float("nan")
    fig, axes = mpf.plot(
        d, type="candle", style=style, addplot=apds, volume=True,
        figsize=(11, 6.5), returnfig=True,
        title=dict(title=(f"{symbol}   收盘{full.iloc[-1]:.2f} ({d.index[-1]:%Y-%m-%d})   "
                          f"现价/年高={prox:.3f}   箱宽={box30:.1%}"), fontsize=13),
        ylabel="价格(前复权)", ylabel_lower="成交量")
    ax = axes[0]
    ax.axvspan(n - 31, n - 1, color="#E1F5EE", alpha=0.6, zorder=0)
    ax.set_ylim(min(d["Low"].min(), lo30) * 0.97, max(d["High"].max(), hi250) * 1.03)
    fig.savefig(outdir / f"{symbol.replace('.', '_')}.png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return True


if __name__ == "__main__":
    raise SystemExit(main())
