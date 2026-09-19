# 第 25 课：错误处理、Guardrail 与可观测性

第 21～24 课已经说明 Run 怎样被接纳、流式观察、取消、恢复，并在多实例竞争下保持唯一所有者。本课收束“运行时可靠性”阶段，回答最后一个核心问题：**模型、工具或控制机制出现异常时，DeerFlow 怎样让 Run 有明确结论、给用户可行动的信息，并留下足以复盘的证据？** 它位于请求主线的“模型/工具执行 → Middleware 干预 → ThreadState、Run 与事件写入 → Finalization”一段。核心阅读约 50～60 分钟。

前置知识是第 10 课的 Middleware 顺序、第 21 课的 Run 状态机，以及第 22 课对 SSE 与持久状态的区分。本课必须掌握三件事：错误恢复、确定性护栏（guardrail）和可观测性解决的是不同问题；`status`、`error` 与 `stop_reason` 必须一起解释；RunJournal、RunEventStore、Token Usage 和 Tracing 提供不同粒度的证据，任何单一来源都不能完整代表执行结果。

本课暂时不展开 Prompt Injection、工具越权、Sandbox 逃逸与部署防护，它们属于第 26 课；也不设计评测集和成功率指标，它们属于第 27、29 课。通用 Guardrail Provider 的权限策略只交代它在故障链中的位置，不在这里展开安全策略配置。大纲中的“超时”需要先校正：当前 Lead Agent worker 没有一条通用的 `running → timeout` 主转换；本课注入的是模型请求超时或子代理超时，并分别核对它们的真实收口，不能仅因 `RunStatus` 枚举含有 `timeout` 就假设 worker 会写入该状态。

## 先把三类机制分开

“系统没有崩”不等于“任务成功”，“留下很多日志”也不等于“故障已经处理”。本课使用下面三个定义：

- **错误处理**：面对异常或无效结果，决定重试、转换为可消费结果，还是让 Run 失败。例如模型连接超时重试、Tool 异常转成 `ToolMessage`。
- **确定性护栏**：面对语法上合法、但继续执行会带来成本、循环或安全风险的正常返回，主动改写控制流。例如 Token 超限后移除工具调用。
- **可观测性**：记录发生过什么、由谁产生、消耗多少以及最终怎样收口。它帮助定位和审计，但不会自动修复故障。

同一个现象可能同时经过三层。一次模型超时先由错误处理中间件重试，重试过程通过 SSE 和日志暴露，最终失败再由 worker 把 Run 写成 `error`；一次循环检测则没有 Python 异常，护栏主动停止工具路由，Run 通常仍是 `success`，并用 `stop_reason=loop_capped` 说明它不是干净完成。

| 维度 | 回答的问题 | 典型字段或证据 | 不能单独证明什么 |
| --- | --- | --- | --- |
| Run 结论 | 这次执行最终怎样结束 | `status`、`error`、`stop_reason` | 中间经历了哪些模型和工具步骤 |
| 用户可见结果 | 用户此刻看到了什么 | AI/Tool Message、SSE `messages`/`custom`/`error` | 内容是否已经持久化，Run 是否成功 |
| 持久事件 | 这次 Run 发生过哪些离散事实 | `run.*`、`llm.*`、`middleware:*`、`subagent.*` | 每个 token 的实时到达过程、完整调用栈 |
| Trace | 一次图执行内部各 span 怎样嵌套 | LangSmith、Langfuse 或 Monocle span | RunStore 的权威终态和业务正确性 |
| Token 汇总 | 成本与上下文消耗大致是多少 | Run 级总量、caller/model 分桶 | 提供商未上报的 token，任务是否完成 |

把它们放回一条故障主线，可以先记住下面这张图：

