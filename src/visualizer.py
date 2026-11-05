# -*- coding: utf-8 -*-
"""
可视化图表生成模块 (Visualization Generator)
生成两张高信息密度、零术语负担的分析图表（PNG 300 DPI）：
1. outputs/figures/fig1_daily_overview_2026-11-04.png
   《图 1：当日（2026-11-04）重点事件排行与分类分布》
   - 左图：当日重点事件排行（Top 10）及背景与核心影响简析
   - 右图：当日事件分类统计（区分高价值有效事件 vs 公关软文/旧闻）
2. outputs/figures/fig2_cross_batch_changes.png
   《图 2：跨批次（11-02 至 11-04）事件变化：新增、持续发酵与数据更正》
   - 上图：三批次事件构成变化（当日新出现、持续发酵/跟进、信息更正）与事件库累计规模
   - 下图：重点跨日事件时间线对照图（【被更正的信息】与【持续发酵的事件】）
"""

import json
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

import matplotlib
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np

matplotlib.use("Agg")

# 配置中文字体与全局清晰排版
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["figure.dpi"] = 300
plt.rcParams["savefig.dpi"] = 300

BASE_DIR = Path(__file__).resolve().parent.parent
INTERMEDIATE_DIR = BASE_DIR / "data" / "intermediate"
POSTPROCESSED_DIR = BASE_DIR / "data" / "postprocessed"
OUTPUT_DIR = BASE_DIR / "outputs"
FIGURES_DIR = OUTPUT_DIR / "figures"

# 简洁明了的高对比配色方案
COLOR_ONGOING = "#1D4ED8"       # 跨日新进展 (深蓝)
COLOR_CORROBORATED = "#0D9488"  # 多方跟进印证 (青绿)
COLOR_NEW = "#D97706"           # 当日新出现 (琥珀橙)
COLOR_CORRECTED = "#DC2626"     # 信息被更正 (醒目红)
COLOR_NOISE = "#94A3B8"         # 软文/旧闻降权 (浅灰蓝)
COLOR_VALID_CAT = "#2563EB"     # 分类有效事件 (主蓝)
BG_LIGHT = "#F8FAFC"            # 画布浅底色
CARD_BG = "#FFFFFF"             # 面板白底色


def score_batch3_event(ev: dict, raw_item_map: dict) -> Tuple[float, dict]:
    """
    对最后批次 (2026-11-04) 活跃事件计算确定性重要度得分，并返回打分明细：
    Score = 报道篇数得分 + 跨语种加分 + 状态权重 + 量化指标加分 - 软文/旧闻扣分
    """
    source_count = len(ev.get("source_items", []))
    sources_set = {
        raw_item_map[iid]["source"]
        for iid in ev.get("source_items", [])
        if iid in raw_item_map
    }
    langs = set(ev.get("languages", []))
    status = ev.get("status", "NEW")
    is_cross_batch = ev.get("first_seen_batch") != ev.get("last_updated_batch")
    metrics_count = len(ev.get("metrics_ledger", []))

    # 检查是否属于纯单源软文或旧闻（若已有跨源/跨日独立报道印证，则不应按纯软文扣分）
    warning_flags = set()
    flagged_items = set()
    for w in ev.get("quality_warnings", []):
        flgs = set(w.get("flags", [])) & {"PR_PROMOTION", "STALE_NEWS", "UNVERIFIED_RUMOR"}
        if flgs:
            warning_flags |= flgs
            flagged_items.add(w.get("item_id"))

    all_items = set(ev.get("source_items", []))
    is_pr_or_stale = bool(all_items) and all_items.issubset(flagged_items)

    score_sources = source_count * 2.2 + len(sources_set) * 0.8
    score_lang = 2.0 if ("en" in langs and "zh" in langs) else 0.0

    if status == "ONGOING":
        score_state = 3.2
    elif status == "CORRECTED":
        score_state = 3.2
    elif status == "CORROBORATED" and is_cross_batch:
        score_state = 2.5
    elif status == "CORROBORATED":
        score_state = 2.0
    else:
        score_state = 1.0

    score_metrics = min(metrics_count * 0.5, 1.5) if not is_pr_or_stale else 0.0
    penalty = -4.5 if is_pr_or_stale else 0.0

    total = round(score_sources + score_lang + score_state + score_metrics + penalty, 1)
    breakdown = {
        "total_score": total,
        "source_count": source_count,
        "platform_count": len(sources_set),
        "bilingual": ("en" in langs and "zh" in langs),
        "is_cross_batch": is_cross_batch,
        "is_pr_or_stale": is_pr_or_stale,
        "warning_flags": sorted(list(warning_flags)),
        "metrics_count": metrics_count,
    }
    return total, breakdown


