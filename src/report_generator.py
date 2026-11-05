# -*- coding: utf-8 -*-
"""
分析日报生成模块 (AI-Driven Daily Insight Report Generator)
采用“程序化结构筛选 + 分段式大模型客观撰稿 + 确定性引用核对”人机协同架构：
1. 程序 (Python)：基于事件库 Schema 字段执行完全通用的规则筛选与上下文压缩（不硬编码任何特定事件 ID）：
   - Part 1 素材：按多维重要度得分与类别代表性筛选当日重点候选事件、同类别历史背景事件、以及当日被标记为 PR_PROMOTION / STALE_NEWS 的过滤清单
   - Part 2 素材：按 Schema 状态与时间线筛选跨批次持续发酵事件、当日新出现有效事件、含口径修正/标题夸大/传闻标记的核对事件、以及跨日趋势支撑事件
2. AI (LLM)：遵循平实、克制、零自媒体修辞的通用分析方法论，分段生成分析日报，
   并将支撑证据 [EVT-xxx | ITEM-xxx (字段名)] 自然嵌入在关键事实与结论句末。
3. 程序后置核对：自动校验必含可视化图表链接与引用绑定关系，输出至 outputs/daily_report_2026-11-04.md。
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

from src.llm_client import call_llm_markdown
from src.visualizer import (
    CATEGORY_SHORT_NAMES,
    OUTPUT_DIR,
    load_all_data,
    run_visualization,
    score_batch3_event,
)

BASE_DIR = Path(__file__).resolve().parent.parent


def _compact_event_for_prompt(
    ev: dict, score_info: dict = None, include_timeline: bool = False, brief: bool = False
) -> dict:
    """精简提取供大模型撰稿所需的核心结构化字段，剔除冗余重复长文本，确保单次 Prompt 轻量高效"""
    summary_limit = 150 if brief else 240
    metric_limit = 2 if brief else 4
    item_obj = {
        "event_id": ev["event_id"],
        "title": ev["canonical_title"],
        "category": CATEGORY_SHORT_NAMES.get(ev["category"], ev["category"]),
        "status": ev["status"],
        "batches": f"{ev['first_seen_batch']} -> {ev['last_updated_batch']}",
        "source_items": ev["source_items"],
        "languages": ev.get("languages", []),
        "summary": ev["synthesis_summary"][:summary_limit],
    }
    if score_info:
        item_obj["score"] = score_info["total_score"]
        item_obj["basis"] = (
            f"{score_info['source_count']}篇报道/{score_info['platform_count']}家平台, "
            f"双语={'是' if score_info['bilingual'] else '否'}, "
            f"跨批次={'是' if score_info['is_cross_batch'] else '否'}"
        )
        if score_info["warning_flags"]:
            item_obj["quality_flags"] = score_info["warning_flags"]

    # 精简提取关键量化指标
    if ev.get("metrics_ledger"):
        seen_m = []
        for m in ev["metrics_ledger"]:
            m_str = f"{m['metric_name']}={m['value']} ({m.get('scope_note', '')[:32]}) [来自 {m['item_id']}]"
            if m_str not in seen_m:
                seen_m.append(m_str)
            if len(seen_m) >= metric_limit:
                break
        item_obj["key_metrics"] = seen_m

    if ev.get("metric_resolution_note"):
        item_obj["metric_correction_note"] = ev["metric_resolution_note"][:180]

    if include_timeline and ev.get("timeline") and len(ev["timeline"]) > 1:
        item_obj["timeline"] = [
            f"{t['batch_date']} [{t['item_id']}] ({t['transition']}): {t['summary'][:90]}"
            for t in ev["timeline"]
        ]
    elif ev.get("quality_warnings"):
        item_obj["warnings"] = [
            f"[{w['item_id']}] {','.join(w.get('flags', []))}: {w.get('reason', '')[:65]}"
            for w in ev["quality_warnings"][:2]
        ]
    return item_obj


def _is_pure_pr_event(ev: dict) -> bool:
    """判断一个事件是否全部信源均为单源公关软文 (PR_PROMOTION)"""
    all_items = set(ev.get("source_items", []))
    if not all_items:
        return False
    pr_items = {
        w.get("item_id")
        for w in ev.get("quality_warnings", [])
        if "PR_PROMOTION" in w.get("flags", [])
    }
    return all_items.issubset(pr_items)


def build_split_contexts(raw_item_map: dict, batches_events: dict) -> Tuple[dict, dict, str, str]:
    """
    基于通用 Schema 规则构建上下半场结构化素材包（零硬编码事件 ID）：
    1. 自动识别首批日期与目标报告日期；
    2. 对目标批次全量活跃事件按多维重要度打分排序，完整传入全部有效活跃事件，并自动分离 PR_PROMOTION / STALE_NEWS 噪音；
    3. 按跨批次属性、status=='CORRECTED' / metric_resolution_note / 风险标签自动归集跨日发酵、口径更正与历史趋势背景事件。
    """
    sorted_dates = sorted(batches_events.keys())
    first_batch = sorted_dates[0]
    target_batch = sorted_dates[-1]
    all_events = batches_events[target_batch]

    # 1. 对目标批次所有活跃事件计算重要度得分并按分值降序排列
    active_events = [e for e in all_events if e["last_updated_batch"] == target_batch]
    scored_active = []
    for ev in active_events:
        sc, brk = score_batch3_event(ev, raw_item_map)
        scored_active.append((sc, brk, ev))
    scored_active.sort(key=lambda x: x[0], reverse=True)

    # 2. 分离当日全部有效活跃事件与被降权排除的软文/旧闻
    valid_scored = [(sc, brk, ev) for sc, brk, ev in scored_active if not brk["is_pr_or_stale"]]
    new_noise_events = [
        _compact_event_for_prompt(ev, brk)
        for _, brk, ev in scored_active
        if brk["is_pr_or_stale"]
    ]

    # 3. 完整保留当日全部有效活跃事件（按重要度得分从高到低排列），供大模型综合选品与分析
    all_valid_active_today = [
        _compact_event_for_prompt(ev, brk, include_timeline=True)
        for _, brk, ev in valid_scored
    ]

    # 4. 划分当日新出现有效事件 vs 跨批次持续发酵事件
    new_valid_events = []
    fermenting_events = []
    for sc, brk, ev in valid_scored:
        if ev["first_seen_batch"] == target_batch:
            new_valid_events.append(_compact_event_for_prompt(ev, brk))
        else:
            fermenting_events.append(_compact_event_for_prompt(ev, brk, include_timeline=True))

    # 5. 按 Schema 规则自动提取“被更正信息与数据口径/传闻核对”事件
    corrected_and_debunked = []
    for ev in all_events:
        if _is_pure_pr_event(ev):
            continue
        has_verify_flag = any(
            set(w.get("flags", [])) & {"EXAGGERATED_TITLE", "UNVERIFIED_RUMOR"}
            for w in ev.get("quality_warnings", [])
        )
        if ev.get("status") == "CORRECTED" or ev.get("metric_resolution_note") or has_verify_flag:
            corrected_and_debunked.append(_compact_event_for_prompt(ev, include_timeline=True))
    corrected_and_debunked.sort(key=lambda x: len(x.get("source_items", [])), reverse=True)

    # 6. 按类别提取前序批次的高证据密度背景与趋势支撑事件（每类最多取前 4 个，使用精简模式）
    historical_context = []
    for cat in CATEGORY_SHORT_NAMES:
        cat_hist = [
            e
            for e in all_events
            if e["category"] == cat
            and e["last_updated_batch"] != target_batch
            and not _is_pure_pr_event(e)
            and (
                len(e.get("source_items", [])) >= 2
                or len(e.get("metrics_ledger", [])) >= 2
                or any("MULTI_TOPIC_DIGEST" in w.get("flags", []) for w in e.get("quality_warnings", []))
            )
        ]
        cat_hist.sort(
            key=lambda e: (
                len(e.get("source_items", [])),
                bool(e.get("quality_warnings")),
                len(e.get("metrics_ledger", [])),
            ),
            reverse=True,
        )
        for ev in cat_hist[:4]:
            historical_context.append(_compact_event_for_prompt(ev, brief=True))

    part1_ctx = {
        "report_date": target_batch,
        "top_focus_candidates": all_valid_active_today,
        "excluded_noise_today": new_noise_events,
        "historical_category_context": historical_context,
    }

    part2_ctx = {
        "first_batch_date": first_batch,
        "report_date": target_batch,
        "newly_emerged_valid_events_today": new_valid_events,
        "newly_emerged_filtered_noise_today": new_noise_events,
        "ongoing_and_fermenting_cross_batch_events": fermenting_events,
        "corrected_and_debunked_events": corrected_and_debunked,
        "historical_trend_support_events": historical_context,
    }
    return part1_ctx, part2_ctx, first_batch, target_batch


def get_part1_system_prompt(report_date: str) -> str:
    return f"""你是一位严谨、克制的行业数据与情报分析师，正在根据结构化事件库撰写《AI 行业舆情分析日报（{report_date}）》的第一部分（今日主要事件与背景影响分析）。