```text
模型或工具产生结果
  ├─ 抛异常
  │    ├─ 可重试模型异常 → 退避重试 → 成功或错误兜底
  │    ├─ Tool 异常 → error ToolMessage → 模型决定恢复路径
  │    └─ 未被转换的基础设施异常 → worker 捕获 → Run error
  └─ 正常返回
       ├─ 空终结回复 → 重试一次 → 回答或错误兜底
       ├─ 循环 / Token / Safety 信号 → 护栏改写消息与控制流
       └─ 普通结果 → 继续 Agent Loop

执行期间：RunJournal 收集完整消息、错误、Token 和中间件事件
终结期间：worker 决定 status/error/stop_reason，刷新事件并发布 END
外部追踪：Tracing callbacks 记录图、模型、工具的 span
```

## 模型失败：异常被转换了，Run 仍然可以判错

`backend/packages/harness/deerflow/agents/middlewares/llm_error_handling_middleware.py::LLMErrorHandlingMiddleware` 包裹每次模型调用。它先区分异常是否值得重试，再决定重试预算：

```python
_RETRIABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}

if isinstance(exc, IndexError):
    return True, "transient"
if status_code in _RETRIABLE_STATUS_CODES:
    return True, "transient"
```

这里的 `IndexError` 不是把任意程序错误都当网络抖动，而是兼容部分上游返回 `200 OK` 却没有 generation 的已知载荷故障。认证、额度或一般不可重试异常不会盲目重试；普通瞬时异常受 `llm_call.retry_max_attempts` 限制，默认最多 3 次尝试。`StreamChunkTimeoutError` 和突发速率限制（burst rate）都被收紧到最多 2 次尝试，即首次请求加一次重试。退避优先尊重 `Retry-After`，否则使用带随机抖动的延迟，避免大量 Run 在固定时刻同时重试。

每次真正重试前，中间件尽力发送一个 `custom` 事件：

```json
{
  "type": "llm_retry",
  "attempt": 1,
  "max_attempts": 3,
  "wait_ms": 1200,
  "reason": "transient",
  "message": "..."
}
```

它适合让在线用户知道系统仍在工作，但不是持久审计保证。`RunJournal` 没有通用的 `on_custom_event` 持久化入口；若需要事后证明重试次数，应结合模型错误 callback、日志或 Trace，而不是假设 `llm_retry` 一定存在于 RunEventStore。

### 重试耗尽后，为什么不是直接抛异常

重试耗尽或遇到不可重试错误时，中间件返回一条用户可见 `AIMessage`，并加上服务器字段：

```python
additional_kwargs={
    "deerflow_error_fallback": True,
    "error_type": error_type,
    "error_reason": reason,
    "error_detail": detail,
}
```

这样即使提供商失败，用户也能得到“额度不足”“认证无效”“暂时不可用”或“请拆分过长输出”等可行动信息；图本身也能正常收束并写入 Checkpoint。可是这条消息不能把失败伪装成成功。`runtime/runs/worker.py::run_agent` 会从根图流帧中寻找新产生的 `deerflow_error_fallback`，`RunJournal` 也会在 `on_llm_end` 中记录同一标志。任一路径命中后，worker 最终写：

```text
status = error
error  = error_detail / error_reason / fallback text
```

然后仍然执行 Finalization 并发布 END。于是“模型调用被转换成了一条 AIMessage”和“Run 成功”是两个不同判断。`backend/tests/test_run_worker_rollback.py::test_run_agent_marks_llm_error_fallback_as_error_status` 固定了这个边界。

这里还有一个审计细节：从 LangGraph callback 看，图可能正常返回，因此 RunJournal 的 `run.end` 事件会带图级 `status=success`；worker 随后才依据错误兜底把持久 Run 判成 `error`。`run.end` 证明“图调用返回了”，不等于 RunStore 的最终业务结论。查询事故时，最终状态以 Run 记录为准，事件负责解释过程。

### Circuit Breaker 不是另一种重试

连续可重试失败达到阈值后，模型错误中间件会打开熔断器（circuit breaker）。打开期间新调用快速得到带 `deerflow_error_fallback` 的用户消息，不再继续压迫故障提供商；恢复窗口结束后只允许一个半开探针。探针成功关闭熔断器，失败则重新打开。

