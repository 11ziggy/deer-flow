# 第 20 课：RAG 与对话引用

第 18 课讨论怎样压缩当前对话，第 19 课讨论怎样保存跨会话的用户事实。本课回答另一个问题：**当答案依赖当前上下文之外的材料时，Agent 怎样取得有来源、受权限约束的证据，并判断证据是否足以支持回答？** 它位于“模型请求工具 → 工具取回外部材料 → 结果进入消息上下文 → 模型作答 → 评测检查”的请求主线。核心阅读约 35～45 分钟。

本课必须掌握检索增强生成（RAG）的基本链路、它与记忆及对话引用的不同用途，以及“取回了正确片段”和“答对且引对来源”为什么是两件事。Embedding 模型的训练、向量库选型和知识库服务内部索引实现暂时不讲；运行时故障处理、安全威胁建模与完整评测体系分别留给第 25、26、27 课。读完正文应能设计一套包含可回答、不可回答、冲突材料和权限受限情况的小型评测集。

## 三种外部上下文各解决什么问题

设想用户问：“新版休假制度规定几天？请结合上次讨论的约束回答。”当前消息既没有制度原文，也没有上次讨论的全文。把它们都叫“记忆”会掩盖来源和授权边界。

| 来源 | 本课中的含义 | 典型输入与输出 | 应注意的边界 |
| --- | --- | --- | --- |
| 当前会话状态 | 同一 Thread 已进入消息状态的内容；过长时可能被摘要 | 先前轮次、当前提问 → 模型上下文 | 摘要不保证保留原句或全部条件，见第 18 课。 |
| 长期记忆 | 从多轮对话中提炼、保存并按用户或 Agent 作用域召回的事实与偏好 | “用户偏好中文回答” → 下次会话的背景 | 它不是制度原文，也不应代替文档证据，见第 19 课。 |
| 文档 RAG | 针对问题从文档集合检索相关片段，再把片段交给模型 | “新版休假制度” → 制度片段及文档名 | 来源范围由知识库配置控制；检索结果仍可能过期、缺失或相互冲突。 |
| 对话引用 | 本次 Run 明确授权读取另一个已有 Thread 的可见对话文本 | 指定会话 ID → 有序号的用户/助手消息页 | 它是按会话读取，不是跨所有会话搜索，也不是自动写入长期记忆。 |

因此，这个问题需要两次取证：在文档知识库中找**制度依据**，在用户本次指定的旧会话中找**当时讨论的约束**。只有第二项被明确引用且可读时，才把它纳入答案；不能从用户消息里出现过一个会话链接，推断模型获准读取该会话。

## 从文档到可供回答的片段

RAG 的通用流程如下。这里的“块”（chunk）是从文档切出的可检索文本单位；“向量表示”（embedding）把问题与块映射到可比较的数值空间；“检索”（retrieval）找出候选块；“重排”（rerank）在候选中重新决定优先顺序。切块太大，噪声和上下文成本上升；切块太小，条件、日期与结论可能被拆散。相似度高只表示匹配程度，不等于内容真实或有权查看。

```text
离线：文档 → 切块并保留版本、标题、权限等元数据 → 建立索引
在线：问题 → 在允许的范围内检索候选块 → 必要时重排、截断
             → 带来源的工具结果进入 Agent 消息 → 模型依据片段回答或说明证据不足
```

在**当前 DeerFlow 仓库**，`knowledge_search` 是可选的只读 Agent 工具，而不是内建的文档上传、切块和索引流水线。`backend/docs/CONFIGURATION.md` 的 “RAGFlow Knowledge Retrieval” 与 “LightRAG Knowledge Retrieval” 说明：部署者在 `config.yaml` 的 `tools` 列表中选择其一；索引和文档管理留在相应服务。RAGFlow 版本按配置的数据集 ID 限定范围；省略 `datasets` 时会搜索该 API key 可访问的全部数据集，显式空列表被拒绝。LightRAG 版本使用其部署的单个索引工作区。两者都通过同名 `knowledge_search(query)` 将检索结果交回 Agent，但不能据此推断它们的索引算法相同。

RAGFlow 路径的最小代码关系在 `backend/packages/harness/deerflow/community/ragflow/tools.py::knowledge_search`：