【一、 行文风格红线——平实、克制、完全基于事实核对】：
1. **彻底禁用自媒体特稿修辞、戏剧化拔高与口号式金句**：
   - **严禁出现**“舆情场域”、“主线交织”、“正面碰撞”、“撕开裂口”、“危机”、“人力阵痛”、“破局”、“组合拳”、“补课”、“信息脱水”等自媒体词汇或互联网黑话。
   - **严禁使用反问/设问句**（如“实质上是在追问：谁应负责？”）和**主观文学评价**（如“形成了耐人寻味的对照”、“微妙的对照”）。
   - **严禁出现系统模板套话**（如“入选事件一：”、“这是今日信息密度最高的事件”、“入选理由有三”）。
   - 全文必须采用**平实、准确、克制的客观陈述句**，有一分证据说一分话。

【二、 章节结构与选品组织规范】：
1. **日报主标题与开篇摘要段**：
   - 顶部输出 `# AI 行业舆情分析日报（{report_date}）`
   - 紧接一段 2～3 句的平实客观摘要，直接陈述当日涵盖的几项主要动态事实（不做宏大叙事渲染）。
   - 摘要段下方插入当日概览图：
     `![图1：当日（{report_date}）重点事件排行与分类分布](figures/fig1_daily_overview_{report_date}.png)`