突发速率限制不会计入“提供商整体不可用”的连续失败，因为继续把它累计进熔断器可能让瞬时流量尖峰演变成整进程的自我停服。异步取消和 LangGraph 的控制流异常 `GraphBubbleUp` 也原样传播，不计作提供商故障。

## 空回复：只修复“工具之后没有终结回答”

模型没有抛异常，也可能在 Tool 执行后返回空 `AIMessage`。如果直接把它当成功，用户看不到结论，严格提供商还可能在下一轮拒绝历史中的空 assistant 消息。

`TerminalResponseMiddleware` 的范围刻意很窄：当前轮必须有真实用户消息，后面必须已经出现 ToolMessage，最后一条 AIMessage 没有可见文本，也没有结构化、原始或非法工具调用意图。满足条件时：

1. 第一次空回复从状态中移除；
2. 下一次模型请求追加隐藏恢复提示，要求根据已有工具结果给出简短终结回答；
3. 同一 Run 第二次仍为空时，用可见错误文案替换空消息，并标记 `deerflow_error_fallback=true`；
4. worker 按上一节规则把 Run 判为 `error`。

恢复预算是“每个 Run 一次”，不是每个空消息一次。若恢复调用又去调用工具，预算也不会刷新，否则会形成“空回复 → 自动重试 → 再调工具”的新循环。没有本轮 Tool 结果的普通空回复不由这个中间件处理；带 Safety finish reason 的空回复则由 Safety 护栏处理，二者不能混为一类。

## Tool 失败：先成为对话事实，不立即终结 Run

`ToolErrorHandlingMiddleware` 捕获同步和异步工具抛出的普通异常，并返回标准错误 ToolMessage：

```python
ToolMessage(
    content="Error: Tool '...' failed with ... Continue with available context, or choose an alternative tool.",
    tool_call_id=tool_call_id,
    name=tool_name,
    status="error",
)
```

消息还会写入 `deerflow_tool_meta`，包含状态、错误类型、模型是否可自行恢复和推荐下一步。工具本身以 `status="error"` 返回、或以文本/JSON 表达失败时，也会经过 `normalize_tool_result` 归一化。`GraphBubbleUp` 不会被包装成 Tool 错误，因为 interrupt、pause、resume 属于图控制流，不是工具业务失败。

这一设计保留了 Agent 的恢复能力：文件不存在时可以换路径，搜索无结果时可以改查询，某个子代理失败时主 Agent 仍可使用已有材料。`RunJournal.on_tool_end` 会把 ToolMessage 持久化为 `llm.tool.result`；随后模型是否成功给出终结回答，才决定整个 Run 的结果。因此：

```text
ToolMessage.status = error
    不推出 Run.status = error
```

若模型根据错误换方案并完成任务，Run 可以是 `success`；若后续又触发模型错误兜底或未处理异常，才会是 `error`。可选的 `ToolProgressMiddleware` 还会根据连续“无新信息”结果警告或阻止特定工具，但它处理的是结果质量停滞；下一节的 Loop Detection 处理的是模型反复发出相同调用模式，两个状态机互不共享计数。

通用 Authorization/Guardrail 中间件也位于 Tool 执行之前。它可以在权限或外部策略拒绝时短路为 ToolMessage，并留下审计结果；安全策略、身份与 fail-closed 取舍留到第 26 课。本课只需掌握：**被策略拦住的调用没有执行，不能与“工具执行后抛异常”写成同一种故障。**

## 三个确定性护栏怎样终止危险的正常返回

模型有时返回完全合法的 AIMessage，却正在耗尽预算、重复调用或携带被提供商截断的工具参数。这时等待 Python 异常太晚了。DeerFlow 在 `after_model` 阶段检查这些信号，并通过改写最后一条 AIMessage 阻止工具节点继续执行。

