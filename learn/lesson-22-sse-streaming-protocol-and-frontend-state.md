# 第 22 课：SSE 流式协议与前端状态

第 21 课说明了 Run 怎样在后台走向终态。本课继续追踪同一个 Run，回答一个更靠近用户的问题：**后台不断产生的模型片段、状态变更和子代理进度，怎样经过服务器发送事件（Server-Sent Events，SSE）到达浏览器，并被合并成稳定、无重复的前端状态？** 它位于请求主线的“Agent Graph 产生事件 → StreamBridge → Gateway SSE → LangGraph JavaScript 软件开发工具包（SDK）→ React 状态与界面”一段。核心阅读约 40～50 分钟。

前置知识是第 6 课的完整请求链、第 7 课对 ThreadState 与 Event 的区分，以及第 21 课对终态与流结束的区分。本课必须掌握三层边界：SSE 只是传输格式，`stream_mode` 决定业务载荷，前端还要按各字段的合并语义维护用户界面（UI）；同时要能解释当前 Web 聊天为什么使用增量事件而不在每一步接收完整 `values`，子代理进度为什么走 `custom`，以及乐观消息怎样与服务端消息合并。页面刷新后的重连、`Last-Event-ID` 重连游标、流缺口恢复、取消和 Checkpoint 回滚留到第 23 课；本课只说明这些机制在事件消费端留下的接口。

## 先把三个“流”分开

一次回复看起来只是文字逐字出现，源码里其实叠了三层协议。SSE 是建立在超文本传输协议（HTTP）响应上的单向事件格式，不是完整的消息或状态模型。把这些层次混在一起，就会产生“收到 SSE 就等于收到一条消息”之类的错误理解。

| 层次 | 本课中的含义 | 典型证据 |
| --- | --- | --- |
| SSE 传输层 | 一个 HTTP 响应中连续发送 `event:`、`data:`、可选 `id:` 字段，空行结束一帧 | `services.py::format_sse`、`sse_consumer` |
| LangGraph 流模式层 | 调用者选择要观察完整状态、节点更新、消息片段还是自定义事件 | `stream_modes.py`、`worker.py::_stream_once` |
| 前端状态层 | SDK 与 DeerFlow Hook 把不同帧按不同规则合并，再决定何时通知 React 和何时渲染 | SDK `StreamManager`、`hooks.ts::useThreadStream` |

主链路可以压缩成：

```text
useThreadStream / thread.submit
  → POST /api/threads/{thread_id}/runs/stream
  → start_run 创建后台 Run
  → run_agent 调用 graph.astream(...)
  → StreamBridge.publish(run_id, event, data)
  → sse_consumer 格式化为 text/event-stream
  → LangGraph SDK 解析帧、拼接消息
  → onUpdateEvent / onCustomEvent + DeerFlow 消息合并
  → React 以受控频率重渲染
```

SSE 在这里是服务器到浏览器的单向输出通道；用户发送消息仍是普通 HTTP POST。SSE 也不保存 ThreadState：在线帧由 StreamBridge 暂存，长期对话状态由 Checkpointer 和消息事件存储负责。连接断开不等于状态消失，状态存在也不保证任意旧帧仍可重放。

## 当前事件词汇：模式名不总等于线上事件名

大纲列出 `metadata`、`values`、`messages-tuple`、`custom` 和 `end`。当前代码还把 `updates` 用作 Web 聊天的主要状态增量，而且“请求中的 `messages-tuple`”在线上实际表现为 `event: messages`。面试或抓包时必须区分请求模式名与 SSE 事件名。