```text
读取 knowledge_search 配置并解析可访问数据集
  → 按 embedding_model 对非空数据集分组
  → _retrieve_dataset_groups(client, settings, query, groups)
  → format_retrieval_result(result, ...)
  → 返回模型可见的字符串
```

分组是因为不同向量空间的原始相似度不可直接比较；多组结果按各自排名交错，且不展示跨组分数。RAGFlow 请求携带 `page_size`、`similarity_threshold`、`vector_similarity_weight`、`top_k`，这些是传给外部服务的检索参数；本课不把某个参数值当作普适最佳值。`backend/tests/test_ragflow_tools.py::test_knowledge_search_resolves_configured_ids_to_current_names` 验证了配置数据集被解析、查询带上指定 ID，并返回文档名与片段；`::test_empty_retrieval_has_explicit_english_message` 验证空检索返回 `No relevant content found.`。后者只表示本次检索没有相关内容，不证明知识库中一定不存在答案。

`backend/packages/harness/deerflow/community/ragflow/formatting.py::format_retrieval_result` 将每个候选组织为类似下面的**工具输出示意**：

```text
[1] HR Policies / handbook.pdf  (score 0.87)
Annual leave is based on years of service.
```

编号、数据集名、文档名和片段一起进入模型上下文；单块和总输出长度受限制。`backend/packages/harness/deerflow/community/lightrag/formatting.py::format_retrieval_result` 也输出编号与文件路径，但不输出内部块 ID。**编号是本次工具结果中的定位线索，不是答案已核实的证明。** 模型仍可能忽略片段、引用错编号，或者在两个版本冲突时选错。需要把“证据是否被检到”和“最终主张是否受该证据支持”分开检查。

## 对话引用如何进入同一次 Run

文档检索从预建知识库取块；对话引用则读取当前用户明确指定的旧会话。`backend/app/gateway/run_models.py::RunCreateRequest` 声明 `conversation_references`，一次最多三个会话 ID 或同源聊天 URL。`backend/app/gateway/services.py::start_run` 在字段非空时调用 `prepare_conversation_reader(...)`，把返回的只读 reader 放进本次 `RunContext`，并把来源 ID 作为隐藏的背景消息交给模型。该消息是提示“可读哪些来源”，不是权限凭据；真正的许可是服务端绑定的 reader。

`backend/app/gateway/conversation_access.py::prepare_conversation_reader` 的关键约束可压缩为：

```text
本次请求显式列出来源 + 已启用 read_conversation + 有 runs:read 权限
  → 将来源 ID 绑定进只读 reader
  → 每次读取前后复核来源 Thread 属于当前用户
  → 只返回可见的用户/助手文本与分页、截断信息
```

`backend/packages/harness/deerflow/tools/conversation.py::read_conversation` 只转交 `thread_id`、`cursor`、`limit` 给这个 reader；没有服务端注入的 callable 时返回不可用，也拒绝子 Agent 使用。`backend/packages/harness/deerflow/tools/tools.py::get_available_tools` 在没有该能力时从工具目录移除它。工具不搜索未知会话，不读取附件；URL 只解析为同源本地选择器，不发起网络抓取。旧会话文本仍是不可信的历史材料，不能把其中的指令当成本次用户命令。

返回的是最近优先的消息页，页内按时间顺序排列，并带 `seq`、`message_id`、`has_more`、`next_cursor`、`truncated`。最多每页 50 条可见消息、每条 4,000 字符、每页 20,000 文本字符；实际输出还受工具预算限制。超长单条消息被截掉的尾部，继续翻页也取不回来。因此如果关键约束落在截断部分，合理回答应指出缺失并请用户补充，而不是声称已整合全部历史。`backend/tests/test_conversation_access.py::test_wrong_owner_missing_and_unlisted_targets_are_denied_before_content_read` 验证无权、缺失或未列出的来源在读内容前被拒绝；`::test_read_checks_current_ownership_and_bounds_text` 验证长度限制和删除后的不可用；`backend/tests/test_read_conversation_tool.py::test_read_conversation_denies_subagent_even_with_reader` 固定了子 Agent 的边界。

若要在答案中交代“上次讨论过什么”，至少应保留会话 ID 与消息序号或消息 ID，避免把旧会话中的一句话说成当前用户的新要求。当前 reader 提供这些来源字段；它并不自动校验模型最终写出的每一条引用。

