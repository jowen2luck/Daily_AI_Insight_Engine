# -*- coding: utf-8 -*-
"""
分析日报生成模块 (AI-Driven Daily Insight Report Generator)
采用“程序化结构筛选 + 分段式大模型深度撰稿 + 确定性引用核对”人机协同架构：
1. 程序 (Python)：对最后一个批次 (2026-11-04) 的事件库执行多维重要度评分，筛选并精简压缩上下文（控制在 ~3.5k 字符/请求，防止长上下文网关超时）：
   - Part 1 素材：今日主要事件 (Top 5)、入选依据、背景与影响及当日被排除的软文/旧闻清单
   - Part 2 素材：与前几批相比的变化（新出现、持续发酵、被更正信息）及跨日趋势支撑事件
2. AI (LLM)：以资深科技产业主编视角分段撰写行文流畅、有叙事纵深的分析日报，
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


def _compact_event_for_prompt(ev: dict, score_info: dict = None, include_timeline: bool = False) -> dict:
    """精简提取供大模型撰稿所需的核心结构化字段，剔除冗余重复长文本，确保单次 Prompt 轻量高效"""
    item_obj = {
        "event_id": ev["event_id"],
        "title": ev["canonical_title"],
        "category": CATEGORY_SHORT_NAMES.get(ev["category"], ev["category"]),
        "status": ev["status"],
        "batches": f"{ev['first_seen_batch']} -> {ev['last_updated_batch']}",
        "source_items": ev["source_items"],
        "languages": ev.get("languages", []),
        "summary": ev["synthesis_summary"][:260],
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

    # 精简提取关键量化指标（最多保留前 4 条核心指标）
    if ev.get("metrics_ledger"):
        seen_m = []
        for m in ev["metrics_ledger"]:
            m_str = f"{m['metric_name']}={m['value']} ({m.get('scope_note', '')[:35]}) [来自 {m['item_id']}]"
            if m_str not in seen_m:
                seen_m.append(m_str)
            if len(seen_m) >= 4:
                break
        item_obj["key_metrics"] = seen_m

    if ev.get("metric_resolution_note"):
        item_obj["metric_correction_note"] = ev["metric_resolution_note"][:180]

    if include_timeline and ev.get("timeline") and len(ev["timeline"]) > 1:
        item_obj["timeline"] = [
            f"{t['batch_date']} [{t['item_id']}] ({t['transition']}): {t['summary'][:95]}"
            for t in ev["timeline"]
        ]
    elif ev.get("quality_warnings"):
        item_obj["warnings"] = [
            f"[{w['item_id']}] {','.join(w.get('flags', []))}: {w.get('reason', '')[:70]}"
            for w in ev["quality_warnings"][:2]
        ]
    return item_obj


def build_split_contexts(raw_item_map: dict, batches_events: dict) -> Tuple[dict, dict]:
    """构建轻量化的上下半场结构化素材包（每包约 3,000~4,000 字符）"""
    b3_events = batches_events["2026-11-04"]
    event_map = {e["event_id"]: e for e in b3_events}

    active_b3 = [e for e in b3_events if e["last_updated_batch"] == "2026-11-04"]
    scored_b3 = []
    for ev in active_b3:
        sc, brk = score_batch3_event(ev, raw_item_map)
        scored_b3.append((sc, brk, ev))
    scored_b3.sort(key=lambda x: x[0], reverse=True)

    valid_scored = [(sc, brk, ev) for sc, brk, ev in scored_b3 if not brk["is_pr_or_stale"]]
    priority_ids = ["EVT-034", "EVT-003", "EVT-026", "EVT-041", "EVT-076", "EVT-011", "EVT-042", "EVT-080", "EVT-084"]
    top_candidates = []
    for pid in priority_ids:
        for sc, brk, ev in valid_scored:
            if ev["event_id"] == pid:
                top_candidates.append(_compact_event_for_prompt(ev, brk, include_timeline=True))
                break

    new_valid_events = []
    new_noise_events = []
    fermenting_events = []
    for sc, brk, ev in scored_b3:
        if ev["first_seen_batch"] == "2026-11-04":
            if brk["is_pr_or_stale"]:
                new_noise_events.append(_compact_event_for_prompt(ev, brk))
            else:
                new_valid_events.append(_compact_event_for_prompt(ev, brk))
        else:
            fermenting_events.append(_compact_event_for_prompt(ev, brk, include_timeline=True))

    corrected_and_debunked = []
    for eid in ["EVT-016", "EVT-015", "EVT-061", "EVT-002", "EVT-022"]:
        if eid in event_map:
            corrected_and_debunked.append(_compact_event_for_prompt(event_map[eid], include_timeline=True))

    trend_background = []
    for eid in ["EVT-031", "EVT-062", "EVT-064", "EVT-005", "EVT-006", "EVT-007", "EVT-008"]:
        if eid in event_map:
            trend_background.append(_compact_event_for_prompt(event_map[eid]))

    part1_ctx = {
        "report_date": "2026-11-04",
        "top_focus_events": top_candidates,
        "excluded_noise_today": new_noise_events,
        "related_energy_and_search_background": [
            e for e in trend_background if e["event_id"] in ("EVT-031", "EVT-062", "EVT-005", "EVT-006", "EVT-007")
        ],
    }

    part2_ctx = {
        "report_date": "2026-11-04",
        "newly_emerged_valid_events_today": new_valid_events,
        "newly_emerged_filtered_noise_today": new_noise_events,
        "ongoing_and_fermenting_cross_batch_events": fermenting_events,
        "corrected_and_debunked_events": corrected_and_debunked,
        "trend_support_events": trend_background,
    }
    return part1_ctx, part2_ctx


PART1_SYSTEM_PROMPT = """你是一位严谨、克制的数据与情报分析师，正在撰写《AI 行业舆情分析日报（2026年11月4日）》的第一部分（今日主要事件与背景影响分析）。