| 请求模式或控制事件 | 实际 SSE 名称 | 载荷语义 | 当前 Web 聊天怎样使用 |
| --- | --- | --- | --- |
| 控制事件 | `metadata` | `{run_id, thread_id}` | worker 会发布；但 `onCreated` 主要从响应头 `Content-Location` 更早取得同一 Run 身份 |
| `messages-tuple` | `messages` | `[消息片段, 元数据]`；同一消息可有多帧 | SDK 按消息标识（ID）拼接，形成实时 AI/Tool 消息 |
| `updates` | `updates` | `{节点名: 本节点写入}`，是 reducer 输入而非完整状态 | DeerFlow 只合并 `title`、`artifacts`、`todos`、`goal` 等渲染字段 |
| `values` | `values` | 某一步之后的完整根图状态快照 | Gateway 支持，但当前 Web 聊天明确不请求 |
| `custom` | `custom` | 应用自定义载荷，例如 `task_*` 子代理事件、`llm_retry` | 进入 `onCustomEvent`，不自动并入 ThreadState |
| 控制事件 | `end` | `null` | 表示发布者不会再发送正常尾部事件；不是 Run 状态枚举 |

Gateway 还可能发出 `error`、`gap`，空闲时也可能发送 `: heartbeat` 注释。它们分别表示流错误、无法完整重放以及连接保活，不是本课要求展开的正常数据模式。尤其是 `gap` 不能当成 `end`：它要求客户端回到持久状态恢复，而 Run 可能仍在执行。

### `metadata`：先确定“这是谁的流”

`backend/packages/harness/deerflow/runtime/runs/worker.py::run_agent` 在真正构造 Agent 前发布：

```python
await bridge.publish(
    run_id,
    "metadata",
    {"run_id": run_id, "thread_id": thread_id},
)
```

创建流接口同时设置响应头：

```text
Content-Location: /api/threads/{thread_id}/runs/{run_id}
```

当前 LangGraph JavaScript SDK 的 `onRunCreated` 从这个响应头提取身份，因此 `hooks.ts::onCreated` 可以立刻把新 Thread 放入侧栏缓存并触发 `onStart`，不必等待后台 worker 运行到 `metadata` 帧。`metadata` 帧仍保持协议兼容，并允许注册了 `onMetadataEvent` 的消费者观察它。这里体现了两条不同保证：响应头确认“创建请求接纳的是哪个 Run”，流事件确认“这个发布流属于哪个 Run”。

### `messages-tuple`：请求别名与线上 `messages`

`runtime/stream_modes.py::to_langgraph_stream_modes` 把公开的 `messages-tuple` 映射为 LangGraph 内部的 `messages`：

```python
mapped = ["messages" if mode == "messages-tuple" else mode for mode in modes]
```

LangGraph 的 `messages` 模式产生 `(message_chunk, metadata)` 元组；`serialization.py::serialize_messages_tuple` 把它序列化为 JSON 数组。worker 保留内部模式名作为 SSE 名，所以浏览器抓包看到的是：

```text
event: messages
data: [{...message chunk...}, {...metadata...}]
```

不是 `event: messages-tuple`。JavaScript SDK 的 `StreamManager` 用 `messages` 分支消费这类帧，`MessageTupleManager` 再按消息 ID 累加 chunk。同一个 AI 消息的 `"你"`、`"好"` 两帧最终应成为一条“你好”，而不是两条气泡。没有稳定 ID 的片段无法可靠拼接，因此 ID 是增量协议的一部分，不只是 React 的 `key`。

### `updates`：节点写入不是完整状态

`updates` 的形状近似下面的示意；节点名取决于实际图，不应在业务代码中硬编码为这个例子：

```json
{
  "agent": {
    "messages": [{"type": "ai", "id": "m-1", "content": "完成"}],
    "artifacts": ["/mnt/user-data/outputs/report.md"],
    "todos": [{"content": "生成报告", "status": "completed"}]
  }
}
```

它表达“这个节点向 channel 写了什么”，不是“此刻所有 channel 的完整值”。因此前端不能把整个对象浅赋值到 ThreadState。`frontend/src/core/threads/stream-state.ts::reduceThreadStateUpdates` 明确镜像相关 reducer 语义：

- `title` 用新字符串替换；
- `artifacts` 与已有路径去重合并；
- `todos: null` 表示未触碰，数组（包括空数组）才替换；
- `goal: null` 保留原值，合法 Goal 才更新；
- `messages` 故意不在这里处理。

最后一条是防重复的关键：同一消息同时出现在 `updates` 和 `messages` 流中时，消息只交给 SDK 的 chunk 管理器。若 `onUpdateEvent` 再把 `updates.messages` 应用一遍，就会绕过片段拼接并产生重复消息。`frontend/tests/unit/core/threads/incremental-stream-state.dom.test.tsx` 固定了“没有 `values` 帧也能更新渲染状态，同时消息不重复”的行为。