### Token Budget：成本上限不是上下文窗口

`TokenBudgetMiddleware` 按 `run_id` 累加当前 Run 中 AIMessage 的 `usage_metadata`。它分别计算总 token、可选输入 token 和可选输出 token 的使用比例，以最高比例决定是否触发：

| 阶段 | 当前行为 |
| --- | --- |
| 低于 `warn_threshold` | 不干预 |
| 达到警告阈值 | 只警告一次；在下一次模型调用末尾追加隐藏 HumanMessage，要求尽快收尾 |
| 达到 `hard_stop_threshold` | 清除结构化与原始工具调用，补上强制停止文本，写 `stop_reason=token_capped` |

警告必须延迟到下一次模型调用，因为 `after_model` 时上一条 AIMessage 的 ToolMessage 还没产生；此时插入新消息会破坏严格提供商要求的 tool-call 配对。硬停止则直接清除当前 AIMessage 的 Tool Call，所以不再需要对应 ToolMessage。

Budget 是每个 Gateway Run 的预算；隐藏 Goal continuation 仍使用同一 `run_id`，不会通过重新进入图获得新额度。它也不是模型的上下文窗口：`context_window` 约束单次请求可容纳多少上下文，Token Budget 约束一整个 Run 累计愿意消费多少 token。

当前默认 `token_budget.enabled: false`。即使启用，护栏也只能计算提供商实际返回的 `usage_metadata`；缺失的 usage 不会被凭空估算。子代理用量先随终态 ToolMessage 回到父 Run，再由 `TokenUsageMiddleware` 归并到发起委派的 AIMessage，Budget 才能在父运行历史中看到它。关闭相关 usage 采集或使用不报告 usage 的提供商时，成本护栏的覆盖范围会变窄，这应当作为实验限制记录。

### Loop Detection：相同参数与同类工具是两种循环

`LoopDetectionMiddleware` 有两层检测：

1. **相同调用集合**：把一轮 Tool Call 的名称与稳定参数键排序后哈希，在滑动窗口内统计同一集合；默认第 3 次警告，第 5 次硬停止。
2. **工具类型频率**：在有界窗口内统计同一工具名，即使参数一直变化也能发现连续读不同文件之类的高频循环；默认 30 次警告、50 次硬停止，并支持按工具覆盖阈值。

警告同样延迟到下一次模型调用；硬停止清除 `tool_calls`、`additional_kwargs.tool_calls/function_call`，必要时把 `finish_reason=tool_calls` 改成 `stop`，并写 `stop_reason=loop_capped`。检测历史按 `(thread_id, run_id)` 隔离，所以同一 Thread 的下一轮用户请求获得新预算，而同一 Run 的 Goal continuation 继续共享历史。

警告与硬停止会通过 `RunJournal.record_middleware` 保存为 `middleware:loop_detection`。持久事件只记录检测层、工具名、计数和阈值，不记录工具参数、消息正文或参数派生哈希；这既足以回答“为什么被停止”，也避免把敏感命令或搜索内容复制到审计事件。

### Safety Finish Reason：正常返回也可能不安全可执行

Safety finish reason 是提供商用正常响应表达的安全终止信号，例如 OpenAI 兼容接口的 `finish_reason=content_filter`、Anthropic refusal 或 Gemini `SAFETY`。它不是异常，所以模型错误重试层不会处理。

风险在于：安全过滤可能发生在 Tool Call JSON 生成中途，消息仍带非空 `tool_calls`。LangChain 的工具路由只看到“有调用”，可能执行截断的 `write_file`、命令或其他参数。`SafetyFinishReasonMiddleware` 因此先于 Loop Detection 观察原始模型结果：

- 有 Tool Call 时，清除结构化和原始调用，保留已有文本并追加用户说明；
- 没有 Tool Call 且内容为空时，回填一条说明，避免把空 assistant 消息写进 Thread；
- 没有 Tool Call 且已有可见文本时，不改写，让提供商的部分回答自然到达用户。

