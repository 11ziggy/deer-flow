# 第 19 课：长期记忆

第 18 课讨论的是把**同一 Thread** 的旧消息压缩成摘要。本课回答另一个问题：**一段对话结束后，哪些关于用户的信息可以跨 Thread 保留，下一次对话又怎样取回并纠正它？** 它位于“图执行后提交记忆更新 → 持久化 → 未来 Run 组装模型上下文”这一段请求主线。核心阅读约 35～45 分钟。

前置知识是第 7、8 课的 Thread 与状态、第 10 课的中间件时点、第 18 课的上下文压缩。本课必须掌握对话状态与长期记忆的边界、默认 DeerMem 的写入门禁与作用域、两种召回模式，以及如何分别验证“存对、取到、答对”。第 20 课再讨论面向外部文档的检索增强生成（RAG）；这里不展开文档切块和向量检索。

## 从一条偏好看两种状态

假设用户在 Thread A 说“我长期偏好简短的中文答复”，助手完成回复；几天后用户在 Thread B 问一个新问题。Thread A 的 `messages` 和 `summary_text` 属于对话状态：Checkpoint 让**这个 Thread** 能继续或恢复，但新 Thread 不会自动继承它们。长期记忆则把经筛选的用户信息放在独立存储中，供未来 Thread 读取。`backend/packages/harness/deerflow/agents/thread_state.py::ThreadState` 包含 `summary_text`，不包含 DeerMem 的事实列表；记忆接口在 `backend/packages/harness/deerflow/agents/memory/manager.py::MemoryManager` 中单独定义 `add()`、`get_context()` 和可选的事实管理方法。这是两类状态的代码边界。

默认路径可以压缩成下面的时序。箭头只表示数据流，不表示每一步都与本轮回答同步完成。

```text
Thread A：用户消息 + 最终助手消息
  → MemoryMiddleware.after_agent / aafter_agent
  → DeerMem.add：过滤消息、识别信号、加入延迟处理队列
  → MemoryUpdater：调用记忆模型提出摘要、事实和删除建议
  → 确定性门禁与 DeerMem 持久化

Thread B：新 Run 开始
  → DynamicContextMiddleware.before_agent / abefore_agent
  → MemoryManager.get_context(当前用户、当前 Agent)
  → 受预算限制的记忆块进入模型输入
  → 模型结合当前请求生成回答
```

`MemoryMiddleware._resolve_add_args` 在功能开启、拿到 `thread_id` 且状态含消息时，提取当前 `user_id`；`after_agent` 把消息、Thread、Agent 和用户身份交给 `get_memory_manager().add(...)`，异步钩子走 `aadd(...)`。DeerMem 的 `add()` 再调用 `_prepare_update()`，只保留用户消息和最终助手回复，要求两者都存在，并滤去无意义的纯确认。之后 `MemoryUpdateQueue.add()` 按 `(thread_id, user_id, agent_name)` 合并待处理项并延迟执行；`MemoryUpdater._do_update_memory_sync_impl()` 才调用记忆模型。**本轮回答结束或入队成功，都不能证明事实已写入。**队列拥堵、模型未配置、模型输出无效或存储失败都可能让本次更新缺席；实验必须等队列处理完，再读取存储结果。`config.example.yaml` 中的 `memory.backend_config.model` 未填写，照搬示例配置不能验证自动抽取。第 18 课的压缩前钩子另有 `add_nowait` 路径，用于在旧消息移除前提交待提取内容，不改变上述“提交不等于完成”的判断。

## 写入门禁：模型建议不是持久事实

记忆模型看到的是经过过滤的对话和当前记忆，它提出 `newFacts`、用户/历史摘要、强化或删除建议。`backend/packages/harness/deerflow/agents/memory/backends/deermem/deermem/core/prompts/memory_update.chat.yaml` 要求每条新事实标注 `scope`（作用范围）、`durability`（持续性）和 `authority`（是否带行动授权）。随后 `deermem/core/updater.py::_fact_scope_gate_reason` 用确定性条件检查。以下摘自该函数：

```python
if any(_normalize_gate_label(fact.get(field)) is None for field in _FACT_CLASSIFICATION_FIELDS):
    return "missing"
if _normalize_gate_label(fact.get("scope")) != "user":
    return "scope"
if _normalize_gate_label(fact.get("durability")) != "durable":
    return "durability"
if _normalize_gate_label(fact.get("authority")) != "descriptive":
    return "authority"
```