### `values`：支持的完整快照，但不是当前 Web 热路径

如果请求没有提供 `stream_mode`，后端 `normalize_stream_modes(None)` 仍默认选择 `values`；其他客户端也可以显式请求它。一个根图 `values` 帧会携带当时完整的 ThreadState，SDK 对根帧采用整体替换语义，因此它适合建立或校准状态基线。

但当前 Web 聊天在 `frontend/src/core/api/stream-mode.ts::forceChatRunStreamOptions` 中强制使用：

```ts
export const CHAT_RUN_STREAM_MODES = [
  "messages-tuple",
  "updates",
  "custom",
] as const;
```

函数还会从模式集合删除 `values`。原因是完整状态会在每个图步骤重复携带越来越长的消息历史，既增加带宽与序列化成本，也会让一个错误归类的子图快照覆盖根线程视图。当前 Web 的方案不是“永远不要状态快照”，而是把快照与增量分工到不同时间点：

```text
进入页面：通过 state history 取得最近 Checkpoint 快照
运行期间：messages + updates + custom 增量更新
运行结束：SDK 重新读取最新 state history，校准最终状态
```

`hooks.ts` 给 `useStream` 传入 `fetchStateHistory: { limit: 1 }`，并注册 `onFinish`；SDK 在流成功结束后重新读取最新 head。因此当前设计仍同时需要“权威快照”和“低延迟增量”，只是权威快照主要通过状态历史接口与 Checkpoint 边界取得，而不是在每一步都发 `values` SSE。

如果未来启用 `stream_subgraphs`，worker 会把子图帧命名为 `values|<namespace>`，SDK 将其路由到子代理状态，而不是把它当成裸 `values` 替换根线程。`backend/tests/test_worker_stream_subgraph_namespace.py::test_subagent_values_snapshot_is_never_published_as_bare_values` 固定了这个隔离不变量。当前 Web 根本不请求 subgraph 流，子代理进度使用下一节的根级 `custom` 事件。

### `end`：流结束，不是状态成功

worker 完成第 21 课所述的终结阶段后调用 `bridge.publish_end(run_id)`。`services.py::sse_consumer` 把桥中的 `END_SENTINEL` 转为：

```text
event: end
data: null
```

然后结束 HTTP 生成器。SDK 的异步迭代随之结束，执行成功回调、重新读取最终状态并把 `isLoading` 设回 `false`。`end` 不携带 `success`/`error` 结论；Run 结果仍应从 Run 状态或最终持久数据读取。反过来，持久 Run 先进入终态但尾部帧尚未发布时，消费者仍应等待真正的 `end`，不能看到终态就提前关闭流。

## 当前 Web 请求到底订阅了什么

`hooks.ts::useThreadStream` 没有直接填写 `streamMode`，但读取 `thread.messages` 会让 SDK 跟踪消息模式，`onUpdateEvent` 与 `onCustomEvent` 又要求相应回调模式。DeerFlow 仍在 API Client 边界再次调用 `forceChatRunStreamOptions`，确保创建流和重连流都使用同一组受支持的增量模式，并拒绝 `messages`、`events` 等 Gateway 不能如实提供的模式。

浏览器中的请求体应包含近似下面的字段：

```json
{
  "assistant_id": "lead_agent",
  "input": {"messages": ["..."]},
  "stream_mode": ["messages-tuple", "updates", "custom"],
  "stream_subgraphs": false,
  "on_disconnect": "continue"
}
```

字段值以你的实际抓包为准；例如连接策略会受 SDK 的重连设置影响。不要根据示意伪造实验记录。服务端 `RunCreateRequest` 验证公开模式，`run_agent` 再执行下面的转换与发布：

```text
公开 messages-tuple
  → LangGraph graph.astream(stream_mode="messages")
  → serialize((chunk, metadata), mode="messages")
  → StreamBridge event="messages"
  → SSE event: messages
  → SDK MessageTupleManager 按 id 拼接
```

