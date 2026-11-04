# -*- coding: utf-8 -*-
"""
核心结构化抽取与跨批次事件实体归并引擎 (Context-Aware Extractor & Incremental Event Merger)
- 严格按照 BATCH-1 (2026-11-02) -> BATCH-2 (2026-11-03) -> BATCH-3 (2026-11-04) 顺序推进
- 读取 rules/event_taxonomy_and_merge_rules.json 中的分类体系、质量诊断规则与初始实体别名表
- 输出每个批次的结构化抽取产物与事件实体表快照至 data/intermediate/
"""

import concurrent.futures
import copy
import json
from datetime import datetime
from pathlib import Path

from src.llm_client import call_llm_json, save_cache

BASE_DIR = Path(__file__).resolve().parent.parent
RULES_FILE = BASE_DIR / "rules" / "event_taxonomy_and_merge_rules.json"
POSTPROCESSED_DIR = BASE_DIR / "data" / "postprocessed"
INTERMEDIATE_DIR = BASE_DIR / "data" / "intermediate"
INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)


def load_rules() -> dict:
    with open(RULES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_entity(name: str, alias_lookup: dict) -> str:
    if not name:
        return ""
    key = name.strip().lower()
    return alias_lookup.get(key, name.strip())


def register_new_aliases(alias_dict: dict, alias_lookup: dict):
    """动态扩充实体别名与跨语种概念映射表"""
    if not isinstance(alias_dict, dict):
        return
    for alias, canonical in alias_dict.items():
        if not alias or not canonical or not isinstance(alias, str) or not isinstance(canonical, str):
            continue
        k = alias.strip().lower()
        c_norm = alias_lookup.get(canonical.strip().lower(), canonical.strip())
        if k and k not in alias_lookup:
            alias_lookup[k] = c_norm


def build_registry_compact_index(event_registry: list) -> str:
    """将当前事件实体表压缩为极简五元组索引，注入 Stage 1 Prompt"""
    if not event_registry:
        return "（当前事件实体表为空，尚无历史事件）"
    lines = []
    for ev in event_registry:
        actors_str = ", ".join(ev["actors"][:5])
        targets_str = ", ".join(ev["target_objects"][:5])
        lines.append(
            f"- [{ev['event_id']}] 主体:{actors_str} | 源头动作:{ev['action'][:45]} | 标的:{targets_str} | 时间:{ev['initial_event_time']} | 标题:{ev['canonical_title']}"
        )
    return "\n".join(lines)


def build_stage1_system_prompt(categories: list) -> str:
    return f"""你是一个具备上下文记忆的 AI 行业情报结构化抽取与数据质量审计引擎。
你将收到：
1. 当前待处理的【单篇新闻原文 (news_item)】
2. 当前全局维护的【已有事件实体表极简索引 (active_event_index)】

请完成两项任务：

任务一：【数据质量与噪音诊断 (quality_flags)】
判断该条目是否存在以下质量问题（可多选，若完全正常则为空列表 []）：
- "EXAGGERATED_TITLE": 标题夸大/标题党/悬念隐去主语（如正文是常规招股书风险披露或第三方应用测评，标题却渲染成“紧急修改、快控制不住了”或蹭乔布斯噱头）
- "PR_PROMOTION": 商业公关稿/软文通稿（如文末含“本文由XX提供/授权转载”、通篇单方面企业获奖或产品宣传而无独立信源）
- "EVENT_AD": 纯线下展会/峰会售票促销广告（如 TechCrunch Founder Summit 门票打折促销，无实质科技新闻事件）
- "STALE_NEWS": 旧闻新发/滞后报道（正文中核心事件实际发生日期早于文章发布日期 published_at 超过 3 天以上）
- "UNVERIFIED_RUMOR": 未证实传闻或正文自我证伪（如标题宣称模型权重泄露，正文承认极可能是假仓库）
- "MULTI_TOPIC_DIGEST": 多事件早报/晚报合集（单篇资讯包含多个互不相关的独立新闻小节）

任务二：【上下文感知的原子事件五元组抽取 (extracted_events)】
1. 若为纯线下展会/峰会售票广告 ("EVENT_AD")，extracted_events 必须返回空列表 []。
2. 若为多事件早报合集 ("MULTI_TOPIC_DIGEST")，请遵循【篇章结构权重 + AI核心度】双重标准拆解：
   - **只提取**与 AI 大模型/Agent、算力芯片与能源基建、AI企业财报/IPO/重大并购、AI版权诉讼与安全监管、核心AI产品直接相关，且在早报中**作为重点小节独立展开（尤其是标题点名）的核心事件**；
   - **严格过滤**并放入 discarded_subtopics：① 传统车企促销/出海、传统游戏销量/帧率、充电器配件、影视娱乐等非AI内容；② 早报末尾一两句话带过的纯硬件外观/屏幕供应链消息（如折叠屏手机缺货、笔记本屏幕涨价）、高管直播带货卖卡、App年龄分级等边缘快讯。
3. **【关键：结合已有事件索引提取五元组，防止遗漏隐蔽关联】**：
   - 阅读正文时，请对照输入的 `active_event_index`。
   - **区分【客观源头动作】与【媒体报道切入角度】**：如果正文报道的事实（例如“某公司内部AI算力消耗超过员工薪资”或“某公司面临算力经济性挑战”）在正文中明确说明源自某次已有的客观源头动作（例如同日提交的IPO招股书披露），请将该客观源头动作及载体一并提取到 `action` 和 `target_objects` 中，并在 `candidate_event_ids` 中填入对应的历史事件 ID！
   - 如果标题是宽泛行业综述（如“数据中心转向旧喷气发动机发电”），但正文核心案例是具体企业（如 xAI 在密西西比州安装喷气发动机），必须在 `actors` 和 `location` 中把具体企业和地点提取出来！
   - 如果正文中的中英文概念与已有事件中的概念互为跨语种对应（如 `"compute campuses"` 对应 `"AI算力园区"`），请在 `target_objects` 和 `entity_aliases_discovered` 中明确标注！

每个原子事件必须包含以下字段：
- "event_title": 客观、中立的中文事件标准名称（15~30字，剥离标题党情绪词）
- "category": 严格从以下 6 个枚举值中选 1 个：{json.dumps(categories, ensure_ascii=False)}
- "actors": 核心参与主体列表（知名企业/模型优先使用标准名，如 ["Anthropic"]、["Google"]、["Apple", "Alibaba Qwen"]、["Revolut", "Zhipu GLM", "Anthropic Claude Code"]）
- "action": 现实中发生的客观源头动作短语
- "target_objects": 动作直接作用的具体标的物列表
- "event_time": 事件实际发生日期（YYYY-MM-DD；若正文未提具体哪天，填文章发布日期 YYYY-MM-DD）
- "location": 事件发生国家或地区（如 "美国"、"中国"、"英国"、"全球"）
- "factual_summary": 1~2句客观事实摘要（若含社区高赞评论的关键反驳或背景补充，请一并概括）
- "metrics": 核心量化数据口径列表（无则填 []），每项含 `{{"metric_name": "...", "value": "...", "scope_note": "..."}}`
- "entity_aliases_discovered": 文中发现的别名或跨语种概念映射字典（如 {{"A社": "Anthropic", "compute campuses": "AI算力园区"}}）
- "candidate_event_ids": 对照 `active_event_index`，列出与本事件高度疑似属于【同一客观事件或跨批次演进】的历史 event_id 列表（若确属全新独立事件则填 []）

请严格输出合法的 JSON 对象（不要输出额外说明文字；JSON 字符串值内部如需引用词语请一律使用中文引号「...」或单引号 '...'，严禁在字符串值内使用未转义的英文双引号）：
{{
  "item_id": "...",
  "quality_flags": [],
  "quality_reason": "简述原因（50字内）",
  "discarded_subtopics": [],
  "extracted_events": [
    {{
      "event_title": "...",
      "category": "...",
      "actors": [],
      "action": "...",
      "target_objects": [],
      "event_time": "...",
      "location": "...",
      "factual_summary": "...",
      "metrics": [],
      "entity_aliases_discovered": {{}},
      "candidate_event_ids": []
    }}
  ]
}}"""


STAGE2_MERGE_SYSTEM_PROMPT = """你是一个跨批次增量事件实体对齐与五元组演进引擎 (Event Entity Resolution & Tuple Evolution Engine)。
你将收到：
1. 当前新提取的【待归并原子事件 (new_event)】
2. 召回的【候选历史事件完整记录 (candidate_events)】

请你基于五元组（actors, action, target_objects, event_time, location）进行比对判定：
【通用合并与防误合准则】：
1. **区分【客观源头动作】与【媒体报道角度】（防漏合）**：
   当两条事件指向**同一主体在同一时间窗口内发生的同一个客观源头动作**时，即使不同媒体聚焦报道了该动作的不同侧面（例如：围绕同一次招股书披露，不同报道分别聚焦其中的安全风险警告、营收与算力支出、内部AI使用成本、算力毛利挑战；或行业综述与具体企业案例讲同一件事；或中英文报道同一政策/诉讼/功能测试），它们都属于**同一个客观事件实体**，必须合并！
2. **同主体不同独立动作严禁合并（防误合）**：
   如果主体相同（例如都是 Anthropic、Apple、Google 或 OpenAI），但现实中发生的是**完全不同的两个独立动作/不同标的**（例如“Anthropic 提交 S-1 招股书” vs “Anthropic 开源 FlashProve 模型身份验证系统”；或“Apple 国行 Apple Intelligence 接入千问” vs “Apple 开放 Siri App Skills 第三方操作接口”；或“Google 强制捆绑 AI 否则切断学校搜索服务” vs “Google 推出 Dreambeans 故事应用”），绝对不能合并，必须判定为 `"NEW_EVENT"`！

若判定为合并，请从以下 3 种演进状态中选择 1 种：
- "MERGE_CORRECTED": 新报道提供了更精确的官方数据口径修正了早期的粗略约数（例如 $895B 或 8954亿美元 修正了早期的 $900B；169.6亿美元 精确化了 170亿美元），或澄清了亏损构成/证伪了传闻（注意：只要新报道对已有指标数值进行了精确化修正或口径澄清，优先选 MERGE_CORRECTED！）
- "MERGE_ONGOING": 同一事件在后续日期出现新进展、新官方回应、正式落地启用或事态升级
- "MERGE_CORROBORATED": 多源交叉印证或同一客观事件的不同侧面补充报道（数值无冲突修正、事态无跨阶段变化）

请严格输出合法的 JSON 对象（reasoning 控制在 80 字以内，不要换行）：
{
  "decision": "NEW_EVENT" | "MERGE_CORROBORATED" | "MERGE_ONGOING" | "MERGE_CORRECTED",
  "matched_event_id": "若合并填对应的 event_id，若为 NEW_EVENT 填 null",
  "reasoning": "80字以内简述五元组比对依据",
  "updated_canonical_title": "若合并，给出涵盖各侧面及最新精确口径的标准事件标题；否则填 null",
  "updated_action": "若合并，给出更新后的客观源头动作描述；否则填 null",
  "updated_synthesis_summary": "若合并，给出融合历史各方面信息与本次新增信息后的综合事实摘要（若有口径修正，必须明确写出前值与修正值）；否则填 null",
  "metric_resolution_note": "若存在数据口径冲突、近似值精确化或口径澄清，说明最新共识口径及差异原因；否则填 null"
}"""


def check_date_lag_stale(published_at_iso: str, event_time_str: str) -> bool:
    """确定性代码校验：判断 event_time 是否早于 published_at 超过 3 天"""
    try:
        pub_dt = datetime.strptime(published_at_iso[:10], "%Y-%m-%d")
        if len(event_time_str) == 10:
            ev_dt = datetime.strptime(event_time_str, "%Y-%m-%d")
            return (pub_dt - ev_dt).days > 3
        elif len(event_time_str) == 7:
            ev_dt = datetime.strptime(event_time_str + "-01", "%Y-%m-%d")
            return (pub_dt - ev_dt).days > 15
    except Exception:
        pass
    return False


def detect_item_lang(item: dict) -> str:
    if item.get("lang") in ("zh", "en"):
        return item["lang"]
    if item.get("language") in ("zh", "en"):
        return item["language"]
    for ch in item.get("title", ""):
        if "\u4e00" <= ch <= "\u9fff":
            return "zh"
    return "en"


def recall_candidates(ev: dict, event_registry: list, alias_lookup: dict) -> list:
    """结合 Stage 1 LLM 推荐的 candidate_event_ids 与加权实体/标的/标题相关性得分召回并排序候选事件"""
    llm_cand_ids = set(ev.get("candidate_event_ids", []))
    new_actors = {normalize_entity(a, alias_lookup).lower() for a in ev.get("actors", []) if a}
    new_targets = {normalize_entity(t, alias_lookup).lower() for t in ev.get("target_objects", []) if t}
    new_title = ev.get("event_title", "")
    broad_giants = {"openai", "google", "anthropic", "apple", "microsoft", "nvidia", "meta"}

    scored_candidates = []
    for reg_ev in event_registry:
        score = 0
        if reg_ev["event_id"] in llm_cand_ids:
            score += 100

        reg_actors = {normalize_entity(a, alias_lookup).lower() for a in reg_ev.get("actors", []) if a}
        reg_targets = {normalize_entity(t, alias_lookup).lower() for t in reg_ev.get("target_objects", []) if t}

        common_actors = new_actors & reg_actors
        common_targets = new_targets & reg_targets

        for ca in common_actors:
            score += 2 if ca in broad_giants else 12
        score += len(common_targets) * 10

        # 标题双字重合度加分（辅助同批次紧邻相似事件排序）
        reg_title = reg_ev.get("canonical_title", "")
        if new_title and reg_title:
            bigrams_new = {new_title[i : i + 2] for i in range(len(new_title) - 1)}
            bigrams_reg = {reg_title[i : i + 2] for i in range(len(reg_title) - 1)}
            score += len(bigrams_new & bigrams_reg)

        if score >= 2 and (reg_ev["event_id"] in llm_cand_ids or common_actors or common_targets):
            scored_candidates.append((score, reg_ev))

    scored_candidates.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in scored_candidates]


