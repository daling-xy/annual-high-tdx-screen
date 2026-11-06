# -*- coding: utf-8 -*-
"""
平滑等幅上升震荡形态筛选系统 - 专业版（本地数据湖版）

与 jianchashuju.py 的关系：
  - 形态逻辑、参数、评分、持仓触发/移除规则 **逐行保持一致**
  - 唯一变化：数据源从 miniqmt(xtdata) 换成 CNEquity 本地数据湖
      xtdata.get_local_data(front)  →  astock_core.lake_reader.load_daily_bars(adjust='qfq')
  - 股票列表：xtdata.get_stock_list_in_sector('沪深A股') → 湖内 instruments 表（同前缀过滤）

运行：
  D:\\hagent\\.venv\\Scripts\\python.exe jianchashuju_local.py
数据新鲜度：跟随数据湖日更（每日 16:05 自动更新到上一交易日）。
盘中触发买入（分时价 > 30日最高收盘价×1.05）在无分时数据时用当日最高价近似，
与原版 xtdata 无分时输入时的回退逻辑一致。
"""

import sys
import os
import re
import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Dict, Any, List, Tuple, Optional
from datetime import datetime
import warnings
import json

warnings.filterwarnings('ignore')

# Windows 控制台默认 GBK，中文/符号输出需要 UTF-8
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# ==========================================
# 本地数据湖接入（可选；TDX 供数路线不需要）
# ==========================================
# 便携版：PROJECT_ROOT 默认取脚本上一级目录（即 skill 根目录），
# 可用环境变量 AHP_ROOT 覆盖。数据湖 astock_core 为可选依赖，
# 缺失不影响 TDX MCP 供数的筛选主流程。
from pathlib import Path as _Path

PROJECT_ROOT = os.environ.get('AHP_ROOT') or str(_Path(__file__).resolve().parents[1])
sys.path.insert(0, PROJECT_ROOT)

try:
    from astock_core import lake_reader   # noqa: E402
    print("✅ 已接入 CNEquity 本地数据湖:", lake_reader.DATA_ROOT)
except Exception:
    lake_reader = None  # TDX 供数路线无需数据湖；数据湖模式调用时会报清晰错误

OUT_DIR = _Path(PROJECT_ROOT) / 'out'
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _require_lake():
    if lake_reader is None:
        raise RuntimeError(
            "本地数据湖（astock_core.lake_reader）未安装。TDX 供数路线不需要它；"
            "若要使用数据湖模式，请先部署 astock_core 并设置 AHP_ROOT。")

# ==========================================
# 形态参数配置（与原文件一致）
# ==========================================
@dataclass
class PatternRules:
    """平滑震荡形态参数 - 专业版"""

    LOOKBACK_DAYS: int = 30          # ← 修改为30日（原为50）
    YEAR_DAYS: int = 250
    HIGH_CHECK_DAYS: int = 30

    # 硬性条件
    MIN_AVG_AMOUNT: float = 4e8          # ⚠️ 硬指标（2026-10-29 用户指令）：成交量/换手率
                                         #    永不放宽。流动性是策略的安全垫，任何实验
                                         #    口径都不得降低本阈值或放开换手上限。
    YEAR_HIGH_RATIO: float = 0.90        # ★ 修改：年高比例由 0.80 改为 0.90
    # LOW_30_YEAR_HIGH_FACTOR 已取消约束

    # ---- 标的池卫生（2026-10-23 新增；借鉴 yh_screen，该套阈值经 35.9 万事件回测校准）----
    EXCLUDE_NAME_PATTERN: str = "ST|退"   # 名称含 ST / 退 → 剔除（湖内含 467 只）
    MIN_LIST_DAYS: int = 120             # 上市满 120 自然日（≈60 交易日）才纳入，排除次新
    MIN_BARS_250: int = 200              # 250日窗口内至少 200 根 bar：排除次新与长期停牌

    # 行业剔除：银行 / 基建 / 铁路公路 / 航运港口 / 航空机场 / 物流仓储 / 通信服务
    # 用户口述规则 + yh_screen 代码清单，双方一致。
    # 数据：out/fundamental_cache.parquet（baostock 证监会分类，由 build_fundamental_cache.py 生成）
    # 注意：必须按证监会「代码前缀」匹配，不能用关键词——否则
    #       C39「计算机、通信和其他电子设备制造业」667 只会被 "通信" 误杀。
    # 2026-10-26 用户拍板：不剔除任何行业，关闭本开关。
    # （当日实测：中远海控 601919 形态 A93/prox 0.939/箱宽 9.1% 完全合格却被行业剔除，
    #   用户认为行业不应作为硬过滤，交给 AI 终审与基本面层判断。）
    EXCLUDE_INDUSTRY_ENABLED: bool = False
    TURN_MAX_30D: float = None           # 30日均换手率上限(%)，None=仅输出不拦截；yh v2 证据：>6% 三周期超额为负
    MIN_ROE: float = None                # ROE 下限(%)，None=仅输出不拦截

    # ---- 年高接近度：双口径 ----
    # 旧口径 prox     = 近30日最高收盘 / 250日最高收盘（原方案，会放行「回落15%后横盘」的票）
    # 新口径 prox_now = 当前收盘       / 250日最高收盘（yh 回测后采用，更贴合「距年高」原意）
    # 2026-10-23 回测定案（2023-01~2026-10，79.3万样本，20日持有）：
    #   现行(旧0.90/箱25) 胜率46.6%/超额44.9% → C档(新0.90/箱20) 50.7%/48.6%
    #   → F档(新0.90/箱15) 53.5%/50.2% → B档(新0.95/箱15) 58.7%/51.8%
    # 现启用 C 档：胜率显著抬升且候选密度仍够形态筛选再过滤。
    PROX_NOW_GATE: float = 0.845         # 当前价/250日最高收盘 ≥ 0.845 才算「接近年高」
    # 注：用户口述「放宽到 85%」。石药创新 300765 实测 prox_now=0.849556，
    # 若严格取 0.85 会以 0.0004 之差被浮点边界卡掉，故取 0.845 使 85% 档意图生效。
    BOX_WIDTH_GATE: float = 0.20         # 30日箱宽(区间振幅) ≤ 20%

    # ---- 缠论中枢口径（2026-10-29 新增，默认仅影子输出，不参与拦截）----
    # 差别：外包络箱宽会把「早已退出当下结构的旧端点」也算进来
    #       （实测瑞芯微 603893：外包络 30.41% vs 最近三段重叠 10.90%，差 19.5pp）。
    #       缠论中枢只取「最近 nlegs 个连续次级别走势的价格重叠区」。
    # ⚠ CENTRAL_ZONE_GATE 尚未回测校准，切 True 前必须先跑 yh_screen 重定阈值。
    CENTRAL_ZONE_ENABLED: bool = False   # False=影子模式(只算不拦) True=用中枢宽度替代外包络做拦截
    CENTRAL_ZONE_GATE: float = 0.10      # 中枢宽度上限

    # ---- 中枢偏离度闸（2026-11-03 用户拍板转正）----
    # 剔除「已突破中枢且延伸」的票：现价高出中枢上沿 ZG 超过 +8% 不追。
    # 起源：中远海控 601919 突破中枢后偏离 +9.2%，用户确认此类票不入选。
    # 依据：健康三买区在 ZG±3% 内；爆拉/延伸票 ≥+10%（新产业/海正药业实测）。
    # 注意：仅当能算出中枢（转折点≥4 且三段重叠）时生效；算不出（Q级静默票、
    #       单边无重叠）时跳过本闸——平滑慢爬票本身不存"远离中枢"问题。
    DEV_FROM_ZG_ENABLED: bool = True
    DEV_FROM_ZG_GATE: float = 0.08
    # 阈值依据（2026-10-29 单日 57 只候选实测，尚未回测）：
    #   ≤8% →10只(丢启明星辰/甘李)   ≤10% →12只(现行10只+瑞芯微+海正药业)
    #   ≤15%/≤20% →仍是12只(中枢宽度天然集中在2~9%，再放宽也不放水)
    # 上线前必须跑 yh_screen 回测复核，不要凭单日样本定案。
    CENTRAL_ZONE_LEGS: int = 3           # 构成中枢的连续次级别走势段数（缠论标准=3）
    CENTRAL_ZONE_FALLBACK: bool = True   # 算不出中枢时 True=退回外包络箱宽，False=直接放行

    # 触发买入条件：分时价格 > 30日最高收盘价 × (1 + TRIGGER_BUY_THRESHOLD)
    TRIGGER_BUY_THRESHOLD: float = 0.05  # 5%

    # 移除条件
    TRIGGER_BUY_RISE_PCT: float = 0.20   # 脱离触发买入价20%涨幅移除
    MAX_DROP_FROM_HIGH_PCT: float = -0.20  # 从30日高点回撤20%移除

    # 新增形态约束
    REBOUND_TOLERANCE: float = 0.07      # 回弹接近前高的容差（7%）
    MAX_DEVIATION_FROM_HIGH: float = 0.13  # 最新价离最高价偏离≤13%

    # 连续涨跌停板检测
    CHECK_LIMIT: bool = True
    CONSECUTIVE_LIMIT_DAYS: int = 3
    LIMIT_TOLERANCE: float = 0.01

    # 连续涨跌天数限制
    MAX_CONSECUTIVE_UP_DAYS: int = 6
    MAX_CONSECUTIVE_DOWN_DAYS: int = 6

    # 整体平盘趋势限制（原为50日，现实际用于30日，参数值不变）
    MAX_50D_DECLINE_PCT: float = -0.06
    MAX_50D_RISE_PCT: float = 0.35
    MAX_10D_DECLINE_PCT: float = -0.05

    # 波动检测
    SWING_PROMINENCE_PCT: float = 0.015
    MIN_SWINGS: int = 2

    # 振幅一致性
    MAX_AMPLITUDE_CV: float = 0.47
    MIN_AVG_AMPLITUDE_PCT: float = 0.04
    MAX_AVG_AMPLITUDE_PCT: float = 0.35

    # 异常大振幅检测
    MAX_AMPLITUDE_RATIO_TO_AVG: float = 1.8
    MAX_AMPLITUDE_RATIO_TO_SECOND: float = 1.5

    # ---- 方向敏感版异常振幅（2026-10-29 v2，影子试跑）----
    # v1 的问题：段振幅只看相对比例(最大/次大)，深跌与暴涨同罪；
    # 且 amplitudes 只含 peak→trough 下跌段，暴涨段根本不在统计里。
    # v2 逻辑（对全部相邻段、带方向）：
    #   爆拉悬空：存在上涨段涨幅 > SPIKE_LEG_PCT 且现价仍高于该段起点×SPIKE_HOLD → 拒
    #   深跌未修复：存在下跌段跌幅 > DROP_LEG_PCT 且修复度(现价-谷)/(峰-谷) < REPAIR_MIN → 拒
    #   其余放行（深跌后已修复一半以上的视为可原谅的V形，不再因"比例悬殊"误杀）
    ABNORMAL_AMP_MODE: str = 'v1'        # 'v1'=现行相对比例  'v2'=方向敏感
    SPIKE_LEG_PCT: float = 0.15          # 单段涨幅超过 15% 才算爆拉
    SPIKE_HOLD: float = 1.08             # 且现价仍高出段起点 8%（涨幅被留住=悬空）
    DROP_LEG_PCT: float = 0.15           # 单段跌幅超过 15% 才算深跌
    DROP_REPAIR_MIN: float = 0.50        # 修复度不足 50% 才算未修复

    # 极端尖锐度控制
    EXTREME_SHARPNESS: float = 5.0
    MIN_PEAK_WIDTH: int = 2

    # 深V检测
    EXTREME_V_CHANGE_PCT: float = 0.12

    # ---- 静默票处理（2026-10-29 用户指令）----
    # 「未检测到波动」(30日内无可识别转折) 的票允许留在股票池：
    # 这类票往往是极度平滑的慢爬形态（如大型银行/权重股贴着年高磨），
    # 恰是本策略想要的核心画像。静默通过给固定中性分 QUIET_PASS_SCORE，
    # 等级标记 'Q' 以便与 zigzag 验证过的票区分；
    # 通过后续航闸门照常：现价/年高 ≥0.90、箱宽 ≤20%、换手、ROE 一个不少。
    ALLOW_QUIET_PASS: bool = True
    QUIET_PASS_SCORE: float = 70.0

    # 高低点趋势
    MAX_HIGH_RISE_PCT: float = 0.15
    ALLOW_TOLERANCE: float = 0.03

    # 近期
    RECENT_DAYS: int = 5
    RECENT_MAX_DECLINE_PCT: float = 0.12