`updates` 与 `custom` 不需要别名转换。StreamBridge 为正常数据事件分配递增 `id`，`sse_consumer` 只负责把桥事件格式化为 SSE；它不会解释消息、执行 reducer 或修改 React 状态。这条职责边界让同一个后台 Run 可以被创建流、join 观察者和 `/wait` 复用。

## 子代理为什么走 `custom`，而不是混入主消息

`task` 工具运行子代理时，`task_tool.py` 通过 LangGraph stream writer 发送一组结构化事件：

```text
task_started
  → task_running（可多次，携带 AI/Tool 步骤、message_index、usage）
  → task_completed | task_failed | task_cancelled | task_timed_out
```

它们都使用提供商的 `tool_call_id` 作为外部 `task_id`，这样主 Agent 的工具调用、ToolMessage、SSE 卡片和持久步骤可以相关联。进程内部用于执行所有权的 `execution_id` 不应该泄漏成 UI 相关键。

`hooks.ts::onCustomEvent` 先把合法的 `task_started`、`task_running` 转成 Subtask 更新；对于 `task_running`，它既保留最新消息，也按 `message_index` 把步骤追加到时间线。终态并不只依赖一闪而过的 custom 帧：父 Agent 最终会收到一个带结构化 `subagent_status` 等字段的 ToolMessage，`message-list.tsx` 通过 `parseSubtaskResult` 从正常消息历史重建完成或失败状态。后端还把识别出的 `task_*` 帧批量保存为 `subagent.start/step/end` 运行事件，供刷新后展开历史步骤。

这套分工解决了两个不同需求：

- `custom` 提供运行中的低延迟进度，不污染主对话的 messages channel；
- ToolMessage 和 RunEventStore 提供完成结果与可恢复历史，不要求用户恰好在线接住终态帧。

当前 Web 提交明确不设置 `streamSubgraphs`。否则子代理内部的完整消息和状态也会进入客户端，既可能泄漏内部上下文，又会与已经设计好的 `task_*` 卡片重复。需要展示的是任务进度与边界化结果，而不是把子代理对话冒充主 Agent 对话。

## 收到很多帧，不等于 React 必须渲染很多次

模型可能每几十毫秒产生一个 token。若每帧都触发消息归并、Markdown 解析、分组和组件重渲染，前端主线程会把大量时间消耗在中间态上。DeerFlow 当前有两层合并节奏，它们解决的不是同一个问题。

第一层位于 SDK 订阅。`useThreadStream` 显式设置：

```ts
throttle: true
```

在当前 SDK 实现中，布尔 `true` 用零延迟定时器把同一宏任务附近的通知合并。代码特意不使用数值延迟，因为 SDK 的数值实现是尾随防抖：若连续事件总比窗口更快，定时器会不断重置，UI 可能长期得不到更新。

第二层位于 DeerFlow 自己的 `useCoalescedStreamMessages`。流式期间，它以 `STREAM_RENDER_COALESCE_MS = 80` 毫秒维护一个面向渲染的消息快照：超过间隔时立即刷新，否则只安排一个剩余时间的尾部刷新，不因新 token 重置截止时间。这样高密度流仍能持续前进，而昂贵的消息合并与渲染最多按约 80 毫秒一次运行。流停止时它取消定时器并直接交出最新数组，不能为了节流丢掉最后几帧。

因此抓包中看到 100 个 `messages` 帧而 React 只渲染十几次是正常现象。网络事件完整性、SDK 内部状态推进和界面刷新频率是三件事；节流只能减少通知与渲染，不能删除协议状态。

## 乐观消息怎样不与服务端回显重复

用户按下发送键后，如果必须等到 Gateway 返回第一帧才显示自己的输入，界面会显得迟钝。`sendMessage` 先创建本地乐观 HumanMessage，同时把同一个客户端生成 ID 放进实际提交：

```text
本地显示：id = local-human-<uuid>
提交请求：id = local-human-<uuid>
服务端可见回显：id = local-human-<uuid>__user
```