def create_new_event_entity(
    event_id: str, batch_date: str, item: dict, ev: dict, q_flags: list, q_reason: str, valid_categories: list
) -> dict:
    item_id = item["item_id"]
    ev_time = ev.get("event_time") or batch_date
    cat = ev.get("category", valid_categories[0])
    if cat not in valid_categories:
        # 模糊匹配修正枚举
        for vc in valid_categories:
            if cat[:4] in vc or vc[:4] in cat:
                cat = vc
                break
        else:
            cat = valid_categories[0]

    return {
        "event_id": event_id,
        "canonical_title": ev["event_title"],
        "category": cat,
        "status": "NEW",
        "first_seen_batch": batch_date,
        "last_updated_batch": batch_date,
        "initial_event_time": ev_time,
        "latest_event_time": ev_time,
        "actors": ev["actors_normalized"],
        "action": ev["action"],
        "target_objects": ev["targets_normalized"],
        "location": ev.get("location", "全球"),
        "synthesis_summary": ev["factual_summary"],
        "source_items": [item_id],
        "languages": [detect_item_lang(item)],
        "quality_warnings": [{"item_id": item_id, "flags": q_flags, "reason": q_reason}] if q_flags else [],
        "metrics_ledger": [
            {
                "item_id": item_id,
                "batch_date": batch_date,
                "source": item["source"],
                "metric_name": m.get("metric_name", ""),
                "value": m.get("value", ""),
                "scope_note": m.get("scope_note", ""),
            }
            for m in ev.get("metrics", [])
            if isinstance(m, dict) and m.get("value")
        ],
        "metric_resolution_note": None,
        "timeline": [
            {
                "batch_date": batch_date,
                "item_id": item_id,
                "source": item["source"],
                "transition": "NEW_EVENT",
                "summary": ev["factual_summary"],
            }
        ],
    }