前两种真正干预的路径会写 `stop_reason=safety_capped`，发送实时 `custom` 事件 `safety_termination`，并持久化 `middleware:safety_termination`。审计内容有 detector、reason、被抑制的工具名/ID和数量，但故意没有被过滤的工具参数。检测器自身抛异常只记录日志并视为未匹配，不能让一个自定义 detector 击穿整个 Run。

## `success + stop_reason` 为什么不是矛盾

Loop、Token 和 Safety 硬停止都不抛异常。它们让图自然终结，并把“为什么提前停止”放到 runtime context；worker 最后读取该值。因此当前主路径通常是：

| 场景 | Run `status` | `error` | `stop_reason` | 含义 |
| --- | --- | --- | --- | --- |
| 正常回答 | `success` | `null` | `null` | 干净完成 |
| 模型异常重试后成功 | `success` | `null` | `null` | 中途恢复成功；重试证据在实时事件、日志或 Trace |
| 模型异常重试耗尽 | `error` | 具体错误 | 通常为空 | 有可见错误兜底，但任务失败 |
| Tool 异常后模型恢复 | `success` | `null` | `null` | 局部工具失败，整体完成 |
| 二次空终结回复 | `error` | 空回复错误原因 | 通常为空 | 用户看到兜底，整体失败 |
| Token 硬停止 | `success` | `null` | `token_capped` | 图正常收束，但因预算提前停止 |
| Loop 硬停止 | `success` | `null` | `loop_capped` | 图正常收束，但因循环提前停止 |
| Safety 抑制工具或回填空内容 | `success` | `null` | `safety_capped` | 危险调用未执行，结果受安全信号限制 |
| 未捕获的图/Checkpoint/Bridge 异常 | `error` | 异常文本 | 通常为空 | worker 捕获执行异常并发布 `error` SSE |

同一套模式还用于 `model_length_capped` 和 `subagent_limit_capped`，但它们不是本课核心。`success` 在这里表示 Run 的执行与终结协议完成，不表示用户目标一定完整达成；`stop_reason` 正是把“干净完成”和“受限完成”分开的结构化信号。

如果 Run 产生了 Outputs 文件却没有通过 `present_files` 覆盖交付，或交付收据无法持久验证，worker 还可能在 Finalization 把本来成功的结果降为 `error`。因此最终状态必须在终结后读取，不能仅从最后一条 AIMessage或护栏局部结果推断。

## RunJournal 与 RunEventStore：采集器和存储不是同一个对象

`backend/packages/harness/deerflow/runtime/journal.py::RunJournal` 是每个 Run 一个的 LangChain callback handler。worker 在可失败的预检之前创建它，把它挂到图根调用的 callbacks，并通过 `runtime.context` 给需要写审计事件的 Middleware 一个窄入口。它完成四类工作：

1. 把完整的 Human、AI 和 Tool 消息标准化为事件；它不记录每个 `on_llm_new_token`，token 级实时流由 SSE 负责；
2. 记录根图的 `run.start`、`run.end`、`run.error` 和 `llm.error` 等过程事件；
3. 接收 `record_middleware()`，形成 `middleware:{tag}` 审计事件；
4. 在内存中累计 Token、消息数、首条用户消息、最后一条可见 AI 文本和 Artifact 交付事实，终结时交给 RunManager。

`RunEventStore` 是统一的存储接口。它要求同一 Thread 内 `seq` 严格递增，`list_messages()` 只返回 `category=message`，`list_events()` 返回指定 Run 的完整事件。当前实现有三种：

| Backend | 适用场景 | 边界 |
| --- | --- | --- |
| `memory` | 开发和单元测试，当前默认 | 重启丢失，不能跨 worker |
| `jsonl` | 单节点、本地调试、便于直接检查 | 不是多 worker 共享事件后端 |
| `db` | 生产查询、多 worker 与控制台统计 | 依赖 SQL 数据库；仍需处理存储失败 |