`__user` 后缀来自动态上下文处理中间件的消息重建。`message-order.ts::messageIdentity` 只对 HumanMessage 归一化这个保留后缀，所以本地 `X` 与服务端 `X__user` 被视为同一个可见输入；ToolMessage 则优先用 `tool_call_id`，其他消息使用自己的 `id`。

最终界面同时面对三类来源：

```text
历史接口：已持久化、可分页的权威历史
实时流：SDK 已拼接的当前 Checkpoint/消息尾部
乐观消息：当前客户端尚未确认的本地输入
```

`mergeMessages` 按稳定身份去重：同一身份的实时副本可以刷新历史内容，已知的服务端顺序 `deerflow_seq` 决定位置，没有服务端位置的流式尾部保持在尾端，最后才加入乐观消息。服务端回显出现后，本地乐观副本被移除。

这里还有一个容易忽略的节流竞态：原始 SDK 消息数组可能已经收到服务端 HumanMessage，而面向用户的 80 毫秒渲染快照还没刷新。若此时立刻删除乐观消息，界面会短暂看不到用户输入。因此清理逻辑观察的是 `renderMessages`，即用户实际看到的合并快照；身份匹配优先，渲染快照中 HumanMessage 数量增加只作兼容回退。`frontend/tests/unit/core/threads/local-turn-order.dom.test.tsx` 覆盖了这种“服务端回显先到内部状态、后到渲染快照”的时序。

可以把防重复不变量总结为三条：

1. 消息片段必须用稳定 message ID 在 SDK 中拼接；
2. 历史、实时流与乐观消息必须用同一套 UI 身份规则合并；
3. 乐观副本只能在用户可见的服务端副本就位后撤下。

只用文本内容或时间戳去重都不可靠：用户可能连续发送相同文本，服务器时间与客户端时间也可能偏移。

## 一份可对照的 SSE 样例

下面是根据当前协议整理的**删减示意**，用于说明帧形状，不是一次真实实验记录。节点名、ID、消息元数据和事件顺序会随模型、工具和图路径变化；你的课程产出必须来自实际 Network 抓包。

```text
event: metadata
data: {"run_id":"run-123","thread_id":"thread-abc"}
id: 1780000000000-0

event: messages
data: [{"type":"AIMessageChunk","id":"ai-1","content":"正在"},{"langgraph_node":"model"}]
id: 1780000000010-1

event: messages
data: [{"type":"AIMessageChunk","id":"ai-1","content":"处理"},{"langgraph_node":"model"}]
id: 1780000000020-2

event: custom
data: {"type":"task_started","task_id":"call-7","description":"检索资料","model_name":"..."}
id: 1780000000030-3

event: updates
data: {"tools":{"artifacts":["/mnt/user-data/outputs/report.md"]}}
id: 1780000000040-4

event: end
data: null
```

若另一个客户端显式请求 `values`，还可能看到：

```text
event: values
data: {"messages":[...],"artifacts":[...],"todos":[...],"goal":null,"title":"..."}
```

不要把这行补进当前 Web 抓包来“满足大纲”；看不到 `values` 正是当前增量设计的预期结果。也不要假设 `end` 前一定有某一种业务帧：一个启动失败或很早被取消的 Run 可能只有 metadata、error 和 end，甚至在某些恢复边界走 gap。

## 课程产出：SSE 事件样例与前端消费说明

启动应用后，用浏览器开发者工具记录一次真实 Web 对话。建议把整理后的结果保存为 `learn/outputs/lesson-22-sse-stream.md` 或你自己的学习记录；不要提交完整的 HTTP 存档（HAR），因为 Cookie、请求头、Prompt 和工具结果可能含有敏感信息。

### 抓包步骤

1. 打开 `http://localhost:2026`，进入浏览器开发者工具的 Network 面板并勾选 Preserve log。
2. 发送一个会产生多段文本的普通请求，筛选 `runs/stream`，确认请求是 `POST /api/langgraph/threads/{id}/runs/stream` 或经 nginx 重写后的等价路径。
3. 记录请求体中的 `stream_mode`、`stream_subgraphs` 和 `on_disconnect`；记录响应的 `Content-Type`、`Content-Location`、`Cache-Control` 与 `X-Accel-Buffering`。
4. 在 EventStream/Response 视图中记录前 10～20 个事件及最后的 `end`。只保留事件类型、ID、关键字段和短内容摘要，移除 Cookie、令牌、用户数据与长工具输出。
5. 切到允许子代理的运行模式，提交一个确实可拆分的任务，尝试观察 `custom` 中的 `task_started` 与 `task_running`。若当前模型或配置没有触发子代理，如实记录“未观察到”，不要伪造事件。
6. 对照前端 UI，标记每类事件造成的可见变化，并确认当前 Web 正常情况下没有 `values` 帧。