def update_existing_event_entity(
    target_reg: dict, decision: str, decision_res: dict, batch_date: str, item: dict, ev: dict, q_flags: list, q_reason: str
):
    """跨批次动态演进更新事件实体表中的五元组、时间线与数据口径台账"""
    item_id = item["item_id"]
    status_map = {
        "MERGE_CORROBORATED": "CORROBORATED",
        "MERGE_ONGOING": "ONGOING",
        "MERGE_CORRECTED": "CORRECTED",
    }
    new_status = status_map.get(decision, "CORROBORATED")

    # 若生成了口径修正说明且包含精确化/修正词汇，自动提升状态至 CORRECTED
    res_note = decision_res.get("metric_resolution_note")
    if res_note and any(k in str(res_note) for k in ["修正", "精确", "近似", "为准", "分歧", "冲突", "口径"]):
        new_status = "CORRECTED"
        decision = "MERGE_CORRECTED"

    priority = {"NEW": 0, "CORROBORATED": 1, "ONGOING": 2, "CORRECTED": 3}
    if priority.get(new_status, 1) >= priority.get(target_reg["status"], 0):
        target_reg["status"] = new_status

    target_reg["last_updated_batch"] = batch_date
    ev_time = ev.get("event_time") or batch_date
    if len(ev_time) == 10 and len(target_reg["latest_event_time"]) == 10:
        if ev_time > target_reg["latest_event_time"]:
            target_reg["latest_event_time"] = ev_time
        if ev_time < target_reg["initial_event_time"]:
            target_reg["initial_event_time"] = ev_time

    if decision_res.get("updated_canonical_title"):
        target_reg["canonical_title"] = decision_res["updated_canonical_title"]
    if decision_res.get("updated_action"):
        target_reg["action"] = decision_res["updated_action"]
    if decision_res.get("updated_synthesis_summary"):
        target_reg["synthesis_summary"] = decision_res["updated_synthesis_summary"]
    if res_note:
        target_reg["metric_resolution_note"] = res_note

    for a in ev["actors_normalized"]:
        if a and a not in target_reg["actors"]:
            target_reg["actors"].append(a)
    for t in ev["targets_normalized"]:
        if t and t not in target_reg["target_objects"]:
            target_reg["target_objects"].append(t)
    if ev.get("location") and ev["location"] not in target_reg["location"]:
        target_reg["location"] = f"{target_reg['location']} / {ev['location']}".strip(" /")

    lang = detect_item_lang(item)
    if lang not in target_reg.get("languages", []):
        target_reg.setdefault("languages", []).append(lang)

    if item_id not in target_reg["source_items"]:
        target_reg["source_items"].append(item_id)
    if q_flags:
        if not any(w["item_id"] == item_id for w in target_reg["quality_warnings"]):
            target_reg["quality_warnings"].append({"item_id": item_id, "flags": q_flags, "reason": q_reason})
    for m in ev.get("metrics", []):
        if isinstance(m, dict) and m.get("value"):
            target_reg["metrics_ledger"].append(
                {
                    "item_id": item_id,
                    "batch_date": batch_date,
                    "source": item["source"],
                    "metric_name": m.get("metric_name", ""),
                    "value": m.get("value", ""),
                    "scope_note": m.get("scope_note", ""),
                }
            )
    target_reg["timeline"].append(
        {
            "batch_date": batch_date,
            "item_id": item_id,
            "source": item["source"],
            "transition": decision,
            "summary": ev["factual_summary"],
            "reasoning": decision_res.get("reasoning", ""),
        }
    )