2. **主体章节 `## 一、 今日主要事件与背景影响分析`**：
   - 从输入数据的 `top_focus_candidates` 中精选 **4 个最具代表性的主要事件主题**作为三级子标题（`### 1. ...` 至 `### 4. ...`），标题须直接陈述具体主体与动作事实。
   - **通用选品与聚合准则**：
     - 优先选取多信源、跨语种交叉印证（`score` 排名靠前）且对搜索/教育入口、数据版权合规、终端生态落地、或企业组织与用户隐私有实质影响的事件；
     - 若同一家企业在当日有两项紧密相关的重点动态（例如同一操作系统版本下的本土化模型合作与全球语音助手接口开放），应合并在同一事件小节内综合分析；
     - 对于以跨批次进展为主的算力基建选址或常规版本迭代事件，可留至日报第二部分“持续发酵事件”中展开，避免第一部分与第二部分重复；而在谈及企业裁员等组织调整事件时，可结合同日新出现的用户数据隐私政策争议等社区焦点一并客观陈述。
   - **单事件内部写法（融合事实、关注依据、背景与影响）**：
     - 每个事件小节内用 2～3 个平实的自然段依次交代：① **核心事实、数据与时间节点**；② **作为今日重点事件的客观依据**（如获得多篇中英文独立报道确认、跨批次跟进、或触及关键业务合规边界——自然融入段落叙述中，不加标签）；③ **背景与影响分析**（调用 `top_focus_candidates` 或 `historical_category_context` 中同领域的关联事件进行客观对照，如将搜索绑定政策与同期披露的 AI 搜索请求量占比及统计口径限制对照、将第三方爬虫诉讼与行业 `robots.txt` 退出标准及内容平台流量受损反馈对照）。
3. **尾部过滤说明段**：
   - 在本部分末尾用一段平实的文字（以 `**低质与非当日数据过滤说明：**` 开头），遍历 `excluded_noise_today` 中的全部事件，逐一列出其事件编号与简明内容，客观说明其因属于缺乏独立信源核实的单源企业宣传通稿（`PR_PROMOTION`）或实际发生时间远早于发布日的旧闻调查（`STALE_NEWS`）而已按规则降权排除，不纳入今日主要事件。

【三、 句末证据引用规范（零幻觉）】：
- 将支撑证据自然附在关键事实、数据或结论句末，格式为 `[EVT-编号 | ITEM-编号 (字段名)]`，字段名取自 `content`、`summary`、`key_metrics`、`timeline`、`quality_flags`。
- **严禁捏造任何 ID！每个 `[EVT-xxx | ITEM-xxx ...]` 方括号内的 `ITEM-xxx` 必须严格属于该 `EVT-xxx` 的 `source_items`！同时引用不同事件时必须分成两个独立方括号。**"""


def get_part2_system_prompt(first_batch: str, report_date: str) -> str:
    return f"""你是一位严谨、克制的行业数据与情报分析师，正在根据结构化事件库撰写《AI 行业舆情分析日报（{report_date}）》的第二部分（跨批次变化与趋势判断）。

