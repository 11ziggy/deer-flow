# 第 16 课：Sub-agent 架构

一个复杂请求可以由 Lead Agent（主 Agent）直接完成，也可以把有明确边界的工作交给 Sub-agent（子 Agent）。本课的核心问题是：**什么时候委派值得做，DeerFlow 又怎样让多个子任务保持各自的上下文和执行身份？** 它位于请求主线的“模型产生工具调用 → Runtime 执行工具 → 状态和事件返回 Lead Agent”一段。核心阅读约 35～45 分钟；实验可另用约 60 分钟。

前置知识是第 5 课的 Agent Loop、第 6 课的请求链、第 9 课的工具组装，以及第 15 课大纲中的专用 Agent 能力边界。读完应能解释委派判断、`task` 调用链、上下文边界、并行条件和两种 ID 的用途。规划与用户确认留到第 17 课；摘要、恢复和运行时故障处理分别留到第 18、23、25 课。本课只讨论普通 `task` 委派，不展开持久批任务 `batch_task`。

## 先判断是否值得委派

委派会启动另一套 Agent 执行：它需要自己的提示、模型调用、工具发现和结果汇总。因此“任务很复杂”本身不是理由。`backend/packages/harness/deerflow/agents/lead_agent/prompt.py` 中的委派路由提示，要求 Lead Agent 先比较直接完成的路径，再衡量三种收益：**并行缩短耗时、专用能力、隔离大量中间上下文**；相应成本包括重复阅读资料、启动与协调、验证结果，以及共享状态冲突。`backend/packages/harness/deerflow/tools/builtins/task_tool.py::task_tool` 的工具描述也表达了同一原则。

以“分别核对两个互不依赖的模块并汇总”为例：子任务 A 只读 Registry 的定义与测试，子任务 B 只读上下文快照的定义与测试，各自交付“输入、输出、代码证据”三项，Lead Agent 最后合并。这两个任务既没有输出依赖，也不修改相同文件，可以尝试并行。若 B 必须先拿到 A 的结论，或两者都要改同一文件，就不该同时派出。并行节省的主要是墙钟时间，不保证 Token 更少；每个子 Agent 都会消耗自己的输入和输出 Token。

这里的“独立”是执行上的约束：每个子任务要有清晰范围、足够的已知背景、预期输出和副作用归属。即使两项工作都只读，也要考虑它们是否重复搜索同一批资料，导致委派成本高于收益。Lead Agent 仍负责核对关键结论并形成最终答案，子 Agent 的完成状态不等于结论正确。

## 一次普通委派怎样经过系统

下面是当前 Gateway 主路径中，与本课有关的最短链路。箭头表示一次委派的先后关系；并行时，多个 `task` 调用各自走一遍工具与执行器流程。

```text
Lead Agent 模型响应中产生 task 工具调用
  → SubagentLimitMiddleware 限制本轮和本次 Run 的委派数量
  → task_tool 校验类型及调用者允许的子 Agent
  → Registry 解析 SubagentConfig
  → SubagentExecutor 新建子 Agent 的消息状态并提交后台执行
  → task_tool 轮询后台结果，发出 task_* 进度事件
  → ToolMessage 把结果交还 Lead Agent
  → Lead Agent 验证、综合并回答
```

入口在 `backend/packages/harness/deerflow/agents/lead_agent/agent.py::_assemble_lead_agent`：只有运行选项打开 `subagent_enabled` 且专用 Agent 的 `allowed_subagents` 不是空名单时，Lead Agent 才获得委派能力。`allowed_subagents=None` 表示不按调用者名单限制；`[]` 是明确禁止。即便提示中出现了某种子 Agent，`task_tool` 仍会从本次运行元数据读取名单，用 `get_available_subagent_names()` 和 `get_subagent_config()` 再校验一次。这个执行时检查让能力边界不只依赖模型遵守提示。Registry 位于 `backend/packages/harness/deerflow/subagents/registry.py`，从内置类型、配置定义和已启用的管理定义解析子 Agent，再应用对应覆盖；内置类型包括 `general-purpose` 和 `bash`，其中 `bash` 还受 Sandbox/宿主命令策略限制。

