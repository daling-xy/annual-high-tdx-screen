# annual-high-tdx-screen

A股「年度新高 + 平滑爬升」盘后选股流水线 —— [WorkBuddy](https://www.workbuddy.cn) Skill（通达信 TDX 供数版）。

收盘后跑一遍，产出次日候选股票池 Excel。策略思想是用工程化指标复刻缠论的「窄中枢 + 第三类买点」：在贴近一年新高的位置，找低波动、结构收敛、流动性充足的票。

**全程只读，不包含也不触发任何交易操作。**

## 效果示例

```
51 只候选 → 形态精算 → 31 只通过（6 只 zigzag 验证 A 级 + 25 只静默 Q 级）
→ baostock 质地四维评分 → 综合分 = 形态×40% + 质地×60% → 最终 Excel
```

## 安装

```bash
# 1. 克隆本仓库
git clone https://github.com/daling-xy/annual-high-tdx-screen.git

# 2. 把 skill 目录拷到 WorkBuddy 用户技能目录
#    Windows:
xcopy /E /I annual-high-tdx-screen "%USERPROFILE%\.workbuddy\skills\annual-high-tdx-screen"
#    macOS/Linux:
cp -r annual-high-tdx-screen ~/.workbuddy/skills/

# 3. 安装 Python 依赖（建议用虚拟环境）
pip install -r annual-high-tdx-screen/scripts/requirements.txt
```

**前置条件**：

| 依赖 | 用途 | 必要性 |
|---|---|---|
| WorkBuddy 的**通达信连接器（tdx-connector）** | 初筛建池 + K 线供数 | 必需 |
| Python ≥ 3.10 | 跑脚本 | 必需 |
| baostock（pip 包） | 公司质地财务数据 | 必需（免费接口） |
| matplotlib + mplfinance | 出形态 K 线图 | 可选，缺失自动跳过 |

## 使用

装好后在 WorkBuddy 里说：**「收盘了，跑每日选股」** 即可触发完整流程。

流程细节（也可纯手动执行）：

```bash
# ③ 形态精算（①初筛②拉K线由 Agent 调用 TDX MCP 完成，见 SKILL.md）
python scripts/daily_tdx.py --candidates out/tdx_candidates_YYYYMMDD.json

# ④ 公司质地
python scripts/company_analysis.py --symbols 002415.SZ,600926.SH --as-of YYYY-MM-DD

# ⑤ 合并产出
python scripts/build_final_report_tdx.py --pool out/日常筛选_TDX供数_*.xlsx --fund out/公司层面分析_*.csv
```

## 筛选规则速览

| 层 | 关键条件 | 说明 |
|---|---|---|
| 初筛 | 30日日均成交额 > 4亿 · 近30日平均换手率 < 6% · 现价 ≥ 250日最高 × 80% | 🔒 成交/换手不可放宽；80% 为粗筛，精确 0.845 由引擎把关 |
| 行业 | 银行（证监会 J66）整批剔除（`EXCLUDE_BANKS=True`，可关） | ✅ |
| 硬过滤 | 30日高 ≥ 250日高 × 90% · 30日均成交 ≥ 4亿 | 🔒 |
| 形态 | zigzag 转折（1.5% prominence）· 段振幅均匀 · 连续单边 ≤ 6日 · 30日跌幅 > -6% | ✅ |
| 收口 | 现价/250日高 ≥ 0.845 · 30日箱宽 ≤ 20% | ✅ 经 79.3 万样本回测校准 |
| 静默票 | 30日无可识别转折 → Q 级放行（固定 70 分） | ✅ |
| 收口2 | 中枢偏离度 ≤ +8%（延伸段不追，Q级静默票自动豁免） | ✅ |
| 影子观察 | 缠论中枢宽度 ≤ 10% · 方向敏感振幅 v2 | ⚪ 只记账不拦截 |

完整阈值档案（哪些是回测校准、哪些是观察值、改动风险）见
[references/methodology.md](annual-high-tdx-screen/references/methodology.md)。

## 目录结构

```
annual-high-tdx-screen/
├── SKILL.md                      # WorkBuddy 技能定义（触发词、流程、纪律）
├── references/
│   └── methodology.md            # 策略方法论与阈值档案
└── scripts/
    ├── jianchashuju_local.py     # 形态引擎（zigzag/箱宽/中枢/静默/振幅）
    ├── daily_tdx.py              # TDX 供数主流程
    ├── company_analysis.py       # baostock 质地四维评分
    ├── build_final_report_tdx.py # 三层合并产出 Excel
    └── requirements.txt
```

## 可选：注入机构一致预期

在 skill 根目录放 `analyst_notes.json`（参考 `analyst_notes.example.json`），
可为个股补充机构评级、目标价、定性点评，体现在最终 Excel 里。缺省为空，不影响排名。

## 免责声明

本项目仅用于量化策略研究与学习交流，产出的是候选观察池，**不构成任何投资建议**。
股市有风险，实盘决策与后果由使用者自行承担。

## License

MIT