事件目录由 `runtime/events/catalog.py` 集中定义。核心固定类型包括：

```text
run.start / run.end / run.error
llm.human.input / llm.ai.response / llm.tool.result / llm.error
subagent.start / subagent.step / subagent.end
middleware:<tag>
run.delivery
```

RunJournal 先缓冲、达到阈值后批量写，Finalization 再强制 flush。普通事件 flush 失败会记录警告并尽力重试，但不会自动把所有业务结果都改成失败；交付收据则有更强的 fail-closed 规则。由此可见，可观测性也有自己的失败模式：事件后端不可用时，Run 状态、Checkpoint、SSE 和外部 Trace 可能保留不同程度的证据。事故复盘必须注明“没有观察到”还是“已证明没有发生”。

Gateway 提供 `GET /api/threads/{thread_id}/runs/{run_id}/events` 查询当前用户可见的 Run 事件；Run 本身通过对应的 `GET .../runs/{run_id}` 查询。线上若要跨 Thread 查询运行量、Token 和成本，`/api/console` 的 reporting API 要求 SQL database backend；`database.backend: memory` 会返回 503，而不是把进程内片段伪装成完整报表。

## Token Usage：一条用量怎样进入 Run 记录

Token 数据有两条相互配合、但开关不同的路径。

第一条是消息与 Budget 路径。`TokenUsageMiddleware` 读取 AIMessage 的 `usage_metadata`，记录本次步骤的归因信息；子代理终态用量随当前 Run 的 ToolMessage 返回，再被合并到发起委派的 AIMessage。`TokenBudgetMiddleware` 扫描这些消息的 usage 增量，实施本 Run 的软警告和硬上限。顶层 `token_usage.enabled` 控制 TokenUsageMiddleware 是否进入链。

第二条是运行汇总路径。RunJournal 在 `on_llm_end` 中按 LangChain model run ID 去重，累计：

```text
total_input_tokens / total_output_tokens / total_tokens
llm_call_count
lead_agent_tokens / subagent_tokens / middleware_tokens
token_usage_by_model[model_name]
```

提供商上报 prompt cache hit 时，还会把 `input_token_details.cache_read` 放进对应模型的稀疏 `cache_read_tokens` 桶。运行中，journal 以节流方式把进度快照写入 Run 记录；Finalization 再写最终汇总。`run_events.track_token_usage` 控制这条 journal 汇总路径，和 `token_usage.enabled` 不是同一个开关。

Token 数字仍是提供商上报事实的聚合，不是 DeerFlow 对所有文本重新分词后的精确估算。缺失的 `usage_metadata`、被外部系统执行却没有返回 usage 的调用，以及配置关闭时的调用都可能形成空白。成本计算还需要模型 pricing 配置；“有 token”不等于“必然有准确费用”。

## Tracing：看调用树，不替代 Run 审计

DeerFlow 支持两类接线方式：

- LangSmith 与 Langfuse 由 `tracing/factory.py::build_tracing_callbacks()` 创建 LangChain callback，并挂在图调用根部。因此一个图 trace 可以把节点、LLM 和 Tool 调用组织成父子 span，而不是每个模型调用各自成为孤立 trace。
- Monocle 不是每 Run callback。Gateway lifespan 只初始化一次进程级 OpenTelemetry provider 并自动插桩；嵌入式 `DeerFlowClient` 和 TUI 不会因环境变量自动完成这一步，调用者需要显式初始化。

Langfuse 路径还会把 Thread 映射为 session、有效用户映射为 user、assistant 映射为 trace name，并添加环境和模型 tag。`deerflow_trace_id` 来自请求入口的 ContextVar，与同一 HTTP 响应的 `X-Trace-Id`、日志字段和 Run metadata 对齐。它不是 `run_id`：一个请求 trace 用于跨日志/span 关联，一条 Thread 可以有许多 Run，而 Run ID 仍是执行与持久状态的主键。