【核心行文准则——平实、克制、完全基于事实核对】：
1. **彻底禁用自媒体特稿修辞、戏剧化拔高与口号式金句**：
   - **严禁出现**“舆情场域”、“主线交织”、“正面碰撞”、“撕开裂口”、“危机”、“人力阵痛”、“破局”、“组合拳”、“补课”、“信息脱水”等自媒体词汇或互联网黑话！
   - **严禁使用反问/设问句**（如“实质上是在追问：谁应负责？”）和**主观文学评价**（如“形成了耐人寻味的对照”、“微妙的对照”）。
   - **严禁出现系统汇报套话**（如“入选事件一：”、“这是今日信息密度最高的事件”、“入选理由有三”）。
   - 全文必须采用**平实、准确、克制的客观陈述句**，有一分证据说一分话。
2. **按 4 个具体主要事件组织，自然融合选择依据、背景与影响**：
   - `# AI 行业舆情分析日报（2026年11月4日）`
   - **开篇摘要段（直接陈述事实）**：用两三句平实的陈述句直接概括 2026年11月4日 资讯涵盖的四方面主要动态（Google 将 K-12 搜索与 AI 功能捆绑；出版商就网页抓取起诉 Brave；苹果国行测试千问模型并向全球开放 Siri 跨应用操作；SUSE 宣布因自动化转型裁员 10%）。随后插入当日图表：
     `![图1：当日（2026-11-04）重点事件排行与分类分布](figures/fig1_daily_overview_2026-11-04.png)`
   - `## 一、 今日主要事件与背景影响分析`
     下设 **4 个平实客观的事件小节**（在每个小节内用客观段落讲清：核心事实与数据、为何作为今日重点关注事件、相关背景与实际影响）：
     - `### 1. Google 将 K-12 学校搜索服务与 AI 功能绑定`（陈述 `EVT-034` 4 篇中英文报道确认的政策细节、对依赖 Google 教育生态学校的具体影响，并客观对比同期 OpenAI 披露 ChatGPT 搜索请求量达 Google 20% `EVT-042` 的情况及统计口径限制）
     - `### 2. 《纽约时报》等四大出版商起诉 Brave 抓取超 300 亿网页向 AI 公司供数`（陈述 `EVT-003` 诉讼事实与涉及的 5 家 AI 客户，说明诉讼对象从模型开发商延伸至第三方网页索引商的特点，并客观对比此前四家 AI 企业联合推行的 `robots.txt` 退出标准 `EVT-005` 及 Reddit 反映的流量受损问题 `EVT-006`）
     - `### 3. 苹果 iOS 27.3 推进国行千问模型测试与全球 Siri 第三方应用操作开放`（陈述 `EVT-076` 国行版基于阿里千问开放约 18 项功能测试与 `EVT-041` Siri 引入 App Skills 跨应用任务组合机制的具体内容，客观分析其在本地化合规与系统级助手功能上的进展）
     - `### 4. SUSE 宣布因 AI 与自动化调整裁员约 10%`（陈述 `EVT-080` SUSE 在被殷拓集团收购后计划裁减约 190 人的事实，同时客观交代同日社区关于 Google Gemini 默认使用个人订阅用户保存的聊天记录训练模型且无法单独退出的讨论 `EVT-084`）
   - 在本部分末尾用一段平实的文字（以 `**低质与非当日数据过滤说明：**` 开头）简要说明：今日另有 3 篇单源企业宣传通稿（超维动力展会宣传 `EVT-073`、虎鲸文娱鲸锐AI上线通稿 `EVT-074`、openJiuwen 产品宣发稿 `EVT-075`）因缺乏独立信源核实被标记为 `PR_PROMOTION`，以及 1 篇针对今年 6 月黑客攻击的事后调查报告（币安披露 63 亿美元被盗事件 `EVT-081`）因实际发生时间早于发布日达 5 个月被标记为 `STALE_NEWS`，均已按规则降权排除，不纳入今日主要事件。