执行的关键两行在 `task_tool.py::task_tool`：

```python
executor = SubagentExecutor(**executor_kwargs)
execution_id = executor.execute_async(prompt, task_id=tool_call_id)
```

`SubagentExecutor` 位于 `backend/packages/harness/deerflow/subagents/executor.py`。它根据子 Agent 配置筛选工具、选择模型并建立自己的图；普通 `task` 不再向子 Agent 提供 `task` 工具，避免递归委派。`execute_async()` 把协程提交到持久的隔离事件循环，并返回后台执行 ID；实际执行先获取进程内的容量槽，再运行子 Agent 图。`task_tool` 用这个 ID 查询结果，向前端发 `task_started`、`task_running`、`task_completed` 等自定义事件，并最终通过 `Command(update={"messages": [ToolMessage(...)]})` 把结果送回 Lead Agent。`backend/tests/test_task_tool_core_logic.py` 中的工具调用测试同时固定了“用后台 ID 轮询、用工具调用 ID 发事件和写 ToolMessage”的关系。

## 隔离的是对话工作区，不是所有资源

默认 `context_mode="isolated"`。`SubagentExecutor._build_initial_state()` 为子 Agent 新建消息列表：它自己的系统提示，加上当前委派任务的 `HumanMessage`。父 Agent 的完整消息历史不会自动成为子 Agent 的对话。Lead Agent 因而必须在 `prompt` 中写出子任务真正需要的约束和交付格式。

如果约束散落在父会话中，调用者可显式选 `context_mode="snapshot"`。`backend/packages/harness/deerflow/subagents/context_snapshot.py::ParentContextSnapshot.from_state` 在派发前截取已保留的用户、AI、工具消息及摘要，过滤父系统消息、隐藏框架消息和未配对的工具调用，再把历史作为一条标明“父会话背景”的 `HumanMessage` 放到当前任务之前。它不会随父会话后续变化同步，也不会把父 Agent 的历史工具行为当成子 Agent 已完成的工作。测试 `backend/tests/test_subagent_context_snapshot.py::test_snapshot_keeps_history_and_summary_but_excludes_parent_authority_and_metadata` 固定了这条边界。选择快照意味着多传历史，可能增加输入 Token。

隔离上下文不等于隔离资源。执行器仍传递子任务需要的线程、用户身份、Sandbox 状态和当前上传文件状态；上传文件列表会深拷贝给子状态。两个子 Agent 可能触及同一 Workspace 或外部系统，因此共享可变文件、数据库记录或外部副作用必须由任务划分避开。子 Agent 的授权归属也沿用父运行身份，不能把“新消息列表”理解为新的权限主体。

## 两种 ID、两层并行约束

`task_tool` 把模型供应商给出的 `tool_call_id` 传入 `execute_async(..., task_id=tool_call_id)`，但执行器会生成完整 UUID 作为 `execution_id`。在 `SubagentResult` 中，字段 `task_id` 存的是这个服务端执行 ID，`external_task_id` 才存外部工具调用 ID。命名略容易误读，按用途记更稳妥：

| 标识 | 来源与用途 |
| --- | --- |
| 外部 `task_id`，即 `tool_call_id` | 关联 `ToolMessage`、`task_*` 事件和前端任务卡片，属于本次模型工具调用。 |
| `execution_id` | 后台 Registry 的键，用于轮询、取消与清理一次执行；在 `SubagentResult.task_id` 中保存。 |

供应商的工具调用 ID 可能在不同父 Run 中重复，不能用它做进程级后台任务键。`executor.py::execute_async` 的 UUID 创建和 `_background_tasks[execution_id]` 赋值是直接证据；`backend/tests/test_subagent_executor.py::test_execute_async_isolates_duplicate_external_task_ids` 用两个相同外部 ID 的执行证明结果和取消互不串线。

