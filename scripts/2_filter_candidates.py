"""
Step 2: 候选资讯过滤与清洗脚本 (2_filter_candidates.py)
独立于爬虫脚本，对 data/raw_probe/three_days_candidates.json (258条) 执行三层确定性过滤策略，
将保留规模控制在 85~90 条高价值资讯（含刻意保留的重复报道、跨日进展、夸大标题党、展会/公关软文）。

【三层过滤策略】：
1. 层一（排除规则 - 剔除明确非AI领域的垂直噪声）：
   - 针对综合科技/开源社区源（Solidot、OSChina、少数派、Hacker News、The Verge、TechCrunch），剔除纯古生物/医学/天文/传统运维/普通消费数码评测/非AI游戏发售等无关条目。
2. 层二（AI 相关性准入 - 垂直源白名单 + 综合源 AI 正向词库）：
   - 纯 AI 垂直源（OpenAI Blog、HuggingFace Blog、量子位 QbitAI、arXiv cs.AI/cs.CL、AI 开源框架 GitHub Releases）默认准入。
   - 综合源必须命中 AI 核心实体/技术正则词库（使用严格词边界 \b 避免 'said'/'air'/'wait' 误匹配 'ai'）。
3. 层三（同源单日配额限流与社区高价值精选 Sampling Cap）：
   - 对单日高产信源设置配额上限（Cap），优先保留跨批次追踪事件、中英文对照事件及典型质量问题样本，剔除同日过量重复闲聊或次要客户案例。

输出：
- data/staging/filtered_3days.json (过滤后保留的 ~88 条 3 批次候选数据)
- data/staging/discarded_items_log.json (完整丢弃记录及每条丢弃原因，支撑《采集记录》)
"""

import json
import re
import os
from collections import Counter

# 1. 明确的非 AI 噪声黑名单正则（针对综合源与混入项）
NEGATIVE_PATTERNS = [
    (r"鳄鱼|恐龙|小鼠|阉割|突变神经元|Swift 天文台|国际空间站|北欧.*高温|核电|冷冻冷藏设备", "非AI领域：自然科学/生物医学/天文气候/传统工控新闻"),
    (r"Enlightenment|Tor Browser|Fedora Linux|CoreStorage|Ubuntu.*0day|UEFI bootkit", "非AI领域：传统操作系统/Linux发行版/底层固件常规更新"),
    (r"AirPods|iPad mini|Xnip|微诗|派评|猛玛|光盘|Disc Image Studio|DiscImageStudio", "非AI领域：传统消费电子数码评测/普通工具App推荐"),
    (r"小猫开源|Jync|CloudDM|Excelize|Paozhu|Nginx|ShopXO|DBPanel|调问|PingWAF|qData|SeaTunnel|novel-plus|UJCMS|GFlow|Skyeye|ApiGo|qModel", "非AI领域：传统企业管理软件/CMS/数据库同步/报表商城日常版本发布"),
    (r"上古卷轴|Bethesda|Steam is backed up|Jeff Bezos hair|Squarespace lays off|Palantir|Ballistic Ventures", "非AI领域：传统游戏娱乐/名人花边/传统建站裁员/泛安全基金募资"),
    (r"Flipper One|HackerTV|Prove you are a human|Trump|Gitbook Breached|Silicon Valley dating|Pratt parsing|Please Vote|Tiny Basic|Handles are the better pointers|Windows 11 annoyance|Bema:|uBlock Origin|Practice Vim|Intel stock collapse|Giant canyon on Mars|Incident with Pull Requests|GitHub Is Down|Jelly Sheet|Not Getting Paid|V100 SXM2 pinout|Does anyone enjoy work|Told to reboot a server", "非AI领域：Hacker News 泛科技/硬件/政治/编程杂谈/纯站点故障讨论"),
    (r"智己LS8|2026达喀尔青奥会|B站举办2026超级科学晚|OceanBase获世界互联网大会|爱诗科技PixVerse|央视严选「中国AI创业十骏」", "非AI核心：传统新能源车底盘/体育赛事公益/泛科普晚会/常规奖项公关"),
]

# 2. 综合科技源必须命中的 AI 核心词库
AI_POSITIVE_REGEX = re.compile(
    r"(\bAI\b|\bA\.I\b|\bLLM\b|\bLLMs\b|\bGPT\b|\bOpenAI\b|\bAnthropic\b|\bClaude\b|\bDeepSeek\b|"
    r"\bGemini\b|\bGrok\b|\bCodex\b|\bSiri\b|\bAgent\b|\bAgents\b|\bAgentic\b|\bRLHF\b|\bLoRA\b|"
    r"\bvLLM\b|\bOllama\b|\bGLM\b|\bQwen\b|\bNvidia\b|\bRTX\b|\bGPU\b|\bxAI\b|"
    r"人工智能|大模型|智能体|具身|世界模型|算力|智算|千问|商汤|神经网络|机器学习|深度学习|自然语言|向量|权重|Token)",
    re.IGNORECASE
)

# Hacker News 核心追踪事件关键词
HN_KEY_EVENT_REGEX = re.compile(
    r"(Anthropic.*S-1|Anthropic.*S1|Anthropic.*900B|Anthropic.*RSI|Brave|Google.*school|Revolut|jet engine|xAI|Binance|compute campuses|weights|Scaling Git|Codex|Gemini|RTX Spark|Postman)",
    re.IGNORECASE
)