`_normalize_gate_label` 会标准化模型给出的标签；三项标签缺失即拒绝，只有**用户范围、持久、描述性**的事实可进入自动写入后续步骤。摘要另需用户范围和描述性标签，暂时性任务要求、项目规则、当前文件状态以及“以后可以替我发布”之类行动许可，不应被提升为跨会话记忆。通过作用域门禁的事实还要满足置信度阈值（`DeerMemConfig.fact_confidence_threshold` 默认 `0.7`）和容量上限（`max_facts` 默认 `100`）。`backend/tests/test_memory_scope_gate.py::test_fact_gate_accepts_only_durable_descriptive_user_facts` 用五个候选事实验证只有符合三标签者留下；`::test_summary_gate_requires_user_scope_and_descriptive_authority` 验证摘要同样会拒绝项目内容与行动授权。

去重有两层，不应笼统说“系统会识别所有同义表达”。`MemoryUpdater._apply_updates` 会跳过规范化内容完全相同的事实；近义去重由 `fact_dedup_enabled` 控制，**默认关闭**。开启时，新的同类别候选事实达到配置的词元相似度阈值才合并到旧事实，保留旧 ID 与正文；纠错所需的替代事实不走这条合并路径。`backend/tests/test_memory_fact_dedup.py::TestFactDedupGate` 验证近义合并与类别边界，`::TestFactDedupConfig` 验证默认值。重复出现也不自动等于“用户再次确认”：只有启用混合容量策略或其影子评估，且模型指向旧事实、最近过滤消息中检测到用户确认信号时，代码才更新该事实的确认元数据；仅因旧事实被注入或再次抽取，不会增加确认依据。

纠错需要看**旧事实是否真正失效**。用户若说“这次报告写详细一点”，这是当前任务例外，不应删掉“长期偏好简短答复”；若说“我现在长期偏好详细答复”，才可能提出用户范围的旧事实删除与新事实替换。`MemoryUpdater._apply_updates` 对删除建议要求 `scope="user"` 和非空 `reason`；若带 `replacementFactIndex`，只有对应新事实通过作用域、置信度与容量检查并确实留在结果中，旧事实才会被删。`test_memory_scope_gate.py::test_thread_scoped_removal_cannot_delete_user_fact`、`::test_paired_removal_is_skipped_when_replacement_fails_scope_gate` 和 `::test_paired_removal_is_atomic_when_replacement_is_persisted` 固定了这些边界。

这些门禁只验证**模型给出的标签和结构**，不会神奇地证明标签本身正确。模型可能把一次性偏好误标为持久偏好，也可能漏掉明确纠正；记忆还可能来自错误的助手回复，之后再影响新回答，形成错误强化。当前实现用提示规则、消息过滤、作用域门禁和纠错约束降低风险，正确性仍需用跨会话案例检查。记忆正文也属于用户可影响的数据；`DynamicContextMiddleware._make_reminder_and_user_messages` 把日期放在框架控制的 `SystemMessage`，把记忆块放在隐藏的 `HumanMessage`，避免赋予记忆系统指令的权限。

## 用户范围、Agent 范围与召回

DeerMem 的默认本地存储把用户摘要和 Agent 事实分开：`{base_dir}/users/{user_id}/memory.json` 保存该用户共享的摘要；`{base_dir}/users/{user_id}/agents/{agent_name}/facts/.../*.md` 每文件保存一条事实。未指定 Agent 时使用保留桶 `__default__`。`FileMemoryStorage` 是持久事实的来源，FTS5 检索索引可以重建。`MemoryMiddleware` 在入队时就固定 `user_id`，避免延迟处理线程丢失请求身份；`backend/tests/test_memory_queue_user_isolation.py::test_queue_keeps_updates_for_different_users_in_same_thread_and_agent` 验证同名 Thread/Agent 的不同用户不会合并入同一队列项。**用户级**回答“归谁所有”，**Agent 级**回答“哪个 Agent 的事实桶可见”；共享摘要与专属事实不能混作一份全局事实库。

默认 `memory.mode: middleware` 时，`DeerMem.get_context()` 读取当前用户的共享摘要和所选 Agent 的事实，由 `format_memory_for_injection()` 按置信度、类别与 Token 预算选择内容；默认纠错类别有单独的保留预算。`DynamicContextMiddleware` 在新 Thread 首次注入完整记忆块，此块会留在该 Thread 的消息状态中；后续轮次不会因后台刚写入新事实而自动刷新它。因此检查新记忆的召回要开**新 Thread**，不能只在原 Thread 再问一次。已持久化也不等于一定进入提示：预算、关闭注入、读取失败或所选 Agent 的事实桶不同，都可能改变输入；进入提示也不保证模型最终答对。

可选的 `memory.mode: tool` 把 `memory_search`、`memory_add`、`memory_update`、`memory_delete` 注册给模型。此时 DeerMem 自动注入共享摘要，事实留给 `memory_search` 按查询检索；显式增删改经 `MemoryManager` 的事实方法执行，**不经过上述自动抽取的三标签门禁**，因此工具权限和模型调用决策要单独评估。`backend/packages/harness/deerflow/agents/memory/tools.py::_resolve_scope` 会把运行时用户与 Agent 传给工具。默认模式的理解不依赖工具模式的具体检索排序；基于文档的 RAG 留到下一课。