【一、 行文风格红线——平实、克制、拒绝绝对化与口号式包装】：
1. **严禁使用绝对化断言与格言式金句**：
   - **严禁出现**“不可逆地转移”、“从发展约束演变为生存约束”、“撕开裂口”、“重塑格局”等未经长期数据证实的夸张口号。
   - 必须使用平实、客观的数据与事实描述（例如直接陈述具体的排队年限、投运周期、支出承诺金额及企业采取的具体应对措施）。
2. **避免与第一部分重复赘述**：第一部分已作为主要事件详细展开过的事件，在本部分跨批次对比时仅用一句话简要交代其跨日时间线增量，将篇幅集中于第一部分未展开的持续发酵事件、数据更正记录与今日其他新动态。直接从 `## 二、` 开始输出。

【二、 章节结构与分析方法论】：
1. `## 二、 与前几批相比的变化：持续发酵、信息更正与今日新动态`
   - 标题下方插入跨批次变化图：
     `![图2：跨批次（{first_batch} 至 {report_date}）事件变化：新出现、持续发酵与被更正信息](figures/fig2_cross_batch_changes.png)`
   - 下设三个三级子章节：
     - `### 2.1 持续发酵与跨日推进的事件`：
       基于 `ongoing_and_fermenting_cross_batch_events`，重点展开出现跨日实质性新进展（`status == "ONGOING"`）的事件，依据其 `timeline` 详细对比首见批次与当日批次的具体变化（并结合 `historical_trend_support_events` 中同领域的关联事件补充背景）；随后用简明语言交代其余跨日跟进印证事件（`status == "CORROBORATED"`）的时间线增量。
     - `### 2.2 被更正的信息与数据口径核对`：
       基于 `corrected_and_debunked_events`，客观梳理跨批次报道中被修正的数据、前提条件说明与传闻澄清：
       ① **财务与基建数据的跨批次修订与会计口径说明**（严格依据 `timeline` 与 `metric_correction_note` 对比初报估算值与后续修订精确值，明确区分可转换票据公允价值变动等会计准则减计与实际营业亏损/现金消耗，以及不同统计口径下的环比增速差异）；
       ② **企业自报降本/提速数据的前提条件说明**（依据 `metric_correction_note` 明确列出硬件部署规模及未隔离的混杂变量）；
       ③ **夸大标题与未证实传闻澄清**（依据 `warnings` 对比媒体标题与一手文件原文边界，或说明正文自认存疑的传闻）。
     - `### 2.3 今日新出现的其他动态`：
       基于 `newly_emerged_valid_events_today`，平实梳理当日首次出现、且未在第一部分展开的其余有效事件（涵盖新发布的具身/垂直模型、硬件与固件问题、存储与基础设施供应链、商业化爆料及开源项目版本更新）。
2. `## 三、 证据范围内的趋势判断`
   - 综合三批次累计事件与量化指标，从 **算力基建与电力/财务约束**、**AI 搜索入口绑定与网页数据抓取争议**、**推理侧调用增长与模型架构/成本优化** 三个维度归纳 3 条平实、客观的行业观察。
   - **证据边界与样本不足内嵌规范（不单列子标题，不勉强下结论）**：
     在每一条趋势判断段落中，先陈述有多个独立事件支撑的趋势事实与量化数据，随后**在同一段落末尾自然说明当前样本不足以支撑哪些延伸结论**：
     - 针对财务与债务指标，须根据事件 `warnings` 指出统计口径争议（如长期机房租约与纯 AI 专项债务未作拆分），说明当前样本不足以评估具体违约风险；
     - 针对搜索份额或商业化收入爆料，须指出其系企业单方按特定口径估算或单一媒体爆料、缺乏第三方独立监测数据，说明当前样本不足以判断真实市场份额变化；
     - 针对自托管开源模型降本或企业自动化裁员个案，须明确指出当前样本不足以推演出“闭源商业 API 收入即将萎缩”或“AI 已引发软件行业普遍裁员潮”的结论（同时列出商业大模型营收高速增长的反证、自建硬件门槛与未隔离变量、以及单一并购重组期企业样本量过小的局限）。