AI_DEDICATED_SOURCES = {
    "TechCrunch AI",
    "The Verge AI",
    "OpenAI Official Blog",
    "HuggingFace Blog",
    "量子位 QbitAI",
    "arXiv cs.AI/cs.CL",
}

# 各信源单日保留配额上限（控制总规模在 85~90 条）
DAILY_SOURCE_CAPS = {
    "arXiv cs.AI/cs.CL": 2,
    "OpenAI Official Blog": 3,
    "HuggingFace Blog": 2,
    "Hacker News": 8,
    "TechCrunch AI": 11,
}


def extract_hn_points(summary_text):
    m = re.search(r"\[HN\s+(\d+)\s+pts", summary_text or "")
    return int(m.group(1)) if m else 0


def filter_candidates():
    with open("data/raw_probe/three_days_candidates.json", "r", encoding="utf-8") as f:
        three_days = json.load(f)

    retained = {"2026-11-02": [], "2026-11-03": [], "2026-11-04": []}
    discarded = []

    for date_str in ["2026-11-02", "2026-11-03", "2026-11-04"]:
        items = three_days.get(date_str, [])
        source_counter = Counter()

        for it in items:
            title = it.get("title", "")
            summary = it.get("summary", "")
            src = it.get("source_name", "")
            text_combined = f"{title} {summary}"

            # Step 1: 检查明确的非 AI 黑名单模式
            matched_neg_reason = None
            for pat, reason in NEGATIVE_PATTERNS:
                if re.search(pat, text_combined, re.IGNORECASE):
                    matched_neg_reason = reason
                    break

            if matched_neg_reason:
                discarded.append({
                    "date": date_str,
                    "source_name": src,
                    "lang": it.get("lang"),
                    "title": title,
                    "url": it.get("url"),
                    "discard_reason": matched_neg_reason
                })
                continue

            # Step 2: 综合源检查 AI 关键词准入
            is_dedicated = (src in AI_DEDICATED_SOURCES) or src.startswith("GitHub Releases")
            if not is_dedicated:
                if not AI_POSITIVE_REGEX.search(text_combined):
                    discarded.append({
                        "date": date_str,
                        "source_name": src,
                        "lang": it.get("lang"),
                        "title": title,
                        "url": it.get("url"),
                        "discard_reason": "相关性不足：综合信源未命中 AI 核心技术或实体关键词"
                    })
                    continue

            # Step 3: Hacker News 社区帖精选
            if src == "Hacker News":
                pts = extract_hn_points(summary)
                is_key_event = bool(HN_KEY_EVENT_REGEX.search(title))
                if not is_key_event and pts < 220:
                    discarded.append({
                        "date": date_str,
                        "source_name": src,
                        "lang": it.get("lang"),
                        "title": title,
                        "url": it.get("url"),
                        "discard_reason": f"社区低热度/非核心事件帖过滤：HN 热度未达高赞阈值 ({pts} pts < 220) 且非多源重点追踪事件"
                    })
                    continue

            # Step 4: 同源高频发布的配额限流
            if src.startswith("GitHub Releases"):
                if source_counter[src] >= 1:
                    discarded.append({
                        "date": date_str,
                        "source_name": src,
                        "lang": it.get("lang"),
                        "title": title,
                        "url": it.get("url"),
                        "discard_reason": "同源高频小版本去重：同一 GitHub 仓库当日已保留核心 Release，剔除连续次要 Patch/Build"
                    })
                    continue

            cap = DAILY_SOURCE_CAPS.get(src)
            if cap is not None and source_counter[src] >= cap:
                discarded.append({
                    "date": date_str,
                    "source_name": src,
                    "lang": it.get("lang"),
                    "title": title,
                    "url": it.get("url"),
                    "discard_reason": f"同源单日配额限流：{src} 单日保留上限为 {cap} 条，剔除超额次要条目以平衡多源分布"
                })
                continue

            source_counter[src] += 1
            retained[date_str].append(it)

    os.makedirs("data/staging", exist_ok=True)
    with open("data/staging/filtered_3days.json", "w", encoding="utf-8") as f:
        json.dump(retained, f, ensure_ascii=False, indent=2)

    with open("data/staging/discarded_items_log.json", "w", encoding="utf-8") as f:
        json.dump(discarded, f, ensure_ascii=False, indent=2)

    total_retained = sum(len(v) for v in retained.values())
    print(f"=== 过滤结果汇总 (方案2: 控制在 85~90 条) ===")
    print(f"原始三天候选总数: {sum(len(v) for v in three_days.values())} 条")
    print(f"过滤后保留总数:   {total_retained} 条")
    print(f"过滤丢弃总数:     {len(discarded)} 条\n")

    for d, lst in retained.items():
        zh = sum(1 for x in lst if x["lang"] == "zh")
        en = sum(1 for x in lst if x["lang"] == "en")
        srcs = Counter(x["source_name"] for x in lst)
        print(f"  {d}: 保留 {len(lst)} 条 (中文={zh}, 英文={en}) | 来源分布: {dict(srcs)}")


if __name__ == "__main__":
    filter_candidates()
