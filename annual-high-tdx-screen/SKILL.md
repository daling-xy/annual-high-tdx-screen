---
name: annual-high-tdx-screen
description: A股「年度新高+平滑爬升」盘后选股流水线（通达信 TDX 供数版）。收盘后执行：tdx_screener 初筛建池（30日日均成交额大于4亿 且 换手低于6% 且 现价不低于250日高的84.5%）→ tdx_kline 批量供数 → zigzag 形态精算（箱宽不超20%、位置不低于0.845、无转折票 Q 级静默放行）→ baostock 公司质地评分 → 形态40%加质地60% 合并出最终 Excel。用户说「收盘后选股」「每日选股」「跑筛选」「盘后复盘+筛选」「年度新高 TDX 版」时使用本技能。
agent_created: true
---

# 年度新高 · 平滑爬升选股（TDX 供数版）

收盘后跑一遍，产出次日候选股票池 Excel。全程只读，**不包含也不触发任何交易操作**。

## 前置条件

1. **通达信 MCP 连接器（tdx-connector）已连接**——初筛与 K 线数据都走它。
2. Python ≥ 3.10，依赖见 `scripts/requirements.txt`：
   ```
   pip install -r scripts/requirements.txt
   ```
   （`matplotlib`/`mplfinance` 仅用于出形态图，缺失时自动跳过，不影响筛选。）
3. 基本面层使用 baostock 免费接口，首次运行需联网。

## 主流程（五步，全程约 25 分钟）

### ① 初筛建池（收盘后 3~5 分钟，等数据落定）

调用 tdx-connector 的 `tdx_screener`，**query 必须使用 `scripts/daily_tdx.py` 顶部的 `SCREENER_QUERY` 常量**（唯一权威口径）：

> 30日日均成交额大于4亿 且 近30日平均换手率小于6% 且 收盘价大于250日最高价的80%

⚠️ **成交额与换手率是硬指标，任何情况下不得放宽或改口径**。两处口径修正记录：①必须「30日日均成交额」，不得用「今日成交额」（当日口径会误杀缩量慢牛股）；②换手率必须写「**近30日平均换手率**」——「30日日均换手率」的措辞会导致 tdx_screener 的 NLP 解析失败。

把返回的候选列表写成 `out/tdx_candidates_YYYYMMDD.json`：
```json
{"source": "tdx_screener", "query": "<SCREENER_QUERY>", "total": N,
 "items": [{"code": "600519", "market": "1", "name": "贵州茅台"}, ...]}
```
（沪市 market="1"，深市/创业板 market="0"）

### ② 批量拉 K 线（约 10 分钟，最慢的一步）

对候选池每只调用 `tdx_kline`：`period="4"`（日线）、`wantNum=300`、`tqFlag="1"`（前复权）；沪市 `setcode="1"`、深市 `setcode="0"`。结果自动落盘到 WorkBuddy 的 tool-results 目录。

### ③ 形态精算（约 2 分钟）

```bash
python scripts/daily_tdx.py --candidates out/tdx_candidates_YYYYMMDD.json
```

- 收盘后运行**不要**加 `--drop-today`（保留当日完整 bar）；盘中运行才加。
- K 线缓存目录自动探测（`~/.workbuddy/projects/**/tool-results` 中最新者），可用 `--scan-dir` 或环境变量 `AHS_KCACHE` 覆盖。
- 产出 `out/日常筛选_TDX供数_YYYYMMDD_HHMMSS.xlsx`（明细含每只票的过/拒原因）与 `out/charts_ai_tdx/*.png` 形态图。

### ④ 公司质地（约 8 分钟）

```bash
python scripts/company_analysis.py --symbols <③通过的代码,逗号分隔,如 002415.SZ,600926.SH> \
    --as-of <YYYY-MM-DD> --out out/公司层面分析_YYYYMMDD.csv
```

### ⑤ 合并产出（1 分钟）

```bash
python scripts/build_final_report_tdx.py --pool out/日常筛选_TDX供数_*.xlsx \
    --fund out/公司层面分析_*.csv --trade-date <YYYY-MM-DD>
```

综合分 = 形态 × 40% + 质地 × 60% → `out/最终筛选结果_*.xlsx`（四个 sheet：最终结论/公司层面/技术形态/评分口径）。

可选：在 skill 根目录放 `analyst_notes.json` 注入机构一致预期与定性点评（结构见 `build_final_report_tdx.py` 头部注释）。

## 并行支线：盘后复盘（无需脚本）

等待 ② 的间隙并行取数：四大指数（`tdx_quotes`：000001/399001/399006/000688，含涨跌家数与主力净流入）、涨停梯队（`tdx_screener` message="今日涨停"）、昨日入选股今日涨跌对照。写成简报与筛选结果一起交付。

## 筛选规则速览（完整版见 references/methodology.md）

| 层 | 关键条件 | 状态 |
|---|---|---|
| 初筛 | 30日日均成交>4亿 · 换手<6% · 现价≥250日高×80%（粗筛，精确0.845由引擎收盘口径把关） | 🔒 硬指标 |
| 行业 | **银行（证监会 J66）整批剔除**（daily_tdx.py `EXCLUDE_BANKS=True`） | ✅ 2026-11-03 起 |
| 硬过滤 | 30日高≥250日高×90% · 30日均成交≥4亿 | 🔒 |
| 形态 | zigzag 转折点（1.5% prominence）· 段振幅均匀 · 连续涨跌≤6日 · 30日跌幅>-6% | ✅ |
| 收口 | 现价/250日高≥0.845 · 30日箱宽≤20% | ✅ 回测校准 |
| 静默票 | 30日无可识别转折 → Q级放行（固定70分），后续闸门照常 | ✅ |
| 收口2 | **中枢偏离度 ≤ +8%**（延伸段不追，算不出中枢的Q级票自动跳过） | ✅ 2026-11-03 转正 |
| 影子 | 缠论中枢宽度≤10% · 振幅v2方向敏感 | ⚪ 只记账不拦截 |

影子三件套的转正前提：先用历史回测校准阈值，不得凭单日样本定案。

## 注意事项

- 本技能只产出候选池，**实盘买卖由使用者在交易端自行完成**。
- TDX 前复权与本地数据湖在除权股上可能有细微口径差，属正常。
- `out/positions_pro.json` 记录触发买入价/移除条件（突破30日最高收盘×1.05 触发买入、+20% 止盈、回撤-20% 止损），属正常状态管理。
- 批量拉 K 线时若个别票无返回，重试一次即可；仍无则当日跳过该票。