【三、 句末证据引用规范（零幻觉）】：
- 每个关键事实、数据与结论句子末尾必须自然附上 `[EVT-编号 | ITEM-编号 (字段名)]`。
- **严禁捏造任何 ID！每个 `[EVT-xxx | ITEM-xxx ...]` 方括号内的 `ITEM-xxx` 必须严格属于该 `EVT-xxx` 的 `source_items`！**"""


def _verify_and_sanitize_citations(markdown_text: str, event_map: dict, raw_item_map: dict) -> str:
    """
    程序化后置防护网：检查大模型生成的日报中的每个 [EVT-xxx | ITEM-xxx ...] 引用块，
    确保所有 ITEM-xxx 严格归属于对应 EVT-xxx，若模型将多个事件的 ITEM 合写在一个方括号中，自动按真实事件表拆分对齐。
    """
    item_to_evts: Dict[str, List[str]] = {}
    for eid, ev in event_map.items():
        for iid in ev.get("source_items", []):
            item_to_evts.setdefault(iid, []).append(eid)

    def _fix_bracket(match: re.Match) -> str:
        inner = match.group(1)
        evts = re.findall(r"EVT-\d{3}", inner)
        items = re.findall(r"ITEM-\d{8}-\d{3}", inner)
        if not evts or not items:
            return match.group(0)

        fields = re.findall(r"\(([a-zA-Z_,\s]+)\)", inner)
        field_suffix = f" ({fields[0].strip()})" if fields else " (content)"

        if len(evts) == 1:
            eid = evts[0]
            valid_items = set(event_map.get(eid, {}).get("source_items", []))
            if all(i in valid_items for i in items):
                return match.group(0)

        # 按每个 item 真实归属的 event_id 分组重构方括号
        grouped: Dict[str, List[str]] = {}
        for iid in items:
            if iid not in raw_item_map:
                continue
            matched_eid = None
            for cand_e in evts:
                if iid in event_map.get(cand_e, {}).get("source_items", []):
                    matched_eid = cand_e
                    break
            if not matched_eid and iid in item_to_evts:
                matched_eid = item_to_evts[iid][0]
            if matched_eid:
                if iid not in grouped.setdefault(matched_eid, []):
                    grouped[matched_eid].append(iid)

        if not grouped:
            return match.group(0)

        repaired_blocks = []
        for g_eid, g_items in grouped.items():
            items_joined = ", ".join(f"{i}{field_suffix}" for i in g_items)
            repaired_blocks.append(f"[{g_eid} | {items_joined}]")
        return "、".join(repaired_blocks)

    return re.sub(r"\[([^\[\]]*EVT-\d{3}[^\[\]]*)\]", _fix_bracket, markdown_text)


def generate_daily_report() -> Dict[str, str]:
    """生成可视化图表并调用大模型分段生成最终分析日报（支持本地确定性缓存复现）"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fig_paths = run_visualization()

    raw_item_map, batches_events, _ = load_all_data()
    part1_ctx, part2_ctx, first_batch, target_batch = build_split_contexts(raw_item_map, batches_events)
    b3_events = batches_events[target_batch]
    event_map = {e["event_id"]: e for e in b3_events}

    p1_user = (
        f"请基于以下结构化事件数据，撰写日报（{target_batch}）的【第一部分：一、今日主要事件与背景影响分析】。\n"
        "直接输出 Markdown 正文：\n\n"
        + json.dumps(part1_ctx, ensure_ascii=False, separators=(",", ":"))
    )
    p2_user = (
        f"请基于以下结构化事件数据，撰写日报（{target_batch}）的【第二部分：二、与前几批相比的变化 与 三、证据范围内的趋势判断】。\n"
        "直接从 `## 二、` 开始输出 Markdown 正文：\n\n"
        + json.dumps(part2_ctx, ensure_ascii=False, separators=(",", ":"))
    )

    print("  - [AI 撰稿 1/2] 正在生成日报前半部分（今日主要事件 + 背景与影响分析）...")
    md_part1 = call_llm_markdown(get_part1_system_prompt(target_batch), p1_user, temperature=0.1)

    print("  - [AI 撰稿 2/2] 正在生成日报后半部分（跨批次三类变化 + 趋势判断与证据边界）...")
    md_part2 = call_llm_markdown(get_part2_system_prompt(first_batch, target_batch), p2_user, temperature=0.1)

    full_md = md_part1.strip() + "\n\n---\n\n" + md_part2.strip()
    cleaned_md = _verify_and_sanitize_citations(full_md, event_map, raw_item_map)

    report_path = OUTPUT_DIR / f"daily_report_{target_batch}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(cleaned_md.strip() + "\n")

    print(f"  - 已生成 AI 深度分析日报: {report_path.relative_to(BASE_DIR)}")
    return {
        "report_path": str(report_path.relative_to(BASE_DIR)).replace("\\", "/"),
        "fig1_path": fig_paths["fig1"],
        "fig2_path": fig_paths["fig2"],
    }


if __name__ == "__main__":
    generate_daily_report()
