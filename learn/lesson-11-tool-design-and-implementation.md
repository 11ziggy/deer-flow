# 第 11 课：Tool 的设计与实现

第 10 课看到中间件可以在工具调用边界放行、拒绝或改写结果。本课进入第三阶段，回答一个更具体的问题：**模型提出一个行动请求后，怎样把它变成参数明确、上下文受控、结果可处理的工具（Tool）？** 本课位于“模型产生工具调用请求（Tool Call）→ 运行时（Runtime）校验并执行工具 → 工具结果消息（ToolMessage）/ ThreadState 更新”这一段请求主线。核心阅读约 35～45 分钟。

前置知识是第 4 课的工具调用消息、第 5 课的 Agent Loop、第 8 课的状态合并，以及第 9～10 课的工具装配和调用包装。必须掌握模型可见的工具契约与运行时注入信息的区别、类型校验与业务校验的区别、普通返回值与状态更新的区别，以及同步和异步实现的选择。Sandbox 生命周期、Skill 激活、MCP 工具发现分别留到第 12～14 课；本课只处理一个 Tool 的调用边界。

## 从“模型想调用”到“工具真的执行”

工具（Tool）不是模型的一段特殊回答，而是运行时注册的可调用能力。模型提供商根据收到的工具名称、描述和参数结构，生成一个包含名称、参数和调用 ID 的 Tool Call；**提供商不替 DeerFlow 执行本地 Python 函数**。第 9 课的 `backend/packages/harness/deerflow/tools/tools.py::get_available_tools` 会装入配置工具和符合条件的内置工具等，按实际工具名去重；Lead Agent 把最终工具列表交给 LangChain `create_agent()`。LangChain / LangGraph 负责把模型请求路由到匹配工具，并把执行结果接回消息或状态；DeerFlow 负责具体工具逻辑、运行上下文和调用边界上的策略。

下面用“列出本会话以前上传的 PDF”贯穿本课。这是教学请求，不是实际运行日志：

```text
用户请求 → AIMessage.tool_calls 中出现
  {name: "list_uploaded_files", args: {extensions: ["pdf"]}, id: "call-1"}
→ LangGraph 按名称找到工具并校验模型给的参数
→ 注入工具运行时上下文（ToolRuntime），执行 list_uploaded_files
→ 结果作为对应 call-1 的 ToolMessage 进入消息状态
→ 模型读到结果，决定继续调用工具或回答
```

`tool_call_id` 是配对键：下一条工具结果必须能回答那次助手工具请求。它不是业务数据，也不该由模型作为普通参数填写。若模型给了错误参数，参数结构能阻挡一部分错误；若给了类型正确却不允许的路径或身份，工具仍须自己拒绝。一次工具执行成功，也不等于整个用户任务完成。

## 工具契约：模型看见什么，运行时补上什么

工具参数结构（Tool Schema）告诉模型能填哪些字段及其类型、默认值和说明。`backend/packages/harness/deerflow/tools/builtins/list_uploaded_files_tool.py::list_uploaded_files` 的签名可摘成：

```python
@tool
def list_uploaded_files(
    runtime: Runtime,
    include_outline: bool | list[str] = False,
    max_results: int = 20,
    query: str | None = None,
    extensions: list[str] | None = None,
) -> dict:
    return _list_uploaded_files_impl(
        include_outline=include_outline,
        max_results=max_results,
        runtime=runtime,
        query=query,
        extensions=extensions,
    )
```

这是**摘录并简化了 `Annotated` 参数说明**的代码，不是可直接复制的原函数。真实函数为每个可见参数写了用途说明；`runtime` 的类型来自 `backend/packages/harness/deerflow/tools/types.py::Runtime`，即以 DeerFlow 上下文和 `ThreadState` 参数化的 `ToolRuntime`。`backend/tests/test_list_uploaded_files_tool.py::TestToolSchema` 验证模型侧参数只有 `include_outline`、`max_results`、`query`、`extensions`，没有 `runtime`，且可以生成 OpenAI 风格的工具结构。因此，“Python 函数需要的参数”不等于“允许模型填写的参数”。

