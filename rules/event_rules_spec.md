# 事件分类体系与跨批次归并演进规则说明

本目录定义了 AI 舆情分析日报系统的核心规则配置（机器可读配置见 [`event_taxonomy_and_merge_rules.json`](file:///d:/Pcode/outside/data_vis/rules/event_taxonomy_and_merge_rules.json)）。

---

## 一、六大事件分类体系 (`event_categories`)

所有抽取的 AI 原子事件必须严格归入以下 6 个枚举类别之一（由自动化校验器强校验）：

| 类别 ID | 标准类别名称 | 覆盖范围 |
| :--- | :--- | :--- |
| `CAT-01` | **产品与模型发布 (Product & Model Launch)** | 大模型、Agent、OS 级 AI 功能（如 Apple Intelligence / Siri）、AI 搜索及核心应用发布、测试或停服 |
| `CAT-02` | **投融资与IPO财报 (Corporate & Financial/IPO)** | IPO 招股书披露（如 S-1 文件）、季度财报、风险融资、重大并购重组及高管变动 |
| `CAT-03` | **算力基建与能源 (Infrastructure & Energy)** | 数据中心建设、GPU 集群、电力供应瓶颈（喷气涡轮发电、电网保护计划）、光互连及算力采购承诺 |
| `CAT-04` | **AI安全、合规与版权诉讼 (Safety, Regulation & Legal)** | 前沿模型安全风险（RSI 阈值、RSP 政策）、版权诉讼、反爬虫退出标准（robots.txt）、AI 诈骗与监管 |
| `CAT-05` | **开源生态与技术突破 (Open-Source & Research)** | 开源权重模型、形式化验证/模型身份证明工具、学术 Benchmark、世界模型与研究论文 |
| `CAT-06` | **行业应用与商业化冲击 (Industry Adoption & Market Impact)** | 企业自托管降本实证、AI 搜索对传统网站流量冲击、AI 自动化裁员与商业化落地 |

---

## 二、六类数据质量诊断标签 (`quality_flags`)

在 Stage 1 结构化抽取阶段，系统对每篇资讯同步执行质量审计：
1. `EXAGGERATED_TITLE`：标题夸大、制造恐慌或隐去核心主语的标题党（提取时剥离情绪修辞，还原客观事实）。
2. `PR_PROMOTION`：商业公关稿或授权转载软文（提取时降权标记）。
3. `EVENT_AD`：纯线下展会/峰会售票广告（直接拦截，不生成行业事件实体）。
4. `STALE_NEWS`：旧闻新发（正文中核心事件实际发生日期 `event_time` 早于发布日期 `published_at` 超过 3 天）。
5. `UNVERIFIED_RUMOR`：未证实传闻或正文自我证伪（在事件摘要与告警中显式标注）。
6. `MULTI_TOPIC_DIGEST`：多事件早报合集（按【篇章结构权重 + AI 核心度】拆解为独立核心 AI 事件，过滤非 AI 汽车/游戏/配件及文末一句话数码边缘快讯）。

---

## 三、事件五元组与跨批次归并规则 (`merge_and_evolution_rules`)

### 1. 事件五元组结构
每个事件实体维护以下核心属性：
- **`actors`（主体）**：经 `entity_aliases` 映射标准化的企业、机构、人物或模型列表。
- **`action`（客观源头动作）**：现实世界中发生的客观源头动作（区分于媒体的报道切入角度）。
- **`target_objects`（作用标的）**：动作直接作用的标的物、产品、文件或技术概念列表。
- **`event_time`（时间锚点）**：维护 `initial_event_time`（首发日）与 `latest_event_time`（最新进展日）。
- **`location`（地点）**：事件发生国家或地区。
- **`metrics_ledger`（数据口径台账）**：按批次与来源记录全部量化指标及其统计口径说明（`scope_note`），并在发生口径差异时生成 `metric_resolution_note`。

### 2. 跨批次归并四大准则與演进状态机
- **准则 1（客观源头动作聚合 —— 防漏合）**：当两条报道指向同一主体在同一时间窗口内发生的同一个客观源头动作（如递交同一份 S-1 招股书）时，即使不同媒体分别聚焦其中的安全条款、财务营收或内部算力成本，均归并为同一事件实体。
- **准则 2（同主体异动作隔离 —— 防误合）**：即使主体完全相同（如均为 Anthropic 或 Apple），只要客观源头动作与作用标的属于不同独立事件（如“提交 S-1 招股书” vs “开源 FlashProve 模型身份验证系统”），严禁合并，必须新建独立事件。
- **准则 3（上下文感知抽取）**：Stage 1 阅读原文时注入当前活跃事件实体表的极简五元组索引，确保正文中的隐蔽背景关联与跨语种概念对应（如 `AI算力园区` $\leftrightarrow$ `compute campuses`）在抽取阶段即被捕获。
- **准则 4（四态演进状态机）**：
  - `NEW`：当前批次首次出现的新事件；
  - `CORROBORATED`：多源/跨语种交叉印证或同一事件不同侧面补充；
  - `ONGOING`：跨批次出现事态新进展、新回应或正式落地；
  - `CORRECTED`：新批次报道提供更精确的官方数据修正早期粗略约数（如 `8954亿美元` 修正 `$900B/$895B`），或对早期传闻进行澄清。