RULES = PatternRules()


def compute_leg_metrics(swings_seq: List[Dict], last_close: float) -> Dict:
    """方向敏感的段指标（2026-10-29 v2 影子口径）。

    swings_seq 为 detect_swings 的交替峰谷点（含 type/price/idx）。
    对全部相邻段（含上涨段）计算带方向振幅，并给出 v2 判定：
      - 爆拉悬空：上涨段涨幅 > SPIKE_LEG_PCT 且现价仍高出段起点×SPIKE_HOLD
      - 深跌未修复：下跌段跌幅 > DROP_LEG_PCT 且修复度 < DROP_REPAIR_MIN
    """
    out = {'legs': [], 'max_up': 0.0, 'max_down': 0.0,
           'spike_leg': None, 'worst_drop': None,
           'v2_pass': True, 'v2_reason': ''}
    pts = list(swings_seq or [])
    if len(pts) < 2 or not last_close:
        return out
    legs = []
    for a, b in zip(pts[:-1], pts[1:]):
        pa, pb = float(a['price']), float(b['price'])
        if pa <= 0:
            continue
        chg = pb / pa - 1
        legs.append({'from': pa, 'to': pb, 'chg': chg,
                     'dir': 'up' if chg > 0 else 'down'})
    out['legs'] = legs
    ups = [l for l in legs if l['dir'] == 'up']
    downs = [l for l in legs if l['dir'] == 'down']
    if ups:
        out['max_up'] = max(l['chg'] for l in ups)
    if downs:
        out['max_down'] = max(-l['chg'] for l in downs)
    # 爆拉悬空：涨幅最大的那一段，现价是否仍悬在段起点之上
    if ups:
        top = max(ups, key=lambda l: l['chg'])
        if top['chg'] > RULES.SPIKE_LEG_PCT and last_close > top['from'] * RULES.SPIKE_HOLD:
            out['spike_leg'] = top
            out['v2_pass'] = False
            out['v2_reason'] = (f"爆拉悬空: 单段+{top['chg']:.1%}"
                                f"({top['from']:.2f}→{top['to']:.2f})且现价仍高出起点"
                                f"{last_close / top['from'] - 1:.1%}")
            return out
    # 深跌未修复：跌幅最大的那一段，现价修复了几成
    if downs:
        deep = max(downs, key=lambda l: -l['chg'])
        depth = -deep['chg']
        if depth > RULES.DROP_LEG_PCT:
            repair = (last_close - deep['to']) / (deep['from'] - deep['to']) \
                if deep['from'] > deep['to'] else 1.0
            deep['repair'] = repair
            out['worst_drop'] = deep
            if repair < RULES.DROP_REPAIR_MIN:
                out['v2_pass'] = False
                out['v2_reason'] = (f"深跌未修复: 单段{deep['chg']:.1%}"
                                    f"({deep['from']:.2f}→{deep['to']:.2f})"
                                    f"修复度仅{repair:.0%}")
    return out


def compute_central_zone(swings_seq: List[Dict], nlegs: int = 3) -> Dict:
    """缠论中枢：最近 nlegs 个「连续次级别走势」的价格重叠区间。

    缠论定义：中枢 = 被至少三个连续次级别走势类型所重叠的部分，
              ZG = min(各段高点)，ZD = max(各段低点)。
              若 ZD > ZG 说明三段没有重叠（单边走势，该级别无中枢）。

    swings_seq 是 detect_swings() 返回的点序列（峰用 high、谷用 low，交替排列），
    相邻两点之间即一个「段」，故 n 段需要 n+1 个点。
    """
    out = {'zg': None, 'zd': None, 'width': None, 'legs': 0, 'overlap': False}
    try:
        pts = list(swings_seq or [])
        if len(pts) < nlegs + 1:
            return out
        pts = pts[-(nlegs + 1):]
        highs, lows = [], []
        for a, b in zip(pts[:-1], pts[1:]):
            highs.append(max(float(a['price']), float(b['price'])))
            lows.append(min(float(a['price']), float(b['price'])))
        zg, zd = min(highs), max(lows)
        out.update({'zg': zg, 'zd': zd, 'legs': len(pts) - 1, 'overlap': zd <= zg})
        if zd > 0 and zd <= zg:
            out['width'] = (zg - zd) / zd
    except Exception:
        return out
    return out


def resolve_width_gate(result: Dict, volatility_50d: float) -> tuple:
    """选出「宽度口径」的 (数值, 名称, 上限)。

    影子模式（CENTRAL_ZONE_ENABLED=False）恒用外包络箱宽，与历史回测口径一致；
    开启中枢口径后优先用中枢重叠区宽度，算不出时按 CENTRAL_ZONE_FALLBACK 处理。
    """
    if RULES.CENTRAL_ZONE_ENABLED:
        cz = result.get('central_zone') or {}
        w = cz.get('width')
        if w is not None:
            return w, '中枢宽度', RULES.CENTRAL_ZONE_GATE
        if RULES.CENTRAL_ZONE_FALLBACK:
            return volatility_50d, '外包络(中枢不可用)', RULES.BOX_WIDTH_GATE
        return None, '中枢无重叠', None
    return volatility_50d, '30日箱宽', RULES.BOX_WIDTH_GATE