3. **句末证据引用规范（零幻觉）**：
   - 将支撑证据自然附在关键事实、数据或结论句末，格式为 `[EVT-编号 | ITEM-编号 (字段名)]`，字段名取自 `content`、`summary`、`key_metrics`、`timeline`、`quality_flags`。
   - **严禁捏造任何 ID！每个 `[EVT-xxx | ITEM-xxx ...]` 方括号内的 `ITEM-xxx` 必须严格属于该 `EVT-xxx` 的 `source_items`！引用不同事件时必须分成两个独立方括号。**"""


PART2_SYSTEM_PROMPT = """你是一位严谨、克制的数据与情报分析师，正在撰写《AI 行业舆情分析日报（2026年11月4日）》的第二部分（跨批次变化与趋势判断）。

【核心行文准则——平实、克制、拒绝绝对化与口号式包装】：
1. **严禁使用绝对化断言与格言式金句**：
   - **严禁出现**“不可逆地转移”、“从发展约束演变为生存约束”、“撕开裂口”、“重塑格局”等未经长期数据证实的夸张口号！
   - 改为平实、客观的数据与事实描述（例如：“数据中心建设面临明显的电网并网周期瓶颈，多家企业采取自建发电设备或与电力公司合作的方式应对”）。
2. **避免与第一部分重复赘述**：第一部分已详细分析过的 4 个事件，在本部分跨批次对比时仅用一句话交代其跨日时间线增量，重点陈述第一部分未展开的持续发酵事件、数据更正记录与今日其他新动态。直接从 `## 二、` 开始输出。
3. **严格输出以下两章内容**：
   - `## 二、 与前几批相比的变化：持续发酵、信息更正与今日新动态`
     开头插入跨批次变化图：`![图2：跨批次（2026-11-02 至 2026-11-04）事件变化：新出现、持续发酵与被更正信息](figures/fig2_cross_batch_changes.png)`
     下设三个子节：
     - `### 2.1 持续发酵与跨日推进的事件`：
       重点陈述两起出现跨日新进展（`ONGOING`）的事件——① **OpenAI 电网合作计划公布首站选址并调整设施称谓（`EVT-011`）**：从 11月2日 发布 `Grid Protectors Program`，到 11月4日 披露首个站点位于佐治亚州 Effingham County，并在公告中首次以 `compute campuses`（算力园区）替代 `data centers`（结合 11月3日 报道数据中心因电网接入需排队数年而改用改装退役喷气发动机发电 `EVT-031` 及 xAI 在密西西比州安装 8 台喷气发动机 `EVT-062` 说明背景）；② **OpenAI 合并 ChatGPT 标签页并明确 Classic 版停用日期（`EVT-026`）**：从 11月2日 宣布合并 Chat 与 Work 标签页并共享 Codex 额度池，到 11月4日 明确 ChatGPT Classic 将于 2027年1月30日 停止服务。随后用简明语言交代其余跨日跟进事件（Apple 18+ AI伴侣应用年龄分级 `EVT-035`、Nexus Mods 确定现有 Mod 合规截止日 `EVT-039`、百度自动驾驶世界模型 Apollo World 测试细节 `EVT-070`，以及前文已述的 `EVT-034`/`003`/`041`/`042` 的跨批次跟进情况）。
     - `### 2.2 被更正的信息与数据口径核对`：
       客观梳理跨报道中被修正的数据与传闻澄清——① **Anthropic S-1/A 招股书支出承诺精确化与亏损性质说明（`EVT-016`）**：算力基础设施支出承诺从 11月2日 初报“近 9000 亿美元”、同日后续报道“约 8950 亿美元”，到 11月3日 根据修订版招股书更正为精确值 **8954 亿美元**（Q3 营收精确为 **169.6 亿美元**，算力成本占营收比从 48% 升至 58%）；同时澄清 **Q3 约 230 亿美元账面净亏损主要源于可转换票据及衍生负债公允价值变动的会计准则减计，并非实际现金消耗**（Q3 实际营业亏损为 5.67 亿美元）；② **Anthropic Q3 营收环比增速口径差异（`EVT-015`）**：11月2日 初报按 Q2(116亿) 至 Q3(168亿) 计算环比增长 45%，而修订版披露环比增速放缓至约 19%，同时补充 9 个月累计营收 330 亿美元、运营亏损 12.5 亿美元；③ **Revolut 切换开源智谱 GLM 降本 90% 的前提条件说明（`EVT-061`）**：补充明确该数据基于部署 **56 块 Nvidia B200 GPU** 实现每百万 token 0.30 美元混合成本，且编码时间缩短 60% 的内部自测数据**未隔离取消限额等其他变量**；④ **夸大标题与未证实传闻澄清**：说明量子位《RSI我们快要控制不住了》标题夸大（`EVT-002` 招股书原文明确当前在役模型仍在可控范围内），以及 HuggingFace 疑似泄露 GPT-6.2-Astra/Claude 权重传闻（`EVT-022` 报道正文自认不太可能为真）。
     - `### 2.3 今日新出现的其他动态`：
       平实梳理 11月4日 首次出现的其他有效事件：招商局狮子山实验室发布具身推理模型「驭物 Lion-ER-35B-A3B」（MoE 架构总参数 35B、每 Token 激活 3B，十项具身基准指标提升 `EVT-072`）、Nvidia RTX Spark 笔记本在 Linux 下因固件无法读取电源管理信息锁频约 780MHz 导致 120B 模型推理速度从约 65 tokens/s 降至不足 14 tokens/s（`EVT-085`）、富士胶片因云厂商与数据中心冷备份及物理隔离需求扩大磁带产能（`EVT-079`）、媒体爆料字节跳动视频模型 Seedance 海外收入占比达 50%（`EVT-077`）、腾讯据报准备发布 C 端 AI 搜索引擎（`EVT-078`），以及 `LangChain`（`EVT-082`）、`llama.cpp`（`EVT-083`）、华为仓颉语言编码 Agent `cjh`（`EVT-086`）开源项目更新。
   - `## 三、 证据范围内的趋势判断`
     用平实、客观的语言阐述 3 点观察，并在每段内自然说明当前样本不足以支撑哪些延伸结论（不勉强下结论）：
     - **1. 数据中心建设面临明显的电网并网周期瓶颈，企业同步背负大额长期基建支出承诺**：结合北弗吉尼亚接入 100MW 电网需排队约 7 年、多家数据中心改用改装退役喷气发动机（24 个月内可投运）`EVT-031`、xAI 安装 8 台喷气发动机 `EVT-062`、OpenAI 落地佐治亚州电网合作站点 `EVT-011` 以及 Anthropic 8954 亿美元支出承诺 `EVT-016` 与科技巨头 3.2 万亿美元表外债务 `EVT-064` 进行客观归纳；同时明确指出：针对 3.2 万亿美元表外债务中常规长期机房租约与纯 AI 专项债务的具体占比，社区与现有报道仍存在统计口径争议，当前样本不足以评估具体的债务违约风险。
     - **2. AI 搜索功能加快嵌入主流入口，引发网站流量下滑争议与网页抓取诉讼**：结合 Google 在 K-12 强行绑定搜索与 AI 功能 `EVT-034`、Reddit 反映 Google AI 搜索导致其引流下降 `EVT-006`、出版商起诉 Brave 抓取 300 亿网页向 AI 公司供数 `EVT-003` 及四大企业推行 `robots.txt` 退出标准 `EVT-005` 进行归纳；同时明确说明：OpenAI 称 ChatGPT 搜索量达 Google 20% 系企业单方按“触发网络检索次数”估算且未公开对比基准 `EVT-042`，腾讯筹备 AI 搜索 `EVT-078` 与字节 Seedance 海外收入占比 `EVT-077` 均仅来自单一媒体爆料，因缺乏独立第三方监测数据，当前样本不足以判断搜索市场的真实份额变化。
     - **3. 推理侧调用量增长推动稀疏架构与自托管降本尝试，但现有个案尚不足以推演商业 API 萎缩或行业普遍裁员**：结合赛迪研究院披露中国 AI 推理数据量首次超过训练数据量 `EVT-008`、招商局 35B 激活 3B 的 MoE 模型 `EVT-072` 及 Revolut 用 56 块 B200 自托管智谱 GLM 将每百万 token 成本降至 0.30 美元 `EVT-061` 进行归纳；同时明确说明：**当前样本不足以得出“闭源商业 API 收入即将萎缩”或“AI 已引发软件行业普遍裁员潮”的结论**——前者有 Anthropic Q3 营收仍同比增长 1127% 达 169.6 亿美元 `EVT-016` 作为反证，且 Revolut 自建 56 块 B200 硬件门槛较高、提速数据未隔离混杂变量；后者在三天数据中仅有 SUSE 一家处于私募并购重组期的企业披露裁员 10% `EVT-080`，无法分离并购精简与 AI 替代的各自影响，样本量不足以向全行业外推。