**模型一次响应中生成两个 `task` 调用，也不保证两个子 Agent 同时运行。** `backend/packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py::SubagentLimitMiddleware._truncate_task_calls` 会保留本轮限额内的调用，丢弃超额调用，并按本次 Run 已有委派数计算总额；`backend/tests/test_subagent_limit_middleware.py::test_task_calls_exceeding_limit_truncated` 验证了这一点。通过这一层后，`backend/packages/harness/deerflow/subagents/capacity.py::SubagentExecutionCapacity.slot` 还要取得进程内运行槽；槽满时可能排队、拒绝或等待超时。因此只有子任务无依赖、无冲突，且模型确实在同一轮派出多个调用、限额与容量允许，才有实际并行的机会。子 Agent 的完成顺序也不应被当作结果的逻辑顺序。

## 课程产出：Sub-agent 委派实验报告

用同一模型、相同运行配置和两个固定的只读子任务，分别做串行与并行委派。示例范围可以是：A 核对 `subagents/registry.py` 的配置解析及其测试；B 核对 `subagents/context_snapshot.py` 的历史筛选及其测试。两者均交付不超过五条结论，每条注明文件与符号，不编辑文件。两组使用同一份任务说明、`context_mode="isolated"` 和相同输出要求；分别在新 Thread 中运行，避免前一次历史改变下一次上下文。

串行组先派 A，收到结果后再派 B；并行组要求 Lead Agent 在同一次模型响应中派 A、B，最后统一综合。记录实际工具调用与事件：若并行组只产生一个调用、被限额截断，或执行容量导致排队，就标记“未形成并行”，不能把它当成并行结果。`task_started` 表示已派发，不单独证明子 Agent 已占到运行槽；需要结合执行开始/结束日志或追踪时间判断是否重叠。每组至少重复三次，记录同一口径的 Run 开始至最终回答耗时、Lead 与两个子 Agent 的 Token 用量（能取得时）及合计，并检查两组是否完成相同的交付要求。缺失的用量应标“不可得”，不要补估算值。

报告可写到 `learn/outputs/lesson-16-subagent-delegation.md`，至少包含下面的记录；表中的空格由实际实验填写，不代表本课已经测得性能结论。

| 组别与轮次 | A/B 是否实际重叠 | 总耗时 | Lead Token | A Token | B Token | 合计 Token | 结果核对与异常 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 串行 1～3 | 否 |  |  |  |  |  |  |
| 并行 1～3 | 待核对 |  |  |  |  |  |  |

报告最后写出两组耗时和 Token 的中位数、实际重叠证据、重复发现或综合成本，以及“在本任务上是否值得委派”的判断。环境、模型、限额、容量和用量来源也要记下，避免把一次模型决策差异误当成架构收益。实验不要求修改 DeerFlow 代码或调用昂贵的真实服务；没有可用模型时，保留方案与空表，并用上文的源码和测试完成机制验收。

**可选代码验证**：读 `registry.py::get_subagent_config` 可核对类型解析优先级；读 `executor.py::_build_initial_state` 可核对新消息状态和传递的运行状态；运行 `test_subagent_context_snapshot.py`、`test_subagent_limit_middleware.py` 及上文点名的 ID 测试，可验证隔离与限额不变量。这些定位练习用于复核正文结论，不是理解本课的前提。

面试验收：能用上述链路在 2～3 分钟内讲清 Lead Agent 为什么委派、`task` 如何返回结果；能区分子 Agent 的独立对话与共享资源，说明何时选 `snapshot`；能解释两个 ID 为什么分开，并用“任务依赖、共享状态、限额、容量、Token 成本”判断一次并行提议是否成立。下一课进入计划、持续目标与需要用户参与的中断流程。
