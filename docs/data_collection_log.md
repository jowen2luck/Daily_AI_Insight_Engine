# 数据采集记录

## 1. 数据来源与取舍

### 已采用信源（共 12 家平台，99 条有效数据）
* **科技媒体（中/英）**：TechCrunch AI、The Verge AI、量子位 QbitAI、极客公园、Solidot、OSChina 开源中国、少数派 SSPai（共 63 条）。
* **官方渠道**：OpenAI Official Blog、HuggingFace Blog、arXiv (`cs.AI/cs.CL`)、GitHub Releases（`vllm`、`ollama`、`llama.cpp`、`langchain`、`open-webui`）（共 16 条）。
* **聚合与社区平台**：Hacker News（共 20 条，含外部报道聚合与社区原生讨论）。

### 放弃的信源及原因
* **VentureBeat AI**：请求返回 `HTTP 429` 限流拦截，无法稳定复现。
* **机器之心 / 36氪 RSS**：RSS 返回的 XML 格式存在非法未转义字符且更新停滞。
* **付费墙媒体（如 WSJ / The Information）**：遵守题目合规要求，不使用付费墙内容。

---

## 2. 采集方式与正文扩充

通过三个独立脚本按序完成，最终生成 `data/raw/` 下的三个批次文件（`2026-11-02` 30条、`2026-11-03` 50条、`2026-11-04` 19条）：

1. **元数据抓取（`scripts/1_crawl_raw.py`）**：解析各信源的 RSS/Atom 订阅流，并调用 Hacker News Algolia API、GitHub Releases API、arXiv API 获取标题、链接、发布时间与初始摘要（三天共抓取 258 条候选）。
2. **规则初筛（`scripts/2_filter_candidates.py`）**：对 258 条候选执行确定性过滤，保留 99 条，丢弃 159 条（丢弃明细存至 `data/staging/discarded_items_log.json`）。
3. **正文与评论扩充（`scripts/3_enrich_content.py`）**：根据原文链接并发抓取网页 `<p>` 正文段落及 `<meta>` 描述；针对 Hacker News 额外调用 Item API 补充 Top 3 高赞评论，解决初始摘要过短的问题。

---

## 3. 筛选规则

1. **时间范围**：仅保留发布日期在 `2026-11-02` 至 `2026-11-04` 三天内的资讯。
2. **AI 相关性**：纯 AI 垂直源直接保留；综合科技源（Solidot、OSChina、少数派、HN 等）必须命中 AI 关键词（如 `AI`、`LLM`、`GPT`、`Claude`、`大模型`、`智能体`、`具身`、`算力` 等），且排除明确的非 AI 领域内容。
3. **同源限流与热度门槛**：
   * 同一 GitHub 仓库单日连续发布的多个小版本，仅保留 1 个核心 Release；
   * 单日数量过多的学术源（arXiv）和常规企业案例限制单日采样数（2~3 条）；
   * Hacker News 仅保留高热度（`Points >= 220`）或命中重点追踪事件的帖子。

---

## 4. 丢弃数据与典型案例（共丢弃 159 条）

| 丢弃原因 | 数量 | 典型丢弃案例 |
| :--- | :---: | :--- |
| **社区低热度 / 泛聊水帖** | 49 条 | • `[Hacker News]` *Ask HN: What hardware are you using to run local LLMs?* (50 pts)<br>• `[Hacker News]` *Show HN: Remene – Understand your MRI* (11 pts) |
| **同源单日超额限流（arXiv 论文等）** | 33 条 | • `[arXiv]` *A simple adaptive accelerated optimistic dual averaging method...*（单日 25 篇纯数学/算法论文，仅采样保留 2 篇） |
| **非 AI 领域的科技/政治/硬件杂谈** | 25 条 | • `[Hacker News]` *Ubuntu still has not released patch for Linux 0day RCE*<br>• `[Hacker News]` *Giant canyon on Mars is still tectonically active* |
| **传统软件 / 数据库日常版本更新** | 18 条 | • `[OSChina]` *ShopXO 免费开源商城系统 v6.9.3 版本已发布*<br>• `[OSChina]` *Excelize 开源基础库发布 2.11.1 版本更新* |
| **自然科学 / 生物医学 / 航天新闻** | 9 条 | • `[Solidot]` *鳄鱼为何免受毁灭恐龙的小行星的影响？*<br>• `[Solidot]` *小鼠研究显示阉割有助于长寿* |
| **传统数码评测与游戏娱乐** | 17 条 | • `[少数派]` *Apple AirPods 5 半入耳降噪真无线耳机体验*<br>• `[Solidot]` *Bethesda 宣布《上古卷轴VI：哨兵》* |
| **GitHub 同日连续小版本冗余** | 8 条 | • `[GitHub]` `ggml-org/llama.cpp Release b12109 / b12102 / b12101 / b12100`（同日连发 5 个构建版本，仅保留最新 `b12110`） |