| 标识 | 主要用途 | 常见误用 |
| --- | --- | --- |
| `thread_id` | 长期会话与 Langfuse session 分组 | 当成一次执行 ID |
| `run_id` | 一次 Agent 执行、状态和事件查询 | 当成跨服务请求 trace |
| `deerflow_trace_id` / `X-Trace-Id` | 关联入口请求、日志和外部 trace | 从客户端 metadata 任意覆盖服务器值 |
| LangChain model/tool run ID | callback/span 内部调用去重与父子关系 | 当成 Gateway Run ID |

Trace 平台适合观察延迟、嵌套调用和模型/工具 span；RunEventStore 适合产品内消息恢复与离散审计；RunStore 负责权威状态和聚合字段。外部追踪通常还意味着把输入、输出或元数据发送给另一个系统，具体采集范围取决于 provider 配置。生产启用前应把 Trace 当作敏感数据面审查，而不是默认无风险日志。

## 用一次模型超时串起全部证据

假设测试模型连续抛出 `APITimeoutError`，可以沿下面的顺序复盘：

```text
第 1 次模型调用抛 APITimeoutError
  → LLMErrorHandlingMiddleware 分类为 transient
  → 发 llm_retry custom 事件，退避
第 2/3 次仍失败
  → 返回带 deerflow_error_fallback 的 AIMessage
  → 用户看到可行动的错误文案
  → RunJournal 可记录 llm.error / AI 响应与已有 token 信息
  → worker 从新消息标记或 journal 标记识别失败
  → Run.status=error，error=具体 provider detail
  → Finalization 刷新事件、更新汇总、发布 END
  → Trace 中保留各次模型 attempt/span（取决于已启用 provider）
```

这里不会自动得到 `RunStatus.timeout`。模型请求超时是 LLM 层的可重试异常，重试耗尽后是 Run `error`。子代理自身超时则通过 `task_timed_out`/结构化 ToolMessage 回到父 Agent，父 Run 仍可能利用其他结果成功结束。只有看到真实状态写入代码或测试，才能声称某条路径使用 `timeout` 枚举。

## 课程产出：故障分类表和故障注入测试

建议把实验记录保存到 `learn/outputs/lesson-25-fault-injection.md`，并在 `backend/tests/` 下提交一份局部故障注入测试。不要调用真实提供商，也不要靠长时间 `sleep` 制造超时；使用假 model/tool handler、`AsyncMock`、事件屏障或把退避配置设为 0，使测试快速且确定。

至少完成下面五类注入。每一行同时记录用户消息、Run、SSE/事件和 Token/Trace 四类结果；只验证异常对象或函数返回值不算完成。

| 注入 | 建议触发方式 | 应验证的核心结果 |
| --- | --- | --- |
| 模型失败 | 假 handler 连续抛 `APIConnectionError`，另做一次先失败后成功 | 成功重试不会留下错误终态；耗尽后产生标记 fallback，worker 写 `error`，仍有 END |
| 超时 | 假 handler 抛 `APITimeoutError` 或 `StreamChunkTimeoutError` | 按有效预算重试；后者最多一次重试；最终是 `error` 而非凭空假定的 `timeout` |
| Tool 失败 | 假工具抛 `ValueError` | 得到 `status=error` 且带 meta 的 ToolMessage；`GraphBubbleUp` 仍传播；父 Run 是否失败取决于后续模型 |
| 循环 | 把同一组 Tool Call 驱动到 `hard_limit` | 工具没有继续执行；Run `success` 且 `stop_reason=loop_capped`；持久审计不含参数 |
| Token 超限 | 构造带 usage 的 AIMessage 越过 hard threshold | Tool Call 被移除；Run `success` 且 `stop_reason=token_capped`；汇总数字不重复累计 |

建议再加两条边界用例：Tool 后连续两次空回复应成为 Run `error`；带 `content_filter` 的截断 Tool Call 应完全不进入工具节点，并留下 `safety_capped` 与 `middleware:safety_termination`。