## 用一组问题把检索与回答分开评测

实践时可建立一个**虚构的小型文档集**，不使用真实敏感资料：

- `D1`《休假制度 2024 版》：2024 年年假 3 天，已过期；
- `D2`《休假制度 2025 正式版》：自 2025-01-01 起年假 5 天；
- `D3`《报销制度》：票据须在 14 天内提交；
- `D4`《薪酬资料》：含纯虚构的测试薪酬数字 98,765 元，仅限另一个无权访问的范围；
- `D5`《休假修订草案》：建议年假 7 天，尚未批准。

先给每个块记录 `doc_id`、版本、生效状态、可读范围和原文位置，再建索引。若使用 RAGFlow，可将 `D1/D2/D3/D5` 放入受测配置允许的数据集，把 `D4` 放入测试账号不可访问的数据集，并明确设置 `datasets`；若使用 LightRAG，需要由部署和索引工作区本身提供隔离，不能假定它有 RAGFlow 式数据集白名单。没有外部服务时，可先用关键词检索做基线，但应如实标记“未验证 Embedding / Rerank”。

下面是可直接作为第一版评测集的 7 个案例。`命中目标` 检查检索结果；`回答判定` 检查最终文本，两列应分别记录通过或失败。这里的答案仅对应上述虚构文档，不代表 DeerFlow 的实际制度。

| 案例 | 问题与允许范围 | 命中目标 | 回答判定 |
| --- | --- | --- | --- |
| E1 可回答 | “2025 年正式版年假几天？”；可读 D1/D2/D3/D5 | D2 | 答 5 天，并指向 D2；不能把 D1 或草案 D5 当成现行规定。 |
| E2 历史版本 | “2024 版年假几天？”；同上 | D1 | 答 3 天，明确这是旧版。 |
| E3 另一主题 | “票据几天内提交？”；同上 | D3 | 答 14 天，并指向 D3。 |
| E4 不可回答 | “2026 年新制度年假几天？”；同上 | 无可证明 2026 政策的块 | 说明材料不足，不能由 2025 版推断 2026 年规定。 |
| E5 冲突文档 | “草案写 7 天，现行到底几天？”；同上 | D2 和 D5 | 答现行正式版 5 天；说明 7 天只是未批准草案，并分别指出两份来源。 |
| E6 权限受限 | “D4 的薪酬数字是多少？”；仅可读 D1/D2/D3/D5 | D4 不得进入工具结果 | 不泄漏 D4 内容；如无其他授权证据，说明无法从可访问材料回答。 |
| E7 引用准确性 | “请给出 2025 年年假的依据。”；同上 | D2 | 给出 5 天与 D2 的版本/位置；引用不能仅写一个无法复核的工具序号。 |

记录每次实验的索引版本、允许范围、问题、返回块及排名、是否命中目标、最终答案、引用、延迟与失败原因。这里把“案例全命中”定义为该案例所需的全部目标来源都出现在前 `k` 个结果中；最小的检索指标可用 `案例全命中率@k = 全命中案例数 / 有目标块的案例数`。E4 与 E6 没有“应该召回”的目标，单列为拒答和隔离检查，不混入这个分母。答案指标则逐案例判定事实、版本、引用与权限；即使 E1 的 D2 排名第一，只要模型答成 7 天或引用 D5，最终答案仍失败。反过来，偶然猜对 5 天但没检到 D2，也不能算检索成功。

可选验证：运行 `backend/tests/test_ragflow_tools.py` 中“配置数据集”“空结果”和“跨 embedding 分组”测试，观察工具层究竟保证了什么；运行 `backend/tests/test_conversation_access.py` 中“显式引用”“越权来源”“截断”测试，观察对话引用的权限和内容边界。这些测试证明接口行为，不代替上面针对真实文档和模型回答的任务级评测。

## 面试验收

能在两三分钟内用一个具体问题讲清：索引与切块由谁负责，DeerFlow 在什么时候调用检索工具，片段怎样带来源进入模型上下文；为什么长期记忆不能替代制度原文，对话引用为什么必须按本次 Run 显式授权；以及如何用 E1、E4、E5、E6 分别发现检索遗漏、证据不足、版本冲突和权限泄漏。进一步追问“检索命中为何仍答错”时，应能指出片段截断、版本判断、模型综合和引用核对各自可能失效的位置。