产出至少包含下面两张表：

| 序号 | SSE `event` | `id` | 关键载荷 | 对应 UI 变化 |
| --- | --- | --- | --- | --- |
| 1 | `metadata` | 实际值 | `run_id`、`thread_id` | 新 Run/Thread 开始 |
| … | … | … | … | … |
| N | `end` | 实际值或空 | `null` | 流停止，最终状态刷新 |

| 前端阶段 | 输入来源 | 合并规则 | 关键源码 |
| --- | --- | --- | --- |
| 初始基线 | state history | 最近 Checkpoint 快照 | `hooks.ts::useThreadStream` |
| 文本流 | `messages` SSE | SDK 按 message ID 拼接 | SDK `MessageTupleManager` |
| 非消息状态 | `updates` SSE | 按 ThreadState reducer 语义折叠 | `stream-state.ts::reduceThreadStateUpdates` |
| 子代理进度 | `custom` SSE | 按 `task_id` 累积步骤 | `hooks.ts::onCustomEvent` |
| 最终校准 | 流结束后的 state history | 最新 head 替换/补齐 | SDK `onSuccess` + `onFinish` |
| 显示列表 | history + live + optimistic | 稳定身份去重并保持可信顺序 | `message-order.ts::mergeMessages` |

最后用不超过 300 字回答两个问题：“为什么当前 Web 不在每一步请求 `values`，却仍然需要状态快照？”以及“为什么不能在收到任意 AI 文本后立即删除乐观 HumanMessage？”

## 可选代码验证

时间有限时，只运行下面的局部测试。前端第一组验证请求模式、增量状态和两层刷新节奏；后端第二组验证 SSE 格式与公开/内部模式映射。它们不需要真实模型调用。

```powershell
cd frontend
pnpm rstest run tests/unit/core/api/stream-mode.test.ts tests/unit/core/threads/incremental-stream-state.dom.test.tsx tests/unit/core/threads/stream-throttle.test.ts tests/unit/core/threads/coalesce.test.ts

cd ../backend
uv run pytest tests/test_gateway_services.py -k "format_sse or normalize_stream_modes or to_langgraph_stream_modes" -q
```

若要进一步验证子图隔离，可选运行：

```powershell
cd backend
uv run pytest tests/test_worker_stream_subgraph_namespace.py -k "subagent_values_snapshot_is_never_published_as_bare_values or root_custom_event_is_persisted" -q
```

代码深挖按收益排序：先读 `frontend/src/core/api/stream-mode.ts` 和 `frontend/src/core/threads/hooks.ts::useThreadStream`，确认浏览器请求与三类回调；再读 `frontend/src/core/threads/stream-state.ts` 与 `message-order.ts::messageIdentity/mergeMessages`，确认状态和消息为何采用不同合并规则；最后只读 `worker.py::_stream_once/_publish_stream_item` 与 `services.py::format_sse/sse_consumer`，把生产者、桥和 HTTP 格式化边界接起来。无需逐行阅读完整 worker、Redis Bridge 或第 23 课的重连恢复算法。

面试验收：能在 2～3 分钟内画出 `graph.astream → StreamBridge → SSE → SDK → Hook → React`，准确解释公开 `messages-tuple` 为什么在抓包中是 `event: messages`；能说明 `values`、`updates`、`messages` 和 `custom` 的载荷及合并语义，并指出当前 Web 为什么只流式订阅后三类；能解释 `throttle: true` 与 80 毫秒渲染快照各自控制什么；能用客户端 ID、`__user` 归一化和渲染快照说明乐观消息为什么既不会重复，也不会提前闪失。