# ==========================================
# 持仓管理类（与原文件一致）
# ==========================================
class PositionManager:
    """
    管理已买入股票的触发买入价格和移除条件
    触发条件：分时价格 > 30日最高收盘价 × 1.05
    """

    def __init__(self):
        self.positions_file = str(OUT_DIR / 'positions_pro.json')
        self.positions = {}
        self.load_positions()

    def load_positions(self):
        try:
            if os.path.exists(self.positions_file):
                with open(self.positions_file, 'r', encoding='utf-8') as f:
                    self.positions = json.load(f)
                    print(f"✅ 已加载 {len(self.positions)} 条持仓记录")
        except Exception as e:
            print(f"⚠️ 加载持仓失败: {e}")
            self.positions = {}

    def save_positions(self):
        try:
            with open(self.positions_file, 'w', encoding='utf-8') as f:
                json.dump(self.positions, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 保存持仓失败: {e}")

    def check_buy_trigger(self, code: str, df_30: pd.DataFrame,
                          intraday_high: float = None) -> Tuple[bool, float]:
        if df_30.empty or len(df_30) < 5:
            return False, 0.0

        max_30d_close = df_30['close'].max()
        trigger_threshold = max_30d_close * (1 + RULES.TRIGGER_BUY_THRESHOLD)

        if intraday_high is not None and intraday_high > trigger_threshold:
            return True, intraday_high

        current_high = df_30['high'].iloc[-1] if 'high' in df_30.columns else df_30['close'].iloc[-1]
        if current_high > trigger_threshold:
            return True, current_high

        return False, 0.0

    def add_position(self, code: str, trigger_price: float, max_30d_high: float):
        self.positions[code] = {
            'trigger_price': float(trigger_price),
            'buy_date': datetime.now().strftime('%Y-%m-%d'),
            'max_30d_high': float(max_30d_high)
        }
        self.save_positions()
        print(f"✅ 添加持仓 {code}: 触发价={trigger_price:.2f}, 30日最高={max_30d_high:.2f}")

    def remove_position(self, code: str, reason: str):
        if code in self.positions:
            del self.positions[code]
            self.save_positions()
            print(f"🗑️ 移除 {code}: {reason}")
            return True
        return False

    def check_removal_conditions(self, code: str, current_close: float,
                                 df_30: pd.DataFrame) -> Tuple[bool, str]:
        if code not in self.positions:
            return False, "无持仓记录"

        pos = self.positions[code]
        trigger_price = pos['trigger_price']

        if trigger_price > 0:
            rise_pct = (current_close - trigger_price) / trigger_price
            if rise_pct >= RULES.TRIGGER_BUY_RISE_PCT:
                return True, (f"涨幅超20%(现{current_close:.2f}/触发价{trigger_price:.2f}, "
                              f"涨{rise_pct:.2%})")

        if not df_30.empty:
            close_30_max = df_30['close'].max()
            drop_threshold = close_30_max * (1 + RULES.MAX_DROP_FROM_HIGH_PCT)
            if current_close <= drop_threshold:
                drop_pct = (current_close - close_30_max) / close_30_max
                return True, (f"回撤超-20%(现{current_close:.2f}/30日高{close_30_max:.2f}, "
                              f"回撤{drop_pct:.2%})")

        return False, "保持持仓"


# ==========================================
# 形态分析器（与原文件逐行一致）
# ==========================================
class SmoothChannelAnalyzer:
    def __init__(self, df_50: pd.DataFrame, df_year: pd.DataFrame, df_30: pd.DataFrame = None):
        self.df_50 = df_50.reset_index(drop=True)
        self.df_year = df_year
        self.df_30 = df_30.reset_index(drop=True) if df_30 is not None else df_50.iloc[-30:].reset_index(drop=True)
        self.closes = df_50['close'].values
        self.highs = df_50['high'].values
        self.lows = df_50['low'].values
        self.amounts = df_50['amount'].values
        self.n = len(self.closes)

        self.prev_closes = np.roll(self.closes, 1)
        self.prev_closes[0] = np.nan

    def check_consecutive_trend(self) -> Tuple[bool, str, Dict]:
        result = {}
        if self.n < 2:
            return True, "数据不足", result

        directions = []
        for i in range(1, self.n):
            if self.prev_closes[i] > 0:
                change = self.closes[i] - self.prev_closes[i]
                directions.append(1 if change > 0 else (-1 if change < 0 else 0))
            else:
                directions.append(0)

        max_consecutive_up = max_consecutive_down = current_up = current_down = 0
        for d in directions:
            if d == 1:
                current_up += 1
                current_down = 0
                max_consecutive_up = max(max_consecutive_up, current_up)
            elif d == -1:
                current_down += 1
                current_up = 0
                max_consecutive_down = max(max_consecutive_down, current_down)
            else:
                current_up = current_down = 0

        result['max_consecutive_up'] = max_consecutive_up
        result['max_consecutive_down'] = max_consecutive_down

        if max_consecutive_up > RULES.MAX_CONSECUTIVE_UP_DAYS:
            return False, f"连续{max_consecutive_up}日上涨>允许{RULES.MAX_CONSECUTIVE_UP_DAYS}日", result
        if max_consecutive_down > RULES.MAX_CONSECUTIVE_DOWN_DAYS:
            return False, f"连续{max_consecutive_down}日下跌>允许{RULES.MAX_CONSECUTIVE_DOWN_DAYS}日", result

        return True, "通过", result

    def check_trend(self) -> Tuple[bool, str, Dict]:
        result = {}
        first_close, last_close = self.closes[0], self.closes[-1]
        total_change = (last_close - first_close) / first_close if first_close > 0 else 0.0
        result['50d_total_change'] = total_change

        if total_change < RULES.MAX_50D_DECLINE_PCT:
            return False, f"30日整体跌{total_change:.2%}<{RULES.MAX_50D_DECLINE_PCT:.0%}", result
        if total_change > RULES.MAX_50D_RISE_PCT:
            return False, f"30日整体涨{total_change:.2%}>{RULES.MAX_50D_RISE_PCT:.0%}", result

        if self.n >= 10:
            close_10d_ago = self.closes[-10]
            recent_change = (last_close - close_10d_ago) / close_10d_ago if close_10d_ago > 0 else 0.0
        else:
            recent_change = 0.0
        result['10d_change'] = recent_change

        if recent_change < RULES.MAX_10D_DECLINE_PCT:
            return False, f"近10日跌{recent_change:.2%}<{RULES.MAX_10D_DECLINE_PCT:.0%}", result
        return True, "通过", result

    def check_consecutive_limit(self) -> Tuple[bool, str]:
        if not RULES.CHECK_LIMIT or not hasattr(self, 'code'):
            return True, "跳过检测"

        limit_pct = 0.20 if (self.code.startswith('300') or self.code.startswith('301') or
                             self.code.startswith('688')) else 0.10

        pct_changes = [(self.closes[i] - self.prev_closes[i]) / self.prev_closes[i]
                       if self.prev_closes[i] > 0 else 0 for i in range(1, self.n)]

        is_limit_up = [p >= (limit_pct - RULES.LIMIT_TOLERANCE) for p in pct_changes]
        is_limit_down = [p <= (-limit_pct + RULES.LIMIT_TOLERANCE) for p in pct_changes]

        consecutive_up = consecutive_down = 0
        for i in range(len(is_limit_up)):
            if is_limit_up[i]:
                consecutive_up += 1
                if consecutive_up >= RULES.CONSECUTIVE_LIMIT_DAYS:
                    return False, f"连续{consecutive_up}日涨停"
            else:
                consecutive_up = 0

            if is_limit_down[i]:
                consecutive_down += 1
                if consecutive_down >= RULES.CONSECUTIVE_LIMIT_DAYS:
                    return False, f"连续{consecutive_down}日跌停"
            else:
                consecutive_down = 0
        return True, "无连续涨跌停"

    def is_extreme_v_shape(self, idx: int, is_peak: bool) -> Tuple[bool, str]:
        window = 5
        if idx < window or idx >= self.n - window:
            return False, ""

        if is_peak:
            before_prices = self.highs[idx - window:idx]
            after_prices = self.highs[idx:idx + window]
            rise_before = (self.highs[idx] - np.min(before_prices)) / np.min(before_prices) if np.min(
                before_prices) > 0 else 0
            fall_after = (self.highs[idx] - np.min(after_prices)) / self.highs[idx] if self.highs[idx] > 0 else 0
            if rise_before > RULES.EXTREME_V_CHANGE_PCT and fall_after > RULES.EXTREME_V_CHANGE_PCT:
                return True, f"倒深V(涨{rise_before:.1%}跌{fall_after:.1%})"
        else:
            before_prices = self.lows[idx - window:idx]
            after_prices = self.lows[idx:idx + window]
            fall_before = (np.max(before_prices) - self.lows[idx]) / np.max(before_prices) if np.max(
                before_prices) > 0 else 0
            rise_after = (np.max(after_prices) - self.lows[idx]) / self.lows[idx] if self.lows[idx] > 0 else 0
            if fall_before > RULES.EXTREME_V_CHANGE_PCT and rise_after > RULES.EXTREME_V_CHANGE_PCT:
                return True, f"深V(跌{fall_before:.1%}涨{rise_after:.1%})"
        return False, ""

    def calculate_sharpness(self, idx: int, is_peak: bool) -> float:
        if idx < 2 or idx >= self.n - 2:
            return 0
        if is_peak:
            left = abs(self.highs[idx] - self.highs[idx - 2]) / 2
            right = abs(self.highs[idx + 2] - self.highs[idx]) / 2
        else:
            left = abs(self.lows[idx] - self.lows[idx - 2]) / 2
            right = abs(self.lows[idx + 2] - self.lows[idx]) / 2
        return abs(left - right) / self.closes[idx] if self.closes[idx] > 0 else 0

    def check_rebound_pattern(self, swings: List[Dict]) -> Tuple[bool, str, Dict]:
        result = {
            'rebound_check_passed': False,
            'deviation_check_passed': False,
            'latest_deviation': 0.0,
            'rebound_details': []
        }

        if len(swings) < 4:
            return False, "波动次数不足", result

        peaks = [s for s in swings if s['type'] == 'peak']
        troughs = [s for s in swings if s['type'] == 'trough']

        if len(peaks) < 2 or len(troughs) < 2:
            return False, "高低点数量不足", result

        rebound_violations = 0
        for i in range(len(troughs) - 1):
            if i >= len(peaks):
                break

            if i < len(peaks) and i < len(troughs):
                prev_peak_price = peaks[i]['price']
                trough_price = troughs[i]['price']
                next_peak_price = peaks[i + 1]['price'] if i + 1 < len(peaks) else None

                if next_peak_price is None:
                    continue

                drop_pct = (prev_peak_price - trough_price) / prev_peak_price if prev_peak_price > 0 else 0
                rebound_pct = (next_peak_price - trough_price) / trough_price if trough_price > 0 else 0

                expected_rebound = prev_peak_price * (1 - RULES.REBOUND_TOLERANCE)
                actual_rebound_high = next_peak_price

                rebound_info = {
                    'wave': i + 1,
                    'drop_pct': drop_pct,
                    'rebound_pct': rebound_pct,
                    'prev_peak': prev_peak_price,
                    'trough': trough_price,
                    'next_peak': next_peak_price,
                    'expected_min': expected_rebound
                }

                if actual_rebound_high < expected_rebound:
                    rebound_violations += 1
                    rebound_info['status'] = 'FAIL'
                else:
                    rebound_info['status'] = 'PASS'

                result['rebound_details'].append(rebound_info)

        if rebound_violations > 2:
            return False, f"回弹不足次数:{rebound_violations}>2", result

        result['rebound_check_passed'] = True

        latest_close = self.closes[-1]
        recent_high = max(self.highs[-10:]) if len(self.highs) >= 10 else max(self.highs)

        if recent_high > 0:
            deviation = (recent_high - latest_close) / recent_high
            result['latest_deviation'] = deviation

            if deviation <= RULES.MAX_DEVIATION_FROM_HIGH:
                result['deviation_check_passed'] = True
                return True, "通过", result
            else:
                return False, f"偏离最近高价{deviation:.2%}>{RULES.MAX_DEVIATION_FROM_HIGH:.0%}", result
        else:
            return False, "无法计算偏离", result

    def detect_swings(self) -> Optional[Dict]:
        try:
            from scipy.signal import find_peaks
            price_mean = np.mean(self.closes)
            prominence = RULES.SWING_PROMINENCE_PCT * price_mean

            peaks_idx, _ = find_peaks(self.highs, prominence=prominence, distance=2)
            troughs_idx, _ = find_peaks(-self.lows, prominence=prominence, distance=2)

            peaks_idx = peaks_idx.tolist() if isinstance(peaks_idx, np.ndarray) else list(peaks_idx)
            troughs_idx = troughs_idx.tolist() if isinstance(troughs_idx, np.ndarray) else list(troughs_idx)

            if len(peaks_idx) < 2 or len(troughs_idx) < 2:
                return None

            valid_peaks, valid_troughs, extreme_rejected = [], [], []

            for idx in peaks_idx:
                is_extreme, reason = self.is_extreme_v_shape(int(idx), is_peak=True)
                sharpness = self.calculate_sharpness(int(idx), is_peak=True)
                peak_info = {'idx': int(idx), 'price': float(self.highs[int(idx)]), 'sharpness': sharpness}

                if is_extreme or sharpness > RULES.EXTREME_SHARPNESS:
                    extreme_rejected.append({**peak_info, 'reason': reason or f"尖锐度{sharpness:.2f}"})
                else:
                    valid_peaks.append(peak_info)

            for idx in troughs_idx:
                is_extreme, reason = self.is_extreme_v_shape(int(idx), is_peak=False)
                sharpness = self.calculate_sharpness(int(idx), is_peak=False)
                trough_info = {'idx': int(idx), 'price': float(self.lows[int(idx)]), 'sharpness': sharpness}

                if is_extreme or sharpness > RULES.EXTREME_SHARPNESS:
                    extreme_rejected.append({**trough_info, 'reason': reason or f"尖锐度{sharpness:.2f}"})
                else:
                    valid_troughs.append(trough_info)

            if len(valid_peaks) < RULES.MIN_SWINGS or len(valid_troughs) < RULES.MIN_SWINGS:
                if not (len(extreme_rejected) > 0 and len(valid_peaks) >= 2 and len(valid_troughs) >= 2):
                    return {'error': 'too_extreme', 'valid_peaks': len(valid_peaks),
                            'valid_troughs': len(valid_troughs), 'extreme_rejected': len(extreme_rejected)}

            all_points = valid_peaks + valid_troughs
            all_points.sort(key=lambda x: x['idx'])

            swings = []
            last_type = None
            for p in all_points:
                ptype = 'peak' if any(vp['idx'] == p['idx'] for vp in valid_peaks) else 'trough'
                if last_type != ptype:
                    swings.append({**p, 'type': ptype})
                    last_type = ptype

            if len(swings) < 4:
                return None
            if swings[0]['type'] != 'peak':
                swings = swings[1:]
            if swings[-1]['type'] != 'trough':
                swings = swings[:-1]
            if len(swings) < 4 or len(swings) % 2 != 0:
                return None

            amplitudes = []
            for i in range(0, len(swings) - 1, 2):
                if i + 1 < len(swings):
                    peak_p = swings[i]['price']
                    trough_p = swings[i + 1]['price']
                    amp = (peak_p - trough_p) / peak_p if peak_p > 0 else 0
                    amplitudes.append({
                        'n': i // 2 + 1,
                        'peak': peak_p,
                        'trough': trough_p,
                        'amplitude': amp
                    })

            if len(amplitudes) < 2:
                return None

            return {
                'swings': swings,
                'peaks': [s for s in swings if s['type'] == 'peak'],
                'troughs': [s for s in swings if s['type'] == 'trough'],
                'amplitudes': amplitudes,
                'n_count': len(amplitudes),
                'extreme_rejected': len(extreme_rejected),
                'avg_peak_sharpness': np.mean([p['sharpness'] for p in valid_peaks]) if valid_peaks else 0,
                'avg_trough_sharpness': np.mean([t['sharpness'] for t in valid_troughs]) if valid_troughs else 0
            }
        except Exception as e:
            print(f"detect错误: {e}")
            return None

    def check_abnormal_amplitude(self, amplitudes: List[Dict]) -> Tuple[bool, str]:
        if len(amplitudes) < 3:
            return True, "振幅数量不足"

        amp_values = sorted([a['amplitude'] for a in amplitudes], reverse=True)
        max_amp, second_amp, avg_amp = amp_values[0], amp_values[1] if len(amp_values) > 1 else 0, np.mean(amp_values)

        if avg_amp > 0 and max_amp / avg_amp > RULES.MAX_AMPLITUDE_RATIO_TO_AVG:
            return False, f"最大振幅{max_amp:.2%}是平均{avg_amp:.2%}的{max_amp / avg_amp:.1f}倍"
        if second_amp > 0 and max_amp / second_amp > RULES.MAX_AMPLITUDE_RATIO_TO_SECOND:
            return False, f"最大振幅{max_amp:.2%}是次大{second_amp:.2%}的{max_amp / second_amp:.1f}倍"
        return True, ""

    def check_pattern(self, swing_data: Dict) -> Tuple[bool, str, Dict]:
        result = {
            'n_count': swing_data.get('n_count', 0),
            'extreme_rejected': swing_data.get('extreme_rejected', 0),
            'avg_peak_sharpness': swing_data.get('avg_peak_sharpness', 0),
            'avg_trough_sharpness': swing_data.get('avg_trough_sharpness', 0),
            'amplitude_mean': 0,
            'amplitude_cv': 999,
            'lows_progression': [],
            'highs_progression': [],
            'total_high_rise': 0,
            'recent_5d_change': 0,
            'rebound_check_passed': False,
            'deviation_from_high': 0.0
        }

        swings = swing_data.get('swings', [])
        amplitudes = swing_data.get('amplitudes', [])
        peaks = swing_data.get('peaks', [])
        troughs = swing_data.get('troughs', [])

        if result['n_count'] < RULES.MIN_SWINGS:
            return False, f"起伏{result['n_count']}<{RULES.MIN_SWINGS}", result

        if RULES.ABNORMAL_AMP_MODE == 'v2':
            lm = compute_leg_metrics(swings, float(self.closes[-1]) if len(self.closes) else 0)
            result['leg_metrics'] = lm
            amp_pass, amp_reason = lm['v2_pass'], (lm['v2_reason'] or 'v2通过')
        else:
            amp_pass, amp_reason = self.check_abnormal_amplitude(amplitudes)
        if not amp_pass:
            return False, f"异常大振幅: {amp_reason}", result

        amp_values = [a['amplitude'] for a in amplitudes]
        result['amplitude_mean'] = float(np.mean(amp_values))
        result['amplitude_std'] = float(np.std(amp_values))
        result['amplitude_cv'] = float(result['amplitude_std'] / result['amplitude_mean']) if result[
                                                                                                  'amplitude_mean'] > 0 else 999

        if result['amplitude_cv'] > RULES.MAX_AMPLITUDE_CV:
            return False, f"振幅不均(CV={result['amplitude_cv']:.2f}>{RULES.MAX_AMPLITUDE_CV})", result

        avg_amp = result['amplitude_mean']
        if not (RULES.MIN_AVG_AMPLITUDE_PCT <= avg_amp <= RULES.MAX_AVG_AMPLITUDE_PCT):
            return False, f"振幅{avg_amp:.2%}不在{RULES.MIN_AVG_AMPLITUDE_PCT:.0%}-{RULES.MAX_AVG_AMPLITUDE_PCT:.0%}", result

        trough_prices = [t['price'] for t in troughs]
        low_violations = 0
        for i in range(1, len(trough_prices)):
            ratio = trough_prices[i] / trough_prices[i - 1] if trough_prices[i - 1] > 0 else 1
            result['lows_progression'].append(ratio)
            if ratio < (1 - RULES.ALLOW_TOLERANCE):
                low_violations += 1
        if low_violations > 2:
            return False, f"低点下降{low_violations}次>1", result

        peak_prices = [p['price'] for p in peaks]
        high_violations = 0
        result['total_high_rise'] = (peak_prices[-1] - peak_prices[0]) / peak_prices[0] if peak_prices[0] > 0 else 0

        for i in range(1, len(peak_prices)):
            ratio = peak_prices[i] / peak_prices[i - 1] if peak_prices[i - 1] > 0 else 1
            result['highs_progression'].append(ratio)
            if ratio < (1 - RULES.ALLOW_TOLERANCE) or (ratio - 1) > 0.06:
                high_violations += 1

        if high_violations > 2 or result['total_high_rise'] > RULES.MAX_HIGH_RISE_PCT:
            return False, f"高点违规{high_violations}或累计{result['total_high_rise']:.1%}>{RULES.MAX_HIGH_RISE_PCT}", result

        rebound_pass, rebound_reason, rebound_data = self.check_rebound_pattern(swings)
        result['rebound_check_passed'] = rebound_data['rebound_check_passed']
        result['deviation_from_high'] = rebound_data['latest_deviation']

        if not rebound_pass:
            return False, f"回弹形态: {rebound_reason}", result

        result['recent_5d_change'] = (self.closes[-1] - self.closes[-RULES.RECENT_DAYS]) / self.closes[
            -RULES.RECENT_DAYS] if self.closes[-RULES.RECENT_DAYS] > 0 else 0
        if result['recent_5d_change'] < -RULES.RECENT_MAX_DECLINE_PCT:
            return False, f"近5日跌{result['recent_5d_change']:.2%}", result

        return True, "通过", result

    def check_hard_filters(self) -> Tuple[bool, str, Dict]:
        result = {}

        # 1. 日均成交额
        avg_amount = float(np.mean(self.amounts))
        result['avg_amount_yi'] = avg_amount / 1e8
        if avg_amount < RULES.MIN_AVG_AMOUNT:
            return False, (f"成交{result['avg_amount_yi']:.2f}亿"
                           f"<{RULES.MIN_AVG_AMOUNT / 1e8:.0f}亿"), result

        # 2. 30日最高收盘价 vs 250日最高收盘价
        high_30 = float(np.max(self.df_30['close'].values))
        high_250 = float(np.max(self.df_year['close'].values))
        ratio = high_30 / high_250 if high_250 > 0 else 0
        result['high_30_close'] = high_30
        result['high_250_close'] = high_250
        result['year_high_ratio'] = ratio

        if ratio < RULES.YEAR_HIGH_RATIO:
            return False, f"30日高{high_30:.2f}<250日高{high_250:.2f}×90%", result

        low_30 = float(np.min(self.df_30['close'].values))
        result['low_30_close'] = low_30

        # 3. 连续涨跌停板
        limit_pass, limit_reason = self.check_consecutive_limit()
        if not limit_pass:
            return False, f"涨跌停: {limit_reason}", result

        # 4. 整体平盘趋势
        trend_pass, trend_reason, trend_data = self.check_trend()
        result.update(trend_data)
        if not trend_pass:
            return False, f"趋势: {trend_reason}", result

        # 5. 连续上涨/下跌天数限制
        consec_pass, consec_reason, consec_data = self.check_consecutive_trend()
        result.update(consec_data)
        if not consec_pass:
            return False, f"连续趋势: {consec_reason}", result

        return True, "通过", result

    def calculate_score(self, check_result: Dict) -> Dict:
        scores = {}
        avg_sharp = (check_result.get('avg_peak_sharpness', 0) + check_result.get('avg_trough_sharpness', 0)) / 2

        if avg_sharp <= 1.0:
            scores['smoothness'] = 25
        elif avg_sharp <= 2.0:
            scores['smoothness'] = 22
        elif avg_sharp <= 4.0:
            scores['smoothness'] = 18
        else:
            scores['smoothness'] = 15

        cv = check_result.get('amplitude_cv', 1)
        if cv <= 0.30:
            scores['amplitude'] = 25
        elif cv <= 0.40:
            scores['amplitude'] = 22
        elif cv <= 0.50:
            scores['amplitude'] = 18
        else:
            scores['amplitude'] = 12

        n = check_result.get('n_count', 0)
        scores['swings'] = 20 if n >= 5 else (18 if n >= 4 else 15)

        rise = check_result.get('total_high_rise', 0)
        if rise <= 0.05:
            scores['trend'] = 15
        elif rise <= 0.08:
            scores['trend'] = 12
        else:
            scores['trend'] = 10

        recent = check_result.get('recent_5d_change', 0)
        if recent > 0:
            scores['recent'] = 10
        elif recent > -0.03:
            scores['recent'] = 8
        else:
            scores['recent'] = 6

        if check_result.get('rebound_check_passed', False):
            scores['rebound'] = 5
        else:
            scores['rebound'] = 0

        total = sum(scores.values())
        return {
            'total_score': total,
            'score_details': scores,
            'grade': 'A' if total >= 85 else 'B' if total >= 75 else 'C' if total >= 65 else 'D'
        }

    def analyze(self) -> Dict[str, Any]:
        result = {
            'passed': False,
            'fail_stage': '',
            'fail_reason': '',
            'hard_filter': {},
            'swing_data': {'n_count': 0, 'extreme_rejected': 0},
            'pattern_check': {'amplitude_cv': 999, 'recent_5d_change': 0, 'deviation_from_high': 0},
            'quality_score': {'total_score': 0, 'grade': 'F'}
        }

        try:
            passed, reason, hard_data = self.check_hard_filters()
            result['hard_filter'] = hard_data
            if not passed:
                result['fail_stage'] = '硬性'
                result['fail_reason'] = reason
                return result

            swings = self.detect_swings()
            if swings is None:
                if RULES.ALLOW_QUIET_PASS:
                    # 静默通过：无可识别转折 = 极度平滑，留在池中，打 'Q' 标记
                    result['passed'] = True
                    result['quiet_pass'] = True
                    result['central_zone'] = compute_central_zone([], RULES.CENTRAL_ZONE_LEGS)
                    result['leg_metrics'] = compute_leg_metrics([], 0.0)
                    result['quality_score'] = {
                        'total_score': RULES.QUIET_PASS_SCORE,
                        'score_details': {'quiet': RULES.QUIET_PASS_SCORE},
                        'grade': 'Q'}
                    return result
                result['fail_stage'] = '形态'
                result['fail_reason'] = '未检测到波动'
                return result
            if isinstance(swings, dict) and swings.get('error') == 'too_extreme':
                result['fail_stage'] = '极端形态'
                result['fail_reason'] = f"极端顶点过多({swings.get('extreme_rejected', 0)}个)"
                return result

            result['swing_data'] = {
                'n_count': swings.get('n_count', 0),
                'extreme_rejected': swings.get('extreme_rejected', 0),
                'avg_peak_sharpness': swings.get('avg_peak_sharpness', 0),
                'avg_trough_sharpness': swings.get('avg_trough_sharpness', 0)
            }

            # 缠论中枢宽度（2026-10-29）：恒计算，是否用于拦截由 RULES.CENTRAL_ZONE_ENABLED 决定
            seq = swings.get('swings', []) if isinstance(swings, dict) else []
            result['swings_seq'] = seq
            result['central_zone'] = compute_central_zone(seq, RULES.CENTRAL_ZONE_LEGS)
            # 方向敏感段指标（2026-10-29 影子列）：无论 ABNORMAL_AMP_MODE 是 v1/v2 都计算输出
            _lc = float(self.closes[-1]) if len(getattr(self, 'closes', [])) else 0.0
            if 'leg_metrics' not in result:
                result['leg_metrics'] = compute_leg_metrics(seq, _lc)

            passed, reason, check_data = self.check_pattern(swings)
            result['pattern_check'] = check_data
            result['fail_reason'] = reason
            if not passed:
                result['fail_stage'] = '形态'
                return result

            result['passed'] = True
            result['quality_score'] = self.calculate_score(check_data)

        except Exception as e:
            result['fail_stage'] = '错误'
            result['fail_reason'] = str(e)

        return result


# ==========================================
# 全市场扫描器（数据层换成本地湖）
# ==========================================
class MarketScreener:
    def __init__(self):
        self.position_manager = PositionManager()
        self._bars = None          # 一次性加载的前复权日K（全市场）
        self._by_symbol = None     # symbol → DataFrame 索引
        self._fund = None          # 基本面/股本缓存（懒加载）

    def _fundamental(self) -> dict:
        """懒加载 out/fundamental_cache.parquet → {symbol: (流通股本, ROE, 行业)}"""
        if self._fund is None:
            self._fund = {}
            try:
                fc = pd.read_parquet(OUT_DIR / 'fundamental_cache.parquet')
                for r in fc.itertuples(index=False):
                    roe = getattr(r, 'roe_pct', None)
                    self._fund[str(r.symbol)] = (
                        getattr(r, 'liutongguben', None),
                        float(roe) if roe is not None and roe == roe else None,
                        str(getattr(r, 'csrc_industry', '') or ''),
                    )
            except Exception as e:
                print(f"[警告] 基本面缓存不可用，换手率/ROE 将为空: {e}")
        return self._fund

    def get_stocks(self) -> List[str]:
        """股票列表：湖内 instruments 表（前缀过滤与原版一致）

        2026-10-23 新增两道卫生过滤（均只用湖内已有字段，零新增数据源）：
          1) 名称含「ST / 退」→ 剔除。ST 票的涨跌停 5% 会让本策略的
             「连续涨跌停 3 天」「振幅 CV」等形态判据失真，且流动性风险不可控。
          2) 上市未满 MIN_LIST_DAYS 天 → 剔除。次新股没有 250 日年高可算，
             YEAR_HIGH_RATIO 恒等于 1，等于无条件放行（001381.SZ 即此类）。
        """
        _require_lake()
        import duckdb
        con = duckdb.connect()
        try:
            p = lake_reader.DATA_ROOT / 'curated' / 'instruments'
            df = con.execute(
                f"SELECT symbol, name, list_date, delist_date FROM read_parquet('{p}/**/*.parquet'"
                f", hive_partitioning=0)").df()
        finally:
            con.close()
        prefixes = ('600', '601', '603', '605', '688', '000', '001', '002', '003', '300', '301')
        latest = self._latest_trade_date()
        from datetime import datetime as _dt
        try:
            latest_dt = _dt.strptime(latest, '%Y-%m-%d')
        except Exception:
            latest_dt = None

        # 3) 行业剔除（yh 清单：银行/基建/铁路公路/航运港口/航空机场/物流/通信服务）
        #    数据来自 out/fundamental_cache.parquet（baostock 证监会分类，每周重建一次）
        excl_ind = set()
        if RULES.EXCLUDE_INDUSTRY_ENABLED:
            try:
                fc = pd.read_parquet(OUT_DIR / 'fundamental_cache.parquet')
                excl_ind = set(fc.loc[fc['is_excluded_industry'].fillna(False), 'symbol'])
                print(f"行业剔除清单已加载：{len(excl_ind)} 只")
            except Exception as e:
                print(f"[警告] 行业缓存不可用，跳过行业剔除: {e}")

        out, n_st, n_new, n_ind = [], 0, 0, 0
        for _, r in df.iterrows():
            sym = r['symbol']
            if not sym.startswith(prefixes):
                continue
            dd = r.get('delist_date')
            if dd is not None and str(dd)[:10] < latest:
                continue  # 已退市
            # 3) 行业剔除
            if sym in excl_ind:
                n_ind += 1
                continue
            # 1) ST / 退市整理期
            nm = str(r.get('name') or '')
            if nm and re.search(RULES.EXCLUDE_NAME_PATTERN, nm):
                n_st += 1
                continue
            # 2) 次新股
            ld = r.get('list_date')
            if latest_dt is not None and ld is not None and str(ld) not in ('', 'NaT', 'None'):
                try:
                    ld_dt = pd.to_datetime(ld)
                    if (latest_dt - ld_dt).days < RULES.MIN_LIST_DAYS:
                        n_new += 1
                        continue
                except Exception:
                    pass
            out.append(sym)
        print(f"获取到 {len(out)} 只股票（截至 {latest}）；"
              f"已剔除 ST/退 {n_st} 只、上市未满{RULES.MIN_LIST_DAYS}日 {n_new} 只、"
              f"剔除行业 {n_ind} 只")
        return sorted(out)

    def _latest_trade_date(self) -> str:
        if self._bars is not None and not self._bars.empty:
            return str(self._bars['trade_date'].max().date())
        _require_lake()
        import duckdb
        con = duckdb.connect()
        try:
            p = lake_reader.DATA_ROOT / 'curated' / 'daily_bars'
            r = con.execute(
                f"SELECT MAX(CAST(trade_date AS VARCHAR)) FROM read_parquet('{p}/**/*.parquet'"
                f", hive_partitioning=0)").fetchone()
            return r[0][:10]
        finally:
            con.close()

    def load_market_data(self, stocks: List[str]):
        """一次性加载全部股票的 250+ 日前复权日K（替代逐只 xtdata 调用）"""
        _require_lake()
        from datetime import timedelta
        end = self._latest_trade_date()
        start = (datetime.strptime(end, '%Y-%m-%d') - timedelta(days=560)).strftime('%Y-%m-%d')
        print(f"加载本地湖数据: {start} ~ {end}（前复权）...")
        df = lake_reader.load_daily_bars(stocks, start=start, end=end, adjust='qfq')
        df = df[df['close'] > 0]
        self._bars = df
        self._by_symbol = {sym: g.reset_index(drop=True) for sym, g in df.groupby('symbol')}
        print(f"加载完成: {len(self._by_symbol)} 只有数据, 共 {len(df)} 行")

    def analyze_one(self, code: str, check_removal: bool = False,
                    intraday_data: Dict = None) -> Dict:
        result = {
            'stock_code': code,
            'passed': False,
            'fail_stage': '',
            'fail_reason': '',
            'removed': False,
            'removal_reason': '',
            'buy_triggered': False,
            'trigger_price': 0.0
        }

        try:
            df_all = self._by_symbol.get(code)
            if df_all is None or df_all.empty:
                result['fail_reason'] = '无30日数据'
                return result

            # 历史长度门槛：250日窗口内 bar 太少 → 次新或长期停牌，年高口径不可信
            if len(df_all) < RULES.MIN_BARS_250:
                result['fail_reason'] = f'历史不足({len(df_all)}根<{RULES.MIN_BARS_250})'
                return result

            df50 = df_all.tail(RULES.LOOKBACK_DAYS)
            if len(df50) < 20:
                result['fail_reason'] = f'数据不足({len(df50)}天)'
                return result

            close_max = df50['close'].max()
            close_min = df50['close'].min()
            volatility_50d = (close_max - close_min) / close_min if close_min > 0 else 0
            result['volatility_50d'] = volatility_50d
            # ↑ 这个量的真实含义是「30日区间振幅/箱宽」，不是标准差意义上的波动率。
            #   旧版把它命名为「30日波动率(收盘价)」，容易与真正的波动率混淆，
            #   导致误以为本策略有波动率过滤。输出列名已更正为「30日箱宽(区间振幅)」。

            df30 = df50.copy()
            df250 = df_all.tail(RULES.YEAR_DAYS)
            if df250.empty:
                result['fail_reason'] = '无250日数据'
                return result

            current_close = float(df50['close'].iloc[-1])

            # 年高接近度「当前价口径」：yh_screen 经回测后采用，比现行的
            # 「近30日高/250日高」更贴合「距年高」原意——旧口径会放行
            # 「从年高回落 15% 后横盘」的票。此处恒计算并输出，是否拦截见 RULES.PROX_NOW_GATE。
            _hi250 = float(df250['close'].max())
            result['prox_now'] = current_close / _hi250 if _hi250 > 0 else 0.0

            # 检查是否触发买入（如果未持仓）
            if code not in self.position_manager.positions:
                intraday_high = intraday_data.get('high') if intraday_data else None
                triggered, trigger_price = self.position_manager.check_buy_trigger(
                    code, df30, intraday_high
                )
                if triggered:
                    result['buy_triggered'] = True
                    result['trigger_price'] = trigger_price
                    max_30d = df30['close'].max()
                    self.position_manager.add_position(code, trigger_price, max_30d)

            # 检查移除条件（如果已持仓）
            if check_removal and code in self.position_manager.positions:
                should_remove, removal_reason = self.position_manager.check_removal_conditions(
                    code, current_close, df30
                )
                if should_remove:
                    result['removed'] = True
                    result['removal_reason'] = removal_reason
                    self.position_manager.remove_position(code, removal_reason)
                    return result

            # 形态分析
            analyzer = SmoothChannelAnalyzer(df50, df250, df30)
            analyzer.code = code
            analysis = analyzer.analyze()
            result.update(analysis)

            hf = analysis.get('hard_filter', {})
            result['avg_amount_yi'] = hf.get('avg_amount_yi', 0)
            result['high_30_close'] = hf.get('high_30_close', 0)
            result['low_30_close'] = hf.get('low_30_close', 0)
            result['high_250_close'] = hf.get('high_250_close', 0)
            result['year_high_ratio'] = hf.get('year_high_ratio', 0)
            result['50d_total_change'] = hf.get('50d_total_change', 0)
            result['10d_change'] = hf.get('10d_change', 0)
            result['max_consecutive_up'] = hf.get('max_consecutive_up', 0)
            result['max_consecutive_down'] = hf.get('max_consecutive_down', 0)

            # ---- 资金流活跃度 + 基本面（fundamental_cache，2026-10-23 新增）----
            # 换手率 = 湖内日线 volume / 流通股本。湖内 volume 单位已验证为「股」
            # （amount/volume ≈ close），故无需 ×100。与 baostock 实测值逐票吻合。
            fund = self._fundamental().get(code)
            if fund:
                shares, roe, ind = fund
                result['csrc_industry'] = ind
                result['roe_pct'] = roe
                if shares and shares > 0 and 'volume' in df50.columns:
                    result['turn_30d'] = float(df50['volume'].mean()) / shares * 100

            # ---- 可选拦截（默认关闭，仅输出指标）----
            # 这两条是 yh_screen 回测校准后的口径。改 RULES.PROX_NOW_GATE / BOX_WIDTH_GATE
            # 即可启用，不必动形态逻辑。
            # 宽度口径：默认外包络箱宽；开中枢口径后自动切换（见 resolve_width_gate）
            width_value, width_label, width_gate = resolve_width_gate(result, volatility_50d)
            result['width_metric'] = width_label
            result['width_value'] = width_value
            result['width_gate'] = width_gate

            # 中枢偏离度提前计算（闸门链要用）：现价/中枢上沿 ZG - 1。
            # 算不出中枢（无转折/三段无重叠）时为 None，对应闸门自动跳过。
            _cz_pre = analysis.get('central_zone') or {}
            _zg_pre = _cz_pre.get('zg')
            _last_close_pre = float(df50['close'].iloc[-1]) if len(df50) else None
            result['dev_from_zg'] = (_last_close_pre / _zg_pre - 1) if (_zg_pre and _last_close_pre) else None

            if result.get('passed'):
                if RULES.PROX_NOW_GATE is not None and result['prox_now'] < RULES.PROX_NOW_GATE:
                    result['passed'] = False
                    result['fail_stage'] = '年高口径'
                    result['fail_reason'] = (f"当前价/年高{result['prox_now']:.3f}"
                                             f"<{RULES.PROX_NOW_GATE:.2f}"
                                             f"（距年高{result['prox_now'] - 1:.1%}）")
                elif (RULES.DEV_FROM_ZG_ENABLED
                      and result.get('dev_from_zg') is not None
                      and result['dev_from_zg'] > RULES.DEV_FROM_ZG_GATE):
                    # 先于宽度闸：延伸段票（如中远海控+9.2%）箱宽往往合格，必须单独拦
                    result['passed'] = False
                    result['fail_stage'] = '中枢偏离'
                    result['fail_reason'] = (f"现价高于中枢上沿{result['dev_from_zg']:.1%}"
                                             f">{RULES.DEV_FROM_ZG_GATE:.0%}（延伸段不追）")
                elif (width_gate is not None and width_value is not None
                      and width_value > width_gate):
                    result['passed'] = False
                    result['fail_stage'] = '宽度'
                    result['fail_reason'] = f"{width_label}{width_value:.1%}>{width_gate:.0%}"
                elif (RULES.TURN_MAX_30D is not None
                      and result.get('turn_30d') is not None
                      and result['turn_30d'] > RULES.TURN_MAX_30D):
                    result['passed'] = False
                    result['fail_stage'] = '换手率'
                    result['fail_reason'] = f"30日均换手{result['turn_30d']:.2f}%>{RULES.TURN_MAX_30D}%"
                elif (RULES.MIN_ROE is not None
                      and result.get('roe_pct') is not None
                      and result['roe_pct'] < RULES.MIN_ROE):
                    result['passed'] = False
                    result['fail_stage'] = 'ROE'
                    result['fail_reason'] = f"ROE {result['roe_pct']:.1f}%<{RULES.MIN_ROE}%"

            sw = analysis.get('swing_data', {})
            result['n_count'] = sw.get('n_count', 0)
            result['extreme_rejected'] = sw.get('extreme_rejected', 0)
            result['avg_peak_sharpness'] = sw.get('avg_peak_sharpness', 0)
            result['avg_trough_sharpness'] = sw.get('avg_trough_sharpness', 0)

            cz = analysis.get('central_zone') or {}
            result['central_zone_width'] = cz.get('width')
            result['central_zone_zg'] = cz.get('zg')
            result['central_zone_zd'] = cz.get('zd')
            result['central_zone_legs'] = cz.get('legs', 0)
            result['central_zone_overlap'] = cz.get('overlap', False)
            # 中枢偏离度（2026-10-29 影子列 → 2026-11-03 转正为闸门，见上方 DEV_FROM_ZG_*）：
            # 现价相对中枢上沿 ZG 的偏离 = close/ZG - 1。值已在闸门链前算好，这里不再重复。

            # 方向敏感段指标（影子列）：v2 口径下的最大涨/跌段与判定
            lm = analysis.get('leg_metrics') or {}
            result['max_leg_up'] = lm.get('max_up', 0)
            result['max_leg_down'] = lm.get('max_down', 0)
            wd = lm.get('worst_drop') or {}
            result['drop_repair'] = wd.get('repair')
            result['amp_v2_pass'] = lm.get('v2_pass')
            result['amp_v2_reason'] = lm.get('v2_reason', '')

            pc = analysis.get('pattern_check', {})
            result['amplitude_mean'] = pc.get('amplitude_mean', 0)
            result['amplitude_cv'] = pc.get('amplitude_cv', 999)
            result['total_high_rise'] = pc.get('total_high_rise', 0)
            result['recent_5d_change'] = pc.get('recent_5d_change', 0)
            result['deviation_from_high'] = pc.get('deviation_from_high', 0)
            result['rebound_check_passed'] = pc.get('rebound_check_passed', False)

            qs = analysis.get('quality_score', {})
            result['total_score'] = qs.get('total_score', 0)
            result['grade'] = qs.get('grade', 'F')
            result['latest_price'] = current_close

        except Exception as e:
            result['fail_reason'] = f'错误: {str(e)}'

        return result

    def scan(self, max_stocks: int = None, check_existing: bool = True):
        stocks = self.get_stocks()
        if max_stocks:
            stocks = stocks[:max_stocks]

        self.load_market_data(stocks)

        results = []
        total = len(stocks)

        print(f"\n扫描 {total} 只股票...")
        print("=" * 80)
        print("平滑等幅上升震荡形态（专业版 · 本地数据湖）:")
        print("  ✓ 30日最高收盘价 ≥ 250日最高×90%")
        print("  ✓ 触发买入: 分时价格 > 30日最高收盘价×1.05")
        print("  ✓ 移除条件1: 收盘价 ≥ 触发买入价×1.20")
        print("  ✓ 移除条件2: 收盘价 ≤ 30日最高收盘价×0.80")
        print("  ✓ 形态约束:")
        print("      - 下跌后回弹接近/超过前高")
        print("      - 最新价离最近最高价偏离 ≤ 5%")
        print("  ✓ 剔除连续涨跌停板/异常振幅/8日连涨连跌")
        print("  ✓ 整体平盘: 30日涨跌幅 -5%~+10%, 近10日跌幅≤3%")
        print("=" * 80)

        removed_positions = []
        if check_existing and self.position_manager.positions:
            print(f"\n检查 {len(self.position_manager.positions)} 个现有持仓的移除条件...")
            for code in list(self.position_manager.positions.keys()):
                if code in stocks:
                    r = self.analyze_one(code, check_removal=True)
                    if r.get('removed'):
                        removed_positions.append(r)
                    results.append(r)
            print(f"移除 {len(removed_positions)} 个持仓")

        for i, code in enumerate(stocks):
            if i % 500 == 0 and i > 0:
                print(f"进度: {i}/{total} ({i / total * 100:.1f}%)")

            if check_existing and code in self.position_manager.positions:
                continue

            r = self.analyze_one(code, check_removal=False)
            results.append(r)

        df = pd.DataFrame(results)

        passed = df[df['passed'] == True]
        failed = df[df['passed'] == False]
        removed = df[df['removed'] == True]
        triggered = df[df['buy_triggered'] == True]

        print("\n" + "=" * 80)
        print(f"总计: {len(df)} | 通过: {len(passed)} | 淘汰: {len(failed)} | "
              f"移除: {len(removed)} | 新触发: {len(triggered)}")

        if len(triggered) > 0:
            print(f"\n★ 新触发买入:")
            for _, r in triggered.iterrows():
                print(f"  {r['stock_code']} @ {r['trigger_price']:.2f}")

        if len(removed) > 0:
            print(f"\n★ 移除的持仓:")
            for _, r in removed.iterrows():
                print(f"  {r['stock_code']} - {r['removal_reason']}")

        if len(failed) > 0:
            print("\n淘汰原因TOP10:")
            reasons = failed['fail_reason'].value_counts().head(10)
            for reason, count in reasons.items():
                print(f"  {count:4d}只 - {reason}")

        if len(passed) > 0:
            passed = passed.sort_values('total_score', ascending=False)
            print(f"\n通过的A/B级股票:")
            top = passed[passed['grade'].isin(['A', 'B'])].head(15)
            for _, r in top.iterrows():
                print(f"  {r['stock_code']} {r['grade']}级{r['total_score']:.0f}分 | "
                      f"起伏:{r['n_count']}次 | 振幅CV:{r['amplitude_cv']:.2f} | "
                      f"高点累计:{r['total_high_rise']:.1%} | "
                      f"偏离高价:{r.get('deviation_from_high', 0):.2%} | "
                      f"回弹检查:{'通过' if r.get('rebound_check_passed') else '失败'} | "
                      f"30日涨跌:{r.get('50d_total_change', 0):.2%}")

        return df

    def save(self, df: pd.DataFrame, ts: str = None):
        if ts is None:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')

        fname = str(OUT_DIR / f'平滑震荡形态_专业版_{ts}.xlsx')

        cols = ['stock_code', 'passed', 'removed', 'removal_reason', 'buy_triggered', 'trigger_price',
                'fail_stage', 'fail_reason', 'grade', 'total_score',
                'avg_amount_yi', 'high_30_close', 'low_30_close', 'high_250_close', 'year_high_ratio',
                'n_count', 'extreme_rejected', 'avg_peak_sharpness', 'avg_trough_sharpness',
                'amplitude_mean', 'amplitude_cv', 'total_high_rise', 'recent_5d_change',
                'latest_price', 'volatility_50d', 'deviation_from_high', 'rebound_check_passed',
                '50d_total_change', '10d_change', 'max_consecutive_up', 'max_consecutive_down',
                'prox_now', 'csrc_industry', 'turn_30d', 'roe_pct',
                'central_zone_width', 'central_zone_zg', 'central_zone_zd',
                'central_zone_legs', 'width_metric', 'width_value']

        cols = [c for c in cols if c in df.columns]
        df_out = df[cols].copy()

        col_names = {
            'stock_code': '股票代码',
            'passed': '是否通过',
            'removed': '是否移除',
            'removal_reason': '移除原因',
            'buy_triggered': '触发买入',
            'trigger_price': '触发价格',
            'fail_stage': '淘汰阶段',
            'fail_reason': '淘汰原因',
            'grade': '等级',
            'total_score': '总分',
            'avg_amount_yi': '日均成交(亿)',
            'high_30_close': '30日最高收盘',
            'low_30_close': '30日最低收盘',
            'high_250_close': '250日最高收盘',
            'year_high_ratio': '年高比例',
            'n_count': '起伏次数',
            'extreme_rejected': '过滤极端顶点',
            'avg_peak_sharpness': '高点尖锐度',
            'avg_trough_sharpness': '低点尖锐度',
            'amplitude_mean': '平均振幅',
            'amplitude_cv': '振幅变异系数',
            'total_high_rise': '高点累计涨幅',
            'recent_5d_change': '近5日涨跌',
            'latest_price': '最新价',
            'volatility_50d': '30日箱宽(区间振幅)',
            'central_zone_width': '缠论中枢宽度',
            'central_zone_zg': '中枢ZG',
            'central_zone_zd': '中枢ZD',
            'central_zone_legs': '中枢段数',
            'dev_from_zg': '中枢偏离度(现价/ZG-1)',
            'quiet_pass': '静默通过(无转折)',
            'max_leg_up': '最大单段涨幅',
            'max_leg_down': '最大单段跌幅',
            'drop_repair': '深跌修复度',
            'amp_v2_pass': 'v2方向敏感通过',
            'amp_v2_reason': 'v2判定原因',
            'width_metric': '宽度口径',
            'width_value': '实际判定的宽度',
            'deviation_from_high': '偏离最近高价',
            'rebound_check_passed': '回弹检查',
            '50d_total_change': '30日涨跌幅',
            '10d_change': '10日涨跌幅',
            'max_consecutive_up': '最大连涨天数',
            'max_consecutive_down': '最大连跌天数',
            'prox_now': '当前价/年高(新口径)',
            'csrc_industry': '行业(证监会)',
            'turn_30d': '30日均换手率(%)',
            'roe_pct': 'ROE(%)'
        }

        df_out.rename(columns={k: v for k, v in col_names.items() if k in df_out.columns}, inplace=True)

        with pd.ExcelWriter(fname, engine='openpyxl') as w:
            df_out.to_excel(w, sheet_name='全部股票', index=False)

            passed = df[df['passed']].sort_values('total_score', ascending=False)
            if len(passed) > 0:
                passed_cols = [c for c in cols if c in passed.columns]
                passed_out = passed[passed_cols].copy()
                passed_out.rename(columns={k: v for k, v in col_names.items() if k in passed_out.columns}, inplace=True)
                passed_out.to_excel(w, sheet_name='通过筛选', index=False)

                for g in ['A', 'B', 'C']:
                    gdf = passed[passed['grade'] == g]
                    if len(gdf) > 0:
                        gdf_out = gdf[passed_cols].copy()
                        gdf_out.rename(columns={k: v for k, v in col_names.items() if k in gdf_out.columns},
                                       inplace=True)
                        gdf_out.to_excel(w, sheet_name=f'{g}级股票', index=False)

            removed_df = df[df['removed'] == True]
            if len(removed_df) > 0:
                removed_cols = [c for c in cols if c in removed_df.columns]
                removed_out = removed_df[removed_cols].copy()
                removed_out.rename(columns={k: v for k, v in col_names.items() if k in removed_out.columns},
                                   inplace=True)
                removed_out.to_excel(w, sheet_name='移除股票', index=False)

            triggered_df = df[df['buy_triggered'] == True]
            if len(triggered_df) > 0:
                triggered_cols = [c for c in cols if c in triggered_df.columns]
                triggered_out = triggered_df[triggered_cols].copy()
                triggered_out.rename(columns={k: v for k, v in col_names.items() if k in triggered_out.columns},
                                     inplace=True)
                triggered_out.to_excel(w, sheet_name='触发买入', index=False)

        print(f"\n保存到: {fname}")
        return fname


# ==========================================
# 主程序
# ==========================================
def main():
    screener = MarketScreener()
    results = screener.scan(max_stocks=None, check_existing=True)
    screener.save(results)
    return results


if __name__ == '__main__':
    main()