记录表至少使用下面这些列：

| case | 注入点 | 重试次数 | 用户可见结果 | Tool 是否执行 | Run status | error | stop_reason | 持久事件 | Token/Trace 证据 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- |
| `model-timeout` | model handler | 实际值 | 实际文案摘要 | 不适用 | `error` | 实际 detail | 空 | 实际观察 | 实际观察 |

先运行现有局部测试建立基线；这些测试不需要真实模型或网络：

```powershell
cd backend
uv run pytest tests/test_llm_error_handling_middleware.py -k "async_model_call_retries_busy_provider_then_succeeds or async_model_call_marks_transient_retry_exhaustion_as_error_fallback or async_stream_chunk_timeout_retries_once" -q
uv run pytest tests/test_tool_error_handling_middleware.py -k "returns_error_tool_message_on_exception or reraises_graph_interrupt" -q
uv run pytest tests/test_terminal_response_middleware.py -k "retries_empty_post_tool_response_once or second_empty_post_tool_response_becomes_visible_error_fallback" -q
uv run pytest tests/test_loop_detection_stop_reason.py -q
uv run pytest tests/test_run_journal.py -k "token_accumulation or record_middleware_uses_middleware_category or chain_start_end_produce_trace_events" -q
uv run pytest tests/test_tracing_factory.py tests/test_tracing_metadata.py -q
```

在自己的测试中，至少直接复用一次 `MemoryRunEventStore` 和真实 `RunJournal`，核对事件 envelope 与 Run 汇总；再通过 `run_agent()` 驱动一次真实 worker 终结，核对 `status/error/stop_reason` 与 `publish_end`。不要把所有场景都写成调用 Middleware 私有方法的孤立单元测试，否则无法证明 Middleware 写入的 runtime context 真被 worker 读取。

若本地已启动 Gateway，可以额外用真实 Run ID 查询：

```text
GET /api/threads/{thread_id}/runs/{run_id}
GET /api/threads/{thread_id}/runs/{run_id}/events
```

把查询结果与浏览器 Network 中的 `custom`、`error`、`end` 对照。不要保存 Cookie、密钥、完整 Prompt、工具参数或第三方返回正文；课程产出只保留事件类型、关键状态、短错误摘要和脱敏 Trace 链接/ID。

## 可选代码深挖与面试验收

代码深挖按故障链而不是文件数量排序。先读 `llm_error_handling_middleware.py::awrap_model_call`、`terminal_response_middleware.py::_apply` 和 `tool_error_handling_middleware.py::awrap_tool_call`，比较“重试后错误兜底”“空回复恢复”“ToolMessage 化”三种收口；再读 `token_budget_middleware.py::_apply`、`loop_detection_middleware.py::_track_and_check/_apply` 与 `safety_finish_reason_middleware.py::_prepare_intervention`，确认三个护栏各自检查什么、怎样阻止 Tool；最后读 `runtime/journal.py::RunJournal` 和 `runtime/runs/worker.py::run_agent` 的结果判定与 finally，连接事件、Token 汇总和最终 Run 状态。Tracing 只需再读 `tracing/factory.py` 与 `tracing/metadata.py`，无需展开各供应商 SDK 内部实现。

面试验收：能在 3 分钟内从一次模型超时讲到“分类 → 退避重试 → 用户错误兜底 → worker 判 `error` → journal flush → END”，并解释为什么可见 AIMessage 不等于成功；能比较 Tool 异常、二次空回复、Token 超限、循环和 Safety 截断的不同状态结果；能说明 `success + stop_reason` 的语义；能画出 RunStore、RunJournal、RunEventStore、SSE 和 Trace 的证据边界，并指出 `run.end`、`END`、Run `status` 为什么不能互相替代；能明确说明当前 Lead worker 没有通用 `RunStatus.timeout` 主转换，超时结论必须按发生层次核对。