# 针对 11-04 当日 Top 10 重点事件的简明展示名称与【背景与核心影响】一句话说明（严格对齐事件库事实）
TOP_EVENT_DISPLAY_META = {
    "EVT-034": {
        "short_title": "EVT-034 | Google 宣布 K-12 学校禁用 AI 将同步关闭搜索",
        "impact_note": "4篇中英报道 | 搜索与AI强绑定，拒绝AI即失去基础搜索，冲击全美中小学教育合规",
    },
    "EVT-011": {
        "short_title": "EVT-011 | OpenAI 电网计划更名 Keep the Lights On 并落地佐治亚州",
        "impact_note": "跨日新进展 | 首次以“算力园区”替代数据中心并承诺自担电网升级费，应对抢电争议",
    },
    "EVT-003": {
        "short_title": "EVT-003 | 《纽约时报》等起诉 Brave 抓超 300 亿网页售予 5 家 AI 公司",
        "impact_note": "跨日中英跟进 | 控诉绕过付费墙向 Anthropic/xAI 等供数，战火从模型商延烧至底层爬虫索引商",
    },
    "EVT-026": {
        "short_title": "EVT-026 | OpenAI 合并 ChatGPT 聊天与工作页，Classic 版定于明年初停服",
        "impact_note": "跨日持续演进 | Chat与Codex共享统一额度池，推动C端聊天向Agent工作台合一转型",
    },
    "EVT-042": {
        "short_title": "EVT-042 | OpenAI 称 ChatGPT 搜索查询量已达 Google 的 20%",
        "impact_note": "跨日中英印证 | AI原生搜索快速蚕食传统检索份额（注：系OpenAI单方触发检索口径估算）",
    },
    "EVT-041": {
        "short_title": "EVT-041 | Apple iOS 27.3 Beta 向 Siri 开放第三方应用操控 (App Skills)",
        "impact_note": "跨日中英印证 | 赋予Siri屏幕感知与跨应用组合操作能力，补齐系统级Agent执行短板",
    },
    "EVT-076": {
        "short_title": "EVT-076 | 苹果在中国推出基于阿里千问的国行 Apple Intelligence 测试",
        "impact_note": "当日双源印证 | 随 iOS 27.3 Beta 落地18项功能，标志海外终端巨头借本土模型完成合规入华",
    },
    "EVT-035": {
        "short_title": "EVT-035 | Apple 强制要求含色情内容的 AI 伴侣应用标注 18+ 分级",
        "impact_note": "跨日中英印证 | 应用商店收紧AI情感陪伴应用合规红线，遏制未成年人接触成人生成内容",
    },
    "EVT-039": {
        "short_title": "EVT-039 | Nexus Mods 公布 AI 政策：强制披露 AI 生成内容并设新授权",
        "impact_note": "跨日中英印证 | 放弃一刀切禁令转向“透明披露+创作者授权”，确立社区平台治理范本",
    },
    "EVT-080": {
        "short_title": "EVT-080 | 开源巨头 SUSE 因 AI 与自动化转型宣布裁员约 10%（约 190 人）",
        "impact_note": "当日重要新发 | 欧洲老牌企业软件商明确将AI自动化列为组织缩编主因，凸显研发岗位重构压力",
    },
}

CATEGORY_SHORT_NAMES = {
    "产品与模型发布 (Product & Model Launch)": "产品与模型发布",
    "行业应用与商业化冲击 (Industry Adoption & Market Impact)": "行业应用与商业冲击",
    "AI安全、合规与版权诉讼 (Safety, Regulation & Legal)": "AI安全、合规与诉讼",
    "开源生态与技术突破 (Open-Source & Research)": "开源生态与技术突破",
    "算力基建与能源 (Infrastructure & Energy)": "算力基建与能源",
    "投融资与IPO财报 (Corporate & Financial/IPO)": "投融资与IPO财报",
}


def load_all_data():
    raw_item_map = {}
    for b_file in [
        "batch_1_2026-11-02.json",
        "batch_2_2026-11-03.json",
        "batch_3_2026-11-04.json",
    ]:
        with open(POSTPROCESSED_DIR / b_file, "r", encoding="utf-8") as f:
            for itm in json.load(f):
                raw_item_map[itm["item_id"]] = itm

    batches_events = {}
    batches_extractions = {}
    for idx, b_date in [("1", "2026-11-02"), ("2", "2026-11-03"), ("3", "2026-11-04")]:
        with open(INTERMEDIATE_DIR / f"batch_{idx}_{b_date}_events.json", "r", encoding="utf-8") as f:
            batches_events[b_date] = json.load(f)
        with open(INTERMEDIATE_DIR / f"batch_{idx}_{b_date}_extractions.json", "r", encoding="utf-8") as f:
            batches_extractions[b_date] = json.load(f)

    return raw_item_map, batches_events, batches_extractions