`backend/packages/harness/deerflow/runtime/runs/worker.py::_build_runtime_context` 建立本次 Run 的上下文字典，至少写入服务端的 `thread_id` 和 `run_id`；`run_agent` 再把它安装进图运行时。Tool 通过 `runtime.context` 取运行身份，通过 `runtime.state` 读取当前状态，通过 `runtime.config` 取得运行配置。`backend/tests/test_list_uploaded_files_tool.py::test_real_graph_state_propagation_to_list_uploaded_files` 用真实 `create_agent` 图和假模型验证：`UploadsMiddleware` 写入的本轮 `uploaded_files` 确实能在工具的 `runtime.state` 里看到，从而把本轮文件排除在“历史文件”结果之外。这里无需真实模型或 Gateway，也证明了上下文不是工具自行猜出的全局变量。

类型校验只解决参数形状。LangChain 的工具对象持有生成的 `args_schema`；`backend/tests/test_tool_args_schema_no_pydantic_warning.py::test_tool_args_schema_does_not_emit_pydantic_context_warning` 直接用该结构验证参数并序列化。`max_results: int` 说明要整数，但 `_list_uploaded_files_impl` 仍将数值限制到 1～100；查询词和扩展名也在函数内归一化。另一个内置工具 `backend/packages/harness/deerflow/tools/builtins/present_file_tool.py::present_file_tool` 接收 `filepaths: list[str]`，但还会调用 `_normalize_presented_filepath`，核对路径是否属于**当前 Thread 的 outputs 目录**。`backend/tests/test_present_file_tool_core_logic.py::test_present_files_rejects_paths_outside_outputs` 用 workspace 内的文件证明“字符串路径类型正确”不代表可以展示。业务边界、权限和资源上限不能仅靠模型指令或参数类型保证；第 12 课再讲 Sandbox 路径映射本身。

## 工具结果怎样回到 Agent

普通工具可以返回数据，框架会把它作为工具结果接回消息链。上述 `list_uploaded_files` 返回包含 `files`、`message` 等字段的字典；真实图测试会在最终 `messages` 中找到对应的 `ToolMessage`。当工具还要更新 Agent 状态时，可以返回 LangGraph 的状态更新命令（`Command`）。`present_file_tool` 的成功路径核心形状是：

```python
return Command(
    update={
        "artifacts": normalized_paths,
        "messages": [ToolMessage("Successfully presented files", tool_call_id=tool_call_id)],
    },
)
```

这段摘自 `present_file_tool.py::present_file_tool`。它同时写入待展示文件的 `artifacts` 和配对的工具消息；`artifacts` 仍按第 8 课的 reducer 合并，并非在 Tool 中直接改写共享状态。函数的 `tool_call_id: Annotated[str, InjectedToolCallId]` 由调用框架注入，不放进模型的业务参数。`backend/tests/test_present_file_tool_core_logic.py::test_present_files_normalizes_host_outputs_path` 断言成功结果中的虚拟路径和消息；越界路径的测试断言没有 `artifacts` 更新，并返回说明错误的消息。

错误至少要分清两类：

| 情况 | 当前代码如何处理 | 设计含义 |
| --- | --- | --- |
| 可预期的业务拒绝，例如 `present_files` 收到 outputs 外路径 | 工具捕获 `ValueError`，返回只含错误 `ToolMessage` 的 `Command` | 不提交展示文件的状态更新；模型能看到拒绝原因。该消息当前没有显式设置 `status="error"`，不能仅凭该字段判断业务成功。 |
| 工具执行中抛出普通异常 | `ToolErrorHandlingMiddleware.wrap_tool_call` / `awrap_tool_call` 捕获并构造 `status="error"`、带原调用 ID 的 `ToolMessage` | 给 Agent 一个可继续处理的失败结果；`GraphBubbleUp` 等中断控制流仍会重新抛出。 |

第二类的直接证据是 `backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py::ToolErrorHandlingMiddleware._build_error_message` 与同步、异步包装方法；`backend/tests/test_tool_error_handling_middleware.py::test_wrap_tool_call_returns_error_tool_message_on_exception`、`::test_awrap_tool_call_returns_error_tool_message_on_exception` 和 `::test_wrap_tool_call_reraises_graph_interrupt` 固定了调用 ID、错误状态及中断边界。由此不能推断“所有失败都安全、可重试，或整个 Run 一定成功”：工具可能已经产生副作用，异常包装也不会自动撤销它。更完整的故障分类在第 25 课处理。