## 课程产出：五组跨会话记忆正确性案例

以下案例使用虚构用户和事实，每组在独立临时存储中运行。至少让 Thread A 完成一轮“用户输入→最终助手回复”，**等待记忆队列处理完成**后，才在无继承 Checkpoint 的新 Thread B 检查记忆；需要纠正时再开新 Thread。记录三层结果：① `manager.get_memory(user_id=..., agent_name=...)` 的持久事实/摘要，②新 Thread 实际注入的记忆块或工具检索结果，③最终回答。对 ① 使用固定记忆模型输出可做确定性单元测试；对 ③ 使用真实模型时应记录配置、输出和失败，不把某一次自然语言回答当作门禁已正确执行的证明。

| 组别与多会话输入 | 持久化与召回验收 | 主要失效信号 |
| --- | --- | --- |
| **1. 保存并召回**：A 明确说“以后默认用简短中文回答”；B 问一个与 A 无关的新问题。 | A 的用户/Agent 桶有一条持久偏好；B 的首次记忆块含它，回答使用中文且简短。 | 只在 A 的 Checkpoint 看见原话，存储为空；或存了却没有进入 B 的输入。 |
| **2. 一次性例外不污染**：先完成第 1 组；新的 A 说“这篇报告请写详细些，并替我发布”（不实际执行发布）；B 再问普通问题。 | 旧的长期偏好仍在；不新增“总要详细写”或“可代用户发布”的事实/摘要，也不删除旧偏好。 | 当前任务要求或行动许可被写成永久偏好；B 因此偏离原有偏好。 |
| **3. 重复与确认**：A 明确给出偏好；另一 Thread 用相同表述重复一次，再用近义表述重复一次；B 查询。 | 完全相同内容只有一条；开启近义去重时，同类别近义候选也只保留一条。关闭该选项时不能以近义去重作为通过条件；旧事实仅被召回不算用户确认。 | 事实数无控制地增加，或系统把自身注入内容当作用户再次确认。 |
| **4. 明确纠正**：A 先说“长期偏好简短中文”；另一个 Thread 明确改为“以后默认详细中文回答”；B 再问新问题。 | 合格替代事实写入后，旧偏好消失；B 只依据新偏好回答。再用一份标签缺失或低置信度的替代建议做负例，旧事实应保留。 | 新旧矛盾事实共存；或替代事实未落盘就删掉旧事实。 |
| **5. 用户与 Agent 隔离**：用户 U1 在 Agent X 的 A 建立偏好；U2 使用 Agent X、U1 使用 Agent Y 分别新开 B；最后 U1 在 X 新开 C。 | U2/X 看不到 U1 内容；U1/Y 看不到 X 的**事实**，但可读取 U1 共享摘要；U1/X 的 C 能召回原事实。 | 跨用户泄漏、把 Agent X 的事实当作所有 Agent 的事实，或同用户新 Thread 无法召回。 |

做确定性测试时，固定记忆模型对这些输入提出的 `newFacts`、摘要和 `factsToRemove`，尤其为第 4 组提供 `replacementFactIndex`；每组结束后可调用 `DeerMem.shutdown_flush(timeout)` 等待待处理任务和正在执行的更新，再检查返回值及持久内容。队列排空本身不代表抽取成功。真实模型实验则另记抽取成功率与回答正确率，避免用假模型测试冒充完整端到端效果。`backend/tests/test_memory_scope_gate.py`、`test_memory_fact_dedup.py`、`test_memory_queue_user_isolation.py` 和 `test_memory_prompt_injection.py` 给出了可复核的局部断言；上表把它们组合为本课的**测试集设计**，不是声称这些跨会话实测已全部通过。

**可选代码深挖**：先读 `memory_middleware.py::MemoryMiddleware.after_agent` 与 `deer_mem.py::DeerMem.add`，确认“入队与落盘”的时间差；再读 `updater.py::_fact_scope_gate_reason`、`MemoryUpdater._apply_updates`，确认拒绝与纠错条件；最后读 `dynamic_context_middleware.py::DynamicContextMiddleware._inject`，确认新 Thread 注入和原 Thread 复用的差异。正文已经给出结论，打开源码的收益是练习在改配置或换记忆后端时重新核对这些边界。

面试验收：能用第 1、2、4 组案例解释“对话状态为何不能替代长期记忆”“为什么记忆模型建议不能直接落盘”“纠错何时允许删旧事实”；能说明用户身份、Agent 桶和共享摘要各约束什么；能把存储正确、召回正确、回答正确拆开检查，并指出默认模式的后台延迟与 Thread 内冻结记忆这两个实验陷阱。