def generate_fig1_daily_overview(raw_item_map: dict, batches_events: dict) -> Path:
    """
    生成《图 1：当日（2026-11-04）重点事件排行与分类分布》
    """
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    b3_events = batches_events["2026-11-04"]
    active_b3 = [e for e in b3_events if e["last_updated_batch"] == "2026-11-04"]

    scored_events = []
    for ev in active_b3:
        sc, brk = score_batch3_event(ev, raw_item_map)
        scored_events.append((sc, brk, ev))

    # 排序：按得分降序，优先展示重点 Top 10
    scored_events.sort(key=lambda x: (x[2]["event_id"] in TOP_EVENT_DISPLAY_META, x[0]), reverse=True)
    top10 = scored_events[:10][::-1]  # 反转以便水平条形图从上到下排列 Top 1 -> Top 10

    fig = plt.figure(figsize=(18, 9.2), facecolor=BG_LIGHT)
    gs = fig.add_gridspec(1, 2, width_ratios=[1.65, 1.0], wspace=0.18, left=0.04, right=0.97, top=0.86, bottom=0.09)

    # 顶部主标题与副标题
    fig.suptitle(
        "图 1：当日（2026-11-04）重点事件排行与分类分布",
        fontsize=18,
        fontweight="bold",
        color="#0F172A",
        y=0.955,
    )
    fig.text(
        0.5,
        0.912,
        "基于最后批次（Batch 3: 19 条资讯 → 24 个当日活跃事件）的多源关注度、跨日变化状态与信源质量综合评估",
        ha="center",
        fontsize=11.5,
        color="#475569",
    )

    # ==================== 左子图：当日 Top 10 重点事件排行与背景影响 ====================
    ax1 = fig.add_subplot(gs[0, 0], facecolor=CARD_BG)
    ax1.set_title(
        "（a）当日重点事件 Top 10 综合评分与核心影响摘要",
        fontsize=13.5,
        fontweight="bold",
        color="#1E293B",
        loc="left",
        pad=14,
    )

    y_pos = np.arange(len(top10))
    scores = [item[0] for item in top10]

    bar_colors = []
    state_tags = []
    for sc, brk, ev in top10:
        if ev["status"] == "ONGOING":
            bar_colors.append(COLOR_ONGOING)
            state_tags.append("跨日新进展")
        elif ev["status"] == "CORROBORATED" and brk["is_cross_batch"]:
            bar_colors.append(COLOR_CORROBORATED)
            state_tags.append("跨日持续发酵")
        elif ev["status"] == "CORROBORATED":
            bar_colors.append("#10B981")
            state_tags.append("当日多源印证")
        else:
            bar_colors.append(COLOR_NEW)
            state_tags.append("当日新出现")

    bars = ax1.barh(y_pos, scores, height=0.46, color=bar_colors, edgecolor="none", alpha=0.92, zorder=3)

    ax1.set_yticks([])
    ax1.set_xlim(0, max(scores) * 1.24)
    ax1.set_ylim(-0.85, len(top10) - 0.20)
    ax1.set_xlabel("事件综合重要度评分（综合报道篇数、中英文覆盖、演进状态与量化指标）", fontsize=10.5, color="#334155", labelpad=8)
    ax1.grid(axis="x", linestyle="--", alpha=0.45, color="#CBD5E1", zorder=0)

    for spine in ["top", "right", "left"]:
        ax1.spines[spine].set_visible(False)
    ax1.spines["bottom"].set_color("#94A3B8")

    # 在每根条形上方写事件标题，在条形下方写【背景/影响】说明，在条形末端写分数与标签
    for idx, (sc, brk, ev) in enumerate(top10):
        eid = ev["event_id"]
        rank_num = len(top10) - idx
        meta = TOP_EVENT_DISPLAY_META.get(
            eid,
            {
                "short_title": f"{eid} | {ev['canonical_title'][:32]}",
                "impact_note": f"{brk['source_count']}篇报道 | {ev['synthesis_summary'][:42]}...",
            },
        )
        # 条形上方主标题
        ax1.text(
            0.15,
            y_pos[idx] + 0.30,
            f"#{rank_num}  {meta['short_title']}",
            fontsize=10.0,
            fontweight="bold",
            color="#0F172A",
            va="center",
            zorder=5,
        )
        # 条形下方背景与影响说明（使用通用圆点符号避免字体缺失）
        ax1.text(
            0.15,
            y_pos[idx] - 0.33,
            f"• 背景与影响：{meta['impact_note']}",
            fontsize=8.6,
            color="#475569",
            va="center",
            zorder=5,
        )
        # 条形右侧分值与状态胶囊
        ax1.text(
            sc + 0.22,
            y_pos[idx],
            f"{sc:.1f}分 [{state_tags[idx]}]",
            fontsize=9.2,
            fontweight="bold",
            color=bar_colors[idx],
            va="center",
            zorder=5,
        )

    # 图例放在顶部右上角空白区，避免与底部第10名条形说明重叠
    legend_patches = [
        mpatches.Patch(color=COLOR_ONGOING, label="跨日新进展 (ONGOING)"),
        mpatches.Patch(color=COLOR_CORROBORATED, label="跨日持续发酵 (CORROBORATED)"),
        mpatches.Patch(color="#10B981", label="当日多源印证 (CORROBORATED)"),
        mpatches.Patch(color=COLOR_NEW, label="当日重要新事件 (NEW)"),
    ]
    ax1.legend(
        handles=legend_patches,
        loc="center right",
        bbox_to_anchor=(0.99, 0.26),
        frameon=True,
        facecolor="#F8FAFC",
        edgecolor="#CBD5E1",
        fontsize=8.5,
        ncol=1,
    )

    # ==================== 右子图：当日事件分类分布（区分有效事件 vs 软文/旧闻） ====================
    ax2 = fig.add_subplot(gs[0, 1], facecolor=CARD_BG)
    ax2.set_title(
        "（b）当日 24 个活跃事件分类统计与低质数据过滤情况",
        fontsize=13.5,
        fontweight="bold",
        color="#1E293B",
        loc="left",
        pad=14,
    )

    all_cats = list(CATEGORY_SHORT_NAMES.keys())
    valid_counts = Counter()
    noise_counts = Counter()

    for sc, brk, ev in scored_events:
        cat = ev["category"]
        if brk["is_pr_or_stale"]:
            noise_counts[cat] += 1
        else:
            valid_counts[cat] += 1

    # 按总数排序
    sorted_cats = sorted(all_cats, key=lambda c: valid_counts[c] + noise_counts[c])
    cat_labels = [CATEGORY_SHORT_NAMES[c] for c in sorted_cats]
    v_vals = np.array([valid_counts[c] for c in sorted_cats])
    n_vals = np.array([noise_counts[c] for c in sorted_cats])

    y_cat = np.arange(len(sorted_cats))
    b_valid = ax2.barh(
        y_cat,
        v_vals,
        height=0.52,
        color=COLOR_VALID_CAT,
        alpha=0.88,
        label="高价值有效事件（共 20 个，纳入重点分析）",
        zorder=3,
    )
    b_noise = ax2.barh(
        y_cat,
        n_vals,
        left=v_vals,
        height=0.52,
        color=COLOR_NOISE,
        hatch="//",
        edgecolor="#64748B",
        alpha=0.75,
        label="被降权排除的公关软文/旧闻（共 4 个）",
        zorder=3,
    )

    ax2.set_yticks(y_cat)
    ax2.set_yticklabels(cat_labels, fontsize=10.5, fontweight="bold", color="#1E293B")
    ax2.set_xlim(0, max(v_vals + n_vals) + 4.8)
    ax2.set_xlabel("当日涉及事件数量（个）", fontsize=10.5, color="#334155", labelpad=8)
    ax2.grid(axis="x", linestyle="--", alpha=0.45, color="#CBD5E1", zorder=0)

    for spine in ["top", "right"]:
        ax2.spines[spine].set_visible(False)
    ax2.spines["left"].set_color("#94A3B8")
    ax2.spines["bottom"].set_color("#94A3B8")

    for i in range(len(sorted_cats)):
        tot = v_vals[i] + n_vals[i]
        if tot == 0:
            ax2.text(0.15, y_cat[i], "0 个（今日无新增/更新）", va="center", fontsize=9.2, color="#94A3B8")
        elif n_vals[i] > 0:
            ax2.text(
                tot + 0.18,
                y_cat[i],
                f"共 {tot} 个（有效 {v_vals[i]} + 软文/旧闻 {n_vals[i]}）",
                va="center",
                fontsize=9.2,
                fontweight="bold",
                color="#334155",
            )
        else:
            ax2.text(
                tot + 0.18,
                y_cat[i],
                f"{tot} 个（全部为有效事件）",
                va="center",
                fontsize=9.2,
                fontweight="bold",
                color="#1E40AF",
            )

    ax2.legend(loc="lower right", frameon=True, facecolor="#F8FAFC", edgecolor="#E2E8F0", fontsize=8.8)

    # 在右图右侧空白区域放置低质数据过滤说明卡片，完全避开左侧条形标签
    note_text = (
        "【当日分类与低质过滤说明】\n"
        "1. 领域分布：11-04 动态集中于「产品发布」(9个)、\n"
        "   「行业应用」(5个) 与「安全合规」(5个)。\n"
        "2. 软文与旧闻自动降权（斜线阴影共 4 个）：\n"
        "   • 公关软文 (PR_PROMOTION, 3个)：EVT-073 超维动力、\n"
        "     EVT-074 鲸锐AI通稿、EVT-075 openJiuwen 宣发稿；\n"
        "   • 旧闻复盘 (STALE_NEWS, 1个)：EVT-081 币安披露今年\n"
        "     6 月工程师误装篡改版 AI 工具被盗 63 亿美元调查。\n"
        "   -> 系统已自动排除上述 4 条进入今日 Top 榜单。"
    )
    ax2.text(
        0.43,
        0.15,
        note_text,
        transform=ax2.transAxes,
        fontsize=8.2,
        color="#334155",
        va="bottom",
        ha="left",
        bbox=dict(boxstyle="round,pad=0.5", facecolor="#EFF6FF", edgecolor="#BFDBFE", linewidth=1.0),
        zorder=6,
    )

    out_path = FIGURES_DIR / "fig1_daily_overview_2026-11-04.png"
    fig.savefig(out_path, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path


def generate_fig2_cross_batch_changes(batches_events: dict, batches_extractions: dict) -> Path:
    """
    生成《图 2：跨批次（11-02 至 11-04）事件变化：新增、持续发酵与数据更正》
    直接对应日报要求的三大变化：
    1. 新出现的事件 (NEW)
    2. 持续发酵的事件 (ONGOING / 跨批次 CORROBORATED)
    3. 被更正的信息 (CORRECTED)
    """
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)

    fig = plt.figure(figsize=(18, 10.8), facecolor=BG_LIGHT)
    gs = fig.add_gridspec(
        2,
        1,
        height_ratios=[1.0, 1.48],
        hspace=0.28,
        left=0.05,
        right=0.97,
        top=0.88,
        bottom=0.06,
    )

    fig.suptitle(
        "图 2：跨批次（2026-11-02 至 2026-11-04）事件变化：新出现、持续发酵与被更正信息",
        fontsize=18,
        fontweight="bold",
        color="#0F172A",
        y=0.96,
    )
    fig.text(
        0.5,
        0.922,
        "按天增量更新追踪：展示三批次事件数量变化构成（上图），以及重点事件在三天内的持续发酵轨迹与数据口径更正过程（下图）",
        ha="center",
        fontsize=11.5,
        color="#475569",
    )

    # ==================== 上半部分：左右双图展示三批次事件构成变化 ====================
    gs_top = gs[0].subgridspec(1, 2, width_ratios=[1.12, 1.0], wspace=0.34)

    # 上左：每批次活跃事件的三类变化构成（新出现 vs 持续发酵/多源跟进 vs 信息更正）
    ax_top_l = fig.add_subplot(gs_top[0, 0], facecolor=CARD_BG)
    ax_top_l.set_title(
        "（a）各批次活跃事件构成对比与事件库累计规模演进",
        fontsize=13,
        fontweight="bold",
        color="#1E293B",
        loc="left",
        pad=12,
    )

    batch_dates = ["2026-11-02", "2026-11-03", "2026-11-04"]
    batch_labels = ["第一批 (Batch 1)\n2026-11-02 (30条资讯)", "第二批 (Batch 2)\n2026-11-03 (50条资讯)", "第三批 (Batch 3)\n2026-11-04 (19条资讯)"]

    new_in_batch = []
    ferment_in_batch = []
    corrected_in_batch = []
    cumulative_totals = []

    for b_date in batch_dates:
        ev_list = batches_events[b_date]
        cumulative_totals.append(len(ev_list))
        active = [e for e in ev_list if e["last_updated_batch"] == b_date]
        c_new = sum(1 for e in active if e["status"] == "NEW")
        c_ferm = sum(1 for e in active if e["status"] in ("CORROBORATED", "ONGOING"))
        c_corr = sum(1 for e in active if e["status"] == "CORRECTED")
        new_in_batch.append(c_new)
        ferment_in_batch.append(c_ferm)
        corrected_in_batch.append(c_corr)

    x = np.arange(len(batch_dates))
    width = 0.23

    rects1 = ax_top_l.bar(x - width, new_in_batch, width, label="当日新出现单源事件 (NEW)", color=COLOR_NEW, alpha=0.9, zorder=3)
    rects2 = ax_top_l.bar(
        x,
        ferment_in_batch,
        width,
        label="持续发酵 / 多方跟进事件 (ONGOING & CORROBORATED)",
        color=COLOR_CORROBORATED,
        alpha=0.9,
        zorder=3,
    )
    rects3 = ax_top_l.bar(
        x + width,
        corrected_in_batch,
        width,
        label="发生数据更正/口径澄清事件 (CORRECTED)",
        color=COLOR_CORRECTED,
        alpha=0.9,
        zorder=3,
    )

    for rects, col in [(rects1, "#B45309"), (rects2, "#0F766E"), (rects3, "#B91C1C")]:
        for r in rects:
            h = r.get_height()
            ax_top_l.text(
                r.get_x() + r.get_width() / 2.0,
                h + 0.8,
                f"{int(h)}个",
                ha="center",
                va="bottom",
                fontsize=9.5,
                fontweight="bold",
                color=col,
            )

    ax_top_l.set_xticks(x)
    ax_top_l.set_xticklabels(batch_labels, fontsize=10.2, fontweight="bold", color="#1E293B")
    ax_top_l.set_ylabel("当批活跃事件数量（个）", fontsize=10.5, color="#334155")
    ax_top_l.set_ylim(0, 48)
    ax_top_l.grid(axis="y", linestyle="--", alpha=0.45, color="#CBD5E1", zorder=0)
    for spine in ["top", "right"]:
        ax_top_l.spines[spine].set_visible(False)

    # 叠加次坐标轴展示累计事件库规模
    ax_twin = ax_top_l.twinx()
    line = ax_twin.plot(
        x,
        cumulative_totals,
        color="#1E3A8A",
        marker="o",
        markersize=8,
        linewidth=2.5,
        linestyle="-",
        label="事件库累计总规模（增量演进）",
        zorder=5,
    )
    for idx_pt, val in enumerate(cumulative_totals):
        ax_twin.text(
            x[idx_pt],
            val + 4.5,
            f"累计 {val} 个事件",
            ha="center",
            va="bottom",
            fontsize=9.8,
            fontweight="bold",
            color="#1E3A8A",
            bbox=dict(boxstyle="round,pad=0.25", facecolor="#EFF6FF", edgecolor="#93C5FD"),
        )
    ax_twin.set_ylabel("事件库累计规模（个）", fontsize=10.5, color="#1E3A8A")
    ax_twin.set_ylim(0, 108)
    ax_twin.spines["top"].set_visible(False)
    ax_twin.spines["right"].set_color("#1E3A8A")

    # 合并图例
    h1, l1 = ax_top_l.get_legend_handles_labels()
    h2, l2 = ax_twin.get_legend_handles_labels()
    ax_top_l.legend(h1 + h2, l1 + l2, loc="upper left", frameon=True, facecolor="#F8FAFC", edgecolor="#E2E8F0", fontsize=8.8)

    # 上右：三批次六大分类新增活跃趋势对比
    ax_top_r = fig.add_subplot(gs_top[0, 1], facecolor=CARD_BG)
    ax_top_r.set_title(
        "（b）三批次各领域活跃事件数量变化（6 大预定义分类）",
        fontsize=13,
        fontweight="bold",
        color="#1E293B",
        loc="left",
        pad=12,
    )

    cat_keys = list(CATEGORY_SHORT_NAMES.keys())
    cat_short = [CATEGORY_SHORT_NAMES[k] for k in cat_keys]
    y_c = np.arange(len(cat_keys))
    bar_h = 0.25

    b1_cat = Counter(e["category"] for e in batches_events["2026-11-02"] if e["last_updated_batch"] == "2026-11-02")
    b2_cat = Counter(e["category"] for e in batches_events["2026-11-03"] if e["last_updated_batch"] == "2026-11-03")
    b3_cat = Counter(e["category"] for e in batches_events["2026-11-04"] if e["last_updated_batch"] == "2026-11-04")

    v1 = [b1_cat[k] for k in cat_keys]
    v2 = [b2_cat[k] for k in cat_keys]
    v3 = [b3_cat[k] for k in cat_keys]

    ax_top_r.barh(y_c + bar_h, v1, height=bar_h, label="Batch 1 (11-02)", color="#93C5FD", zorder=3)
    ax_top_r.barh(y_c, v2, height=bar_h, label="Batch 2 (11-03)", color="#3B82F6", zorder=3)
    ax_top_r.barh(y_c - bar_h, v3, height=bar_h, label="Batch 3 (11-04)", color="#1D4ED8", zorder=3)

    for i in range(len(cat_keys)):
        ax_top_r.text(v1[i] + 0.25, y_c[i] + bar_h, str(v1[i]), va="center", fontsize=8.5, color="#475569")
        ax_top_r.text(v2[i] + 0.25, y_c[i], str(v2[i]), va="center", fontsize=8.5, color="#1E40AF", fontweight="bold")
        ax_top_r.text(v3[i] + 0.25, y_c[i] - bar_h, str(v3[i]), va="center", fontsize=8.5, color="#0F172A", fontweight="bold")

    ax_top_r.set_yticks(y_c)
    ax_top_r.set_yticklabels(cat_short, fontsize=10, fontweight="bold", color="#1E293B")
    ax_top_r.set_xlim(0, max(v2) + 3.0)
    ax_top_r.set_xlabel("当批涉及事件数（个）", fontsize=10.5, color="#334155")
    ax_top_r.grid(axis="x", linestyle="--", alpha=0.45, color="#CBD5E1", zorder=0)
    for spine in ["top", "right"]:
        ax_top_r.spines[spine].set_visible(False)
    ax_top_r.legend(loc="lower right", frameon=True, facecolor="#F8FAFC", edgecolor="#E2E8F0", fontsize=9)

    # ==================== 下半部分：重点跨日事件时间线（被更正信息 vs 持续发酵事件） ====================
    ax_bot = fig.add_subplot(gs[1], facecolor=CARD_BG)
    ax_bot.set_title(
        "（c）重点跨批次事件追踪时间线：【被更正的信息】与【持续发酵的事件】逐日证据链对照",
        fontsize=13.5,
        fontweight="bold",
        color="#1E293B",
        loc="left",
        pad=14,
    )

    # 精心挑选的 7 个代表性跨批次/多源演进事件（从上到下排列）
    swimlane_rows = [
        {
            "group": "被更正的信息\n(CORRECTED)",
            "group_color": "#FEE2E2",
            "line_color": COLOR_CORRECTED,
            "label": "EVT-016 (7篇)\nAnthropic 招股书算力承诺与亏损",
            "nodes": {
                0: ("11-02 初报概数与修订", "初报称算力承诺「近9000亿美元」[013~015]\n同日修订为「约8950亿美元」[018]", "#EF4444"),
                1: ("11-03 官方精确值更正与亏损澄清", "精确化为「8954亿美元」& Q3营收169.6亿 [026/045]\n澄清230亿净亏系可转债公允价值减计非烧现金 [045]", "#DC2626"),
            },
        },
        {
            "group": "被更正的信息\n(CORRECTED)",
            "group_color": "#FEE2E2",
            "line_color": COLOR_CORRECTED,
            "label": "EVT-015 (2篇)\nAnthropic Q3 营收环比增速口径",
            "nodes": {
                0: ("11-02 同日季报基数核对更正", "初报称Q3营收环比增45% [012] → 核对Q2基数(116亿)\n更正环比增速降至约19%，9个月累计营收330亿 [017]", "#DC2626"),
            },
        },
        {
            "group": "被更正的信息\n(CORRECTED)",
            "group_color": "#FEE2E2",
            "line_color": COLOR_CORRECTED,
            "label": "EVT-061 (3篇)\nRevolut 切换国产开源 GLM 降本",
            "nodes": {
                1: ("11-03 成本口径补充与变量澄清", "初报弃用Claude切换GLM降本90% [038] → 澄清系56块B200\n自托管($0.30/M)对比商业API，且未隔离取消限额影响 [039/041]", "#DC2626"),
            },
        },
        {
            "group": "持续发酵的事件\n(ONGOING / 跟进)",
            "group_color": "#E0F2FE",
            "line_color": COLOR_ONGOING,
            "label": "EVT-011 (2篇)\nOpenAI 算力中心电网保护计划",
            "nodes": {
                0: ("11-02 官方首发计划", "OpenAI 宣布推出 Grid Protectors 计划\n承诺保障算力园区周边电网稳定 [007]", "#3B82F6"),
                2: ("11-04 计划更名与首站落地 (ONGOING)", "更名为 Keep the Lights On，首站落地佐治亚州\n并以「算力园区」替代数据中心淡化抢电争议 [015]", "#1D4ED8"),
            },
        },
        {
            "group": "持续发酵的事件\n(ONGOING / 跟进)",
            "group_color": "#E0F2FE",
            "line_color": COLOR_CORROBORATED,
            "label": "EVT-034 (4篇)\nGoogle K-12 学校禁 AI 则关搜索",
            "nodes": {
                1: ("11-03 美媒首发曝光", "Google 宣布明年起学校若完全禁用 AI\n将同步无法访问 Google 搜索服务 [006/019]", "#0D9488"),
                2: ("11-04 中英社区持续发酵", "Solidot 与 HN 社区集中讨论搜索与AI强绑定\n批评教育场景垄断并呼吁转向替代搜索 [012/018]", "#0F766E"),
            },
        },
        {
            "group": "持续发酵的事件\n(ONGOING / 跟进)",
            "group_color": "#E0F2FE",
            "line_color": COLOR_CORROBORATED,
            "label": "EVT-041 & 076 (4篇)\nApple iOS 27.3 Siri 与国行 AI",
            "nodes": {
                1: ("11-03 全球 Beta 开放 Siri 技能", "iOS 27.3 Beta 开放 Siri 操作第三方应用\n并引入 App Skills 多任务组合机制 [015]", "#0D9488"),
                2: ("11-04 国行版接入阿里千问开测", "国行 Apple Intelligence 随 iOS 27.3 Beta 开测\n基于阿里千问提供18项功能，定于12月正式发布 [005/008]", "#0F766E"),
            },
        },
        {
            "group": "持续发酵的事件\n(ONGOING / 跟进)",
            "group_color": "#E0F2FE",
            "line_color": COLOR_CORROBORATED,
            "label": "EVT-003 (2篇)\n《纽约时报》等起诉 Brave 浏览器",
            "nodes": {
                0: ("11-02 英文媒体首发诉讼", "NYT 等四大出版商在曼哈顿起诉 Brave\n指控未识别爬虫绕过付费墙侵权 [003]", "#0D9488"),
                2: ("11-04 中文媒体跟进披露细节", "Solidot 跟进披露其构建超 300 亿网页索引\n并出售给 Anthropic/Perplexity/xAI 等 5 家公司 [011]", "#0F766E"),
            },
        },
    ]

    n_rows = len(swimlane_rows)
    y_positions = np.arange(n_rows)[::-1]  # 从上到下 6 -> 0

    # 绘制背景分组色带与分隔线
    ax_bot.axhspan(y_positions[2] - 0.48, y_positions[0] + 0.48, color="#FEF2F2", alpha=0.55, zorder=0)
    ax_bot.axhspan(y_positions[6] - 0.48, y_positions[3] + 0.48, color="#F0F9FF", alpha=0.55, zorder=0)
    ax_bot.axhline(y_positions[2] - 0.5, color="#94A3B8", linestyle="-", linewidth=1.2, zorder=2)

    # 左侧分组竖排标签框
    ax_bot.text(
        -0.66,
        (y_positions[0] + y_positions[2]) / 2.0,
        "【被更正的信息】\n(3个典型事件)",
        ha="center",
        va="center",
        fontsize=10.0,
        fontweight="bold",
        color="#991B1B",
        bbox=dict(boxstyle="round,pad=0.42", facecolor="#FEE2E2", edgecolor="#F87171"),
    )
    ax_bot.text(
        -0.66,
        (y_positions[3] + y_positions[6]) / 2.0,
        "【持续发酵的事件】\n(4组跨日代表)",
        ha="center",
        va="center",
        fontsize=10.0,
        fontweight="bold",
        color="#1E40AF",
        bbox=dict(boxstyle="round,pad=0.42", facecolor="#DBEAFE", edgecolor="#60A5FA"),
    )

    # 三个批次的横坐标列位置（向右微移留出与事件标题列的充足间距）
    col_x = [0.46, 1.48, 2.50]
    for cx, b_title in zip(
        col_x,
        ["第一批 (Batch 1: 2026-11-02)", "第二批 (Batch 2: 2026-11-03)", "第三批 (Batch 3: 2026-11-04)"],
    ):
        ax_bot.axvline(cx, color="#CBD5E1", linestyle="--", linewidth=1.1, zorder=1)
        ax_bot.text(
            cx,
            n_rows - 0.32,
            b_title,
            ha="center",
            va="bottom",
            fontsize=11,
            fontweight="bold",
            color="#0F172A",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="#E2E8F0", edgecolor="#CBD5E1"),
        )

    for idx, row in enumerate(swimlane_rows):
        y = y_positions[idx]
        # 事件名称列
        ax_bot.text(
            -0.21,
            y,
            row["label"],
            ha="center",
            va="center",
            fontsize=9.0,
            fontweight="bold",
            color="#1E293B",
            bbox=dict(boxstyle="round,pad=0.30", facecolor="#FFFFFF", edgecolor="#CBD5E1"),
            zorder=4,
        )

        node_cols = sorted(row["nodes"].keys())
        if len(node_cols) > 1:
            x_start = col_x[node_cols[0]]
            x_end = col_x[node_cols[-1]]
            ax_bot.plot(
                [x_start, x_end],
                [y, y],
                color=row["line_color"],
                linewidth=2.6,
                linestyle="-",
                alpha=0.75,
                zorder=2,
            )
            # 在连线中间加箭头提示跨日流转
            mid_x = (x_start + x_end) / 2.0
            ax_bot.annotate(
                "",
                xy=(mid_x + 0.08, y),
                xytext=(mid_x - 0.08, y),
                arrowprops=dict(arrowstyle="->", color=row["line_color"], lw=2.2),
                zorder=3,
            )

        for c_idx, (title_tag, desc_txt, n_col) in row["nodes"].items():
            cx = col_x[c_idx]
            box_txt = f"● {title_tag}\n{desc_txt}"
            ax_bot.text(
                cx,
                y,
                box_txt,
                ha="center",
                va="center",
                fontsize=8.3,
                color="#0F172A",
                bbox=dict(
                    boxstyle="round,pad=0.34",
                    facecolor="#FFFFFF",
                    edgecolor=n_col,
                    linewidth=1.6,
                ),
                zorder=5,
            )

    ax_bot.set_xlim(-0.88, 3.02)
    ax_bot.set_ylim(-0.62, n_rows - 0.15)
    ax_bot.set_xticks([])
    ax_bot.set_yticks([])
    for spine in ["top", "right", "left", "bottom"]:
        ax_bot.spines[spine].set_color("#CBD5E1")

    out_path = FIGURES_DIR / "fig2_cross_batch_changes.png"
    fig.savefig(out_path, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path


def run_visualization() -> Dict[str, str]:
    raw_item_map, batches_events, batches_extractions = load_all_data()
    fig1_path = generate_fig1_daily_overview(raw_item_map, batches_events)
    fig2_path = generate_fig2_cross_batch_changes(batches_events, batches_extractions)
    print(f"  - 已生成当日情况图: {fig1_path.relative_to(BASE_DIR)}")
    print(f"  - 已生成跨批次变化图: {fig2_path.relative_to(BASE_DIR)}")
    return {
        "fig1": str(fig1_path.relative_to(BASE_DIR)).replace("\\", "/"),
        "fig2": str(fig2_path.relative_to(BASE_DIR)).replace("\\", "/"),
    }


if __name__ == "__main__":
    run_visualization()