`backend/packages/harness/deerflow/agents/middlewares/tool_result_meta.py::normalize_tool_result` 还会给带 `Error:` 前缀的工具消息附加错误分类元数据，但不会因此把消息自身的 `status` 改成 `error`。设计新工具时，应明确返回结构和失败语义，不能把任意错误文本当作可靠的状态协议。

## 同步、异步与工具粒度

同步函数适合简单、短时的本地计算；需要等待网络或其他异步接口时，应优先提供与调用方式匹配的异步实现，并避免在事件循环上直接做阻塞工作。第 10 课的 `ToolErrorHandlingMiddleware` 也有两条对应入口：`wrap_tool_call` 直接调用 `handler(request)`，`awrap_tool_call` 则 `await handler(request)`。项目两种路径都要照顾：`backend/packages/harness/deerflow/tools/tools.py::_ensure_sync_invocable_tool` 在装配时给**只有协程实现**的工具补一个同步入口；`backend/packages/harness/deerflow/tools/sync.py::make_sync_tool_wrapper` 在同步调用路径运行该协程。其同步调用仍会等待结果，不能把适配器误解为“任意同步代码都自动不阻塞”，也不要求每个 Tool 都手写两份相同的实现。`backend/tests/test_tool_args_schema_no_pydantic_warning.py::test_sandbox_tool_sync_async_signatures_and_forwarding_stay_aligned` 验证了一组 Sandbox 工具的同步、异步签名及参数转发一致；具体 Sandbox 初始化留到下一课。

工具粒度应围绕**一个模型能明确选择、参数能约束、结果能验证的动作**。例如，`list_uploaded_files` 只做历史文件发现，`present_files` 只把合规结果加入展示列表。若把“搜索、读取、改写、发布”塞进一个工具，模型难以知道何时调用，Schema 也很难表达不同分支的约束；若为每一种文件后缀都建一个独立工具，又会增加选择负担。判断粒度时依次问：调用前能否说明动作及副作用？参数是否能在入口校验？失败后能否指出是哪一步失败？返回结果是否足够短且能让下一步判断？这比“函数越小越好”更可操作。

## 可选实践：Markdown 结构检查 Tool

大纲的课程产出是“带类型、错误处理和单元测试的 Tool”。理解本课不以提交新功能为前提；愿意做二次开发时，可实现一个**只读**的 Markdown 结构检查工具，检查给定文件是否存在一级标题、标题层级是否跳级，返回问题列表及行号。先把契约写清：模型只填允许的相对路径；运行时注入 Thread / 用户上下文；工具在读取前验证路径属于当前允许的工作区，并限制文件大小；读取或解析失败返回可辨识的错误，不返回未验证的“检查通过”。如果需要把结果写入状态，才使用 `Command`，否则返回有界的结构化结果即可。

至少覆盖四个测试：合法文件得到确定的问题列表；跳级标题报告正确行号；越界路径在读取前被拒绝；不存在或无法读取的文件得到错误结果。再加一个工具 Schema 测试，确认模型可见参数只有路径而没有 `ToolRuntime`；若提供异步入口，还要核对其结果与同步入口一致。建议先在不接真实模型的条件下测试纯检查函数和工具包装，再把工具接入本地 Agent 测试。这样得到的是可验证的 Tool 契约，而不是一次偶然成功的模型演示。

面试验收：能沿开头的 `call-1` 路径解释模型、LangChain / LangGraph、DeerFlow 各做什么；能用 `list_uploaded_files` 说明 Schema 与 `ToolRuntime` 为什么分开，用 `present_files` 说明类型校验之后仍要做业务校验；能解释普通返回值、`Command`、`ToolMessage` 和调用 ID 的关系；能说清同步 / 异步适配的边界，并为一个新 Tool 给出合理粒度、错误结果和最少测试。下一课再追踪工具访问文件时依赖的 Sandbox 与 Artifact 生命周期。