def run_extraction_and_merging() -> dict:
    rules = load_rules()
    valid_categories = [c["label"] for c in rules["event_categories"]]
    alias_lookup = dict(rules["initial_entity_aliases"])
    stage1_sys_prompt = build_stage1_system_prompt(valid_categories)

    batches_cfg = [
        ("BATCH-1", "2026-11-02", POSTPROCESSED_DIR / "batch_1_2026-11-02.json"),
        ("BATCH-2", "2026-11-03", POSTPROCESSED_DIR / "batch_2_2026-11-03.json"),
        ("BATCH-3", "2026-11-04", POSTPROCESSED_DIR / "batch_3_2026-11-04.json"),
    ]

    event_registry = []
    merge_audit_logs = []
    batch_stats = {}
    event_seq = 1

    for batch_id, batch_date, batch_path in batches_cfg:
        with open(batch_path, "r", encoding="utf-8") as f:
            items = json.load(f)

        print(f"\n" + "=" * 80)
        print(f"开始处理 {batch_id} ({batch_date}) | 待处理资讯数: {len(items)} 条 | 当前事件表规模: {len(event_registry)} 个")
        print("=" * 80)

        batch_extractions = []
        chunk_size = 2  # 每 2 条一组并发执行 Stage 1（避免触发远程 API 连接重置），随后立即串行执行 Stage 2 更新事件实体表

        for chunk_start in range(0, len(items), chunk_size):
            chunk_items = items[chunk_start : chunk_start + chunk_size]
            active_index_str = build_registry_compact_index(event_registry)

            def _extract_one(itm):
                u_prompt = json.dumps(
                    {
                        "active_event_index": active_index_str,
                        "news_item": {
                            "item_id": itm["item_id"],
                            "source": itm["source"],
                            "published_at": itm["published_at"],
                            "title": itm["title"],
                            "content": itm["content"],
                            "hn_comments": itm.get("metadata", {}).get("top_comments", []),
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                return call_llm_json(stage1_sys_prompt, u_prompt)

            with concurrent.futures.ThreadPoolExecutor(max_workers=chunk_size) as executor:
                future_map = {executor.submit(_extract_one, itm): itm for itm in chunk_items}
                chunk_results = {}
                for fut in concurrent.futures.as_completed(future_map):
                    itm = future_map[fut]
                    chunk_results[itm["item_id"]] = fut.result()

            # 按原始 item_id 顺序逐条处理抽取结果并更新事件实体表
            for item in chunk_items:
                item_id = item["item_id"]
                extracted = chunk_results[item_id]
                extracted["item_id"] = item_id
                extracted["source"] = item["source"]
                extracted["published_at"] = item["published_at"]
                extracted["title"] = item["title"]

                q_flags = extracted.get("quality_flags", [])
                if not isinstance(q_flags, list):
                    q_flags = []
                q_reason = extracted.get("quality_reason", "")
                sub_events = extracted.get("extracted_events", [])
                if not isinstance(sub_events, list):
                    sub_events = []

                # 确定性规则补充校验 1：纯峰会售票广告强制标记 EVENT_AD 并清空事件
                if "founder summit" in item["title"].lower() and "save up to" in item["title"].lower():
                    if "EVENT_AD" not in q_flags:
                        q_flags.append("EVENT_AD")
                    q_reason = "TechCrunch 线下峰会门票促销广告，无实质行业新闻事件"
                    sub_events = []
                    extracted["extracted_events"] = []

                # 确定性规则补充校验 2：检查每个抽取事件的 event_time 是否早于 published_at > 3 天
                for ev in sub_events:
                    ev_time = ev.get("event_time", batch_date)
                    if check_date_lag_stale(item["published_at"], ev_time):
                        if "STALE_NEWS" not in q_flags:
                            q_flags.append("STALE_NEWS")
                            q_reason = (q_reason + f"；事件实际发生日({ev_time})早于发布日超过3天").strip("；")

                extracted["quality_flags"] = q_flags
                extracted["quality_reason"] = q_reason
                batch_extractions.append(extracted)

                print(f"  [{item_id}] {item['source']} | 质量标签:{q_flags} | 抽取事件数:{len(sub_events)}")

                if "EVENT_AD" in q_flags:
                    print(f"    -> [已拦截纯商业广告] {item['title'][:50]}")
                    continue

                for ev in sub_events:
                    register_new_aliases(ev.get("entity_aliases_discovered", {}), alias_lookup)
                    norm_actors = list(
                        dict.fromkeys(normalize_entity(a, alias_lookup) for a in ev.get("actors", []) if a)
                    )
                    norm_targets = list(
                        dict.fromkeys(normalize_entity(t, alias_lookup) for t in ev.get("target_objects", []) if t)
                    )
                    if not norm_actors:
                        authors = item.get("metadata", {}).get("authors", [])
                        if authors and isinstance(authors, list):
                            norm_actors = authors[:2]
                        elif norm_targets:
                            norm_actors = [norm_targets[0]]
                        else:
                            norm_actors = [item["source"]]
                    ev["actors_normalized"] = norm_actors
                    ev["targets_normalized"] = norm_targets

                    candidates = recall_candidates(ev, event_registry, alias_lookup)
                    if not candidates:
                        new_event_id = f"EVT-{event_seq:03d}"
                        event_seq += 1
                        reg_entry = create_new_event_entity(
                            new_event_id, batch_date, item, ev, q_flags, q_reason, valid_categories
                        )
                        event_registry.append(reg_entry)
                        print(f"    -> [新建 {new_event_id}] {ev['event_title']} (主体:{norm_actors[:3]})")
                    else:
                        cand_slim = [
                            {
                                "event_id": c["event_id"],
                                "canonical_title": c["canonical_title"],
                                "category": c["category"],
                                "actors": c["actors"],
                                "action": c["action"],
                                "target_objects": c["target_objects"],
                                "initial_event_time": c["initial_event_time"],
                                "location": c["location"],
                                "synthesis_summary": c["synthesis_summary"],
                                "metrics_ledger": c["metrics_ledger"],
                                "source_items": c["source_items"],
                            }
                            for c in candidates[:8]
                        ]
                        merge_prompt = json.dumps(
                            {
                                "batch_date": batch_date,
                                "source_item_id": item_id,
                                "source_name": item["source"],
                                "new_event": ev,
                                "candidate_events": cand_slim,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                        decision_res = call_llm_json(STAGE2_MERGE_SYSTEM_PROMPT, merge_prompt)
                        decision = decision_res.get("decision", "NEW_EVENT")
                        matched_id = decision_res.get("matched_event_id")

                        target_reg = next((c for c in event_registry if c["event_id"] == matched_id), None)
                        if decision == "NEW_EVENT" or target_reg is None:
                            new_event_id = f"EVT-{event_seq:03d}"
                            event_seq += 1
                            reg_entry = create_new_event_entity(
                                new_event_id, batch_date, item, ev, q_flags, q_reason, valid_categories
                            )
                            event_registry.append(reg_entry)
                            merge_audit_logs.append(
                                {
                                    "batch_date": batch_date,
                                    "item_id": item_id,
                                    "new_event_title": ev["event_title"],
                                    "candidate_ids": [c["event_id"] for c in candidates[:8]],
                                    "decision": "NEW_EVENT",
                                    "matched_event_id": new_event_id,
                                    "reasoning": decision_res.get("reasoning", ""),
                                }
                            )
                            print(f"    -> [对比后新建 {new_event_id}] {ev['event_title']}")
                        else:
                            update_existing_event_entity(
                                target_reg, decision, decision_res, batch_date, item, ev, q_flags, q_reason
                            )
                            merge_audit_logs.append(
                                {
                                    "batch_date": batch_date,
                                    "item_id": item_id,
                                    "new_event_title": ev["event_title"],
                                    "candidate_ids": [c["event_id"] for c in candidates[:8]],
                                    "decision": target_reg["timeline"][-1]["transition"],
                                    "matched_event_id": matched_id,
                                    "reasoning": decision_res.get("reasoning", ""),
                                }
                            )
                            print(
                                f"    -> [{target_reg['timeline'][-1]['transition']} -> {matched_id}] 状态:{target_reg['status']} | 关联数:{len(target_reg['source_items'])}"
                            )

        # 保存当前批次的结构化抽取产物与事件实体表快照
        idx_num = batch_id.split("-")[1]
        ext_out_path = INTERMEDIATE_DIR / f"batch_{idx_num}_{batch_date}_extractions.json"
        evt_out_path = INTERMEDIATE_DIR / f"batch_{idx_num}_{batch_date}_events.json"

        with open(ext_out_path, "w", encoding="utf-8") as f:
            json.dump(batch_extractions, f, ensure_ascii=False, indent=2)
        with open(evt_out_path, "w", encoding="utf-8") as f:
            json.dump(copy.deepcopy(event_registry), f, ensure_ascii=False, indent=2)

        status_counts = {}
        for e in event_registry:
            status_counts[e["status"]] = status_counts.get(e["status"], 0) + 1
        batch_stats[batch_id] = {
            "batch_date": batch_date,
            "input_items": len(items),
            "total_events_in_registry": len(event_registry),
            "status_breakdown": status_counts,
        }
        print(f"\n>>> {batch_id} 完成！已保存中间产物：{ext_out_path.name}, {evt_out_path.name}")
        print(f"    当前事件表统计: {status_counts}")

    # 保存全局归并审计日志与最终实体别名表快照
    with open(INTERMEDIATE_DIR / "merge_audit_log.json", "w", encoding="utf-8") as f:
        json.dump(merge_audit_logs, f, ensure_ascii=False, indent=2)
    with open(INTERMEDIATE_DIR / "entity_aliases_snapshot.json", "w", encoding="utf-8") as f:
        json.dump(alias_lookup, f, ensure_ascii=False, indent=2)

    save_cache()
    return {
        "batch_stats": batch_stats,
        "total_events": len(event_registry),
        "total_merge_decisions": len(merge_audit_logs),
    }