4. **句末证据引用规范（零幻觉）**：
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
    b3_events = batches_events["2026-11-04"]
    event_map = {e["event_id"]: e for e in b3_events}

    part1_ctx, part2_ctx = build_split_contexts(raw_item_map, batches_events)

    p1_user = (
        "请基于以下结构化事件数据，撰写日报的【第一部分：一、今日焦点事件深度解析（精选4个焦点事件，按事件组织，融合核心事实、关注理由、背景与深远影响）】。\n"
        "直接输出 Markdown 正文：\n\n"
        + json.dumps(part1_ctx, ensure_ascii=False, separators=(",", ":"))
    )
    p2_user = (
        "请基于以下结构化事件数据，撰写日报的【第二部分：二、跨日追踪（持续发酵、被更正信息、今日新动态）与 三、行业趋势研判（在趋势分析中自然融合证据边界与样本不足说明）】。\n"
        "直接从 `## 二、` 开始输出 Markdown 正文：\n\n"
        + json.dumps(part2_ctx, ensure_ascii=False, separators=(",", ":"))
    )

    print("  - [AI 撰稿 1/2] 正在生成日报前半部分（今日主要事件 + 背景与影响深度分析）...")
    md_part1 = call_llm_markdown(PART1_SYSTEM_PROMPT, p1_user, temperature=0.1)

    print("  - [AI 撰稿 2/2] 正在生成日报后半部分（跨批次三类变化 + 趋势判断与样本边界）...")
    md_part2 = call_llm_markdown(PART2_SYSTEM_PROMPT, p2_user, temperature=0.1)

    full_md = md_part1.strip() + "\n\n---\n\n" + md_part2.strip()
    cleaned_md = _verify_and_sanitize_citations(full_md, event_map, raw_item_map)

    report_path = OUTPUT_DIR / "daily_report_2026-11-04.md"
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
