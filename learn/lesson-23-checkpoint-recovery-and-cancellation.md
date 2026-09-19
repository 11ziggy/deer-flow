# 第 23 课：Checkpoint、恢复与取消

第 22 课说明了一个活跃 Run 怎样把增量事件送到浏览器。本课从连接断开、用户停止和历史重生成三个故障/控制场景切入，回答一个可靠性问题：**实时连接或当前执行被打断后，DeerFlow 怎样恢复可见状态，同时避免把旧 Run、旧 Checkpoint 和新的执行分支混成一条时间线？** 它位于请求主线的“流中断或取消 → Run 收口 → Checkpoint/消息历史恢复 → 必要时创建新 Run”一段。核心阅读约 45～55 分钟。

前置知识是第 7 课对 Run、Checkpoint、Event 的区分，第 8 课的状态与 reducer，以及第 21、22 课的 Run 终态和 SSE。必须掌握：页面刷新恢复的不是旧 HTTP 连接或 Python 协程；普通取消与回滚取消保留的状态不同；Checkpoint 是状态恢复点，不是执行快照；历史重生成会基于一个安全的旧 Checkpoint 创建新 Run；取消已经被接纳后仍必须完成 Finalization。多实例所有权、租约和取消请求的跨 worker 竞争留到第 24 课，通用异常、重试和可观测性留到第 25 课。

## 先分开三种“恢复”

“恢复”在本课中可能指三件完全不同的事。先分清恢复对象，后面的机制才不会混乱。

| 场景 | 真正要恢复的对象 | 当前机制 | 不会发生什么 |
| --- | --- | --- | --- |
| 网络闪断或运行中刷新页面 | 对活跃 Run 的观察，以及用户界面（UI）的可信状态 | 用 Run ID 重新加入流；有准确游标时用 `Last-Event-ID` 重放；缺帧时加载持久状态再续接 | 不创建第二个 Run，不从 Checkpoint 复制出旧协程 |
| 用户停止当前执行 | Run 的明确终点，以及保留或撤销本轮状态的决定 | `action=interrupt` 保留已提交的当前状态；`action=rollback` 尝试恢复运行前状态；两者都继续终结 | 不把关闭浏览器等同于可靠取消，也不会撤销已发生的外部副作用 |
| 对历史回答执行“重新生成”或“编辑并重试” | 从某个旧对话边界开始一条新执行 | 找到目标用户消息之前的已安定 Checkpoint，清理输入后创建一个新 Run | 不复活旧 Run，不在旧 Run 内继续推理，不直接改写历史 Checkpoint |

可以把恢复层次压缩成下面这张图：

```text
连接仍可续接：Run 不变 ── Last-Event-ID / join ──→ 补齐或继续 SSE
事件已经缺失：Run 不变 ── durable state + message feed ──→ 重建 UI 后继续观察
用户普通取消：Run 收口 ── 保留最新已提交 Checkpoint ──→ interrupted
用户回滚取消：Run 收口 ── 恢复 pre-run state ──→ error("Rolled back by user")
历史重新执行：旧 Run 不变 ── 选择旧 Checkpoint ──→ 创建新 Run
```

其中最重要的不变量是：**SSE 负责观察过程，Checkpoint 负责恢复状态，Run 负责记录一次执行的结论。三者可以关联，但不能互相替代。**

## 页面刷新后，系统怎样重新接上一个活跃 Run

当前 Web 在 `frontend/src/core/threads/hooks.ts::useThreadStream` 中配置：

```ts
useStream({
  threadId: onStreamThreadId,
  reconnectOnMount: true,
  fetchStateHistory: { limit: 1 },
  // ...
})
```

提交时 Hook 还传入 `streamResumable: true`。这里容易产生误解：保存重连所需 Run ID 的是 SDK 在 `reconnectOnMount: true` 下启用的会话存储；`streamResumable: true` 会让 SDK 默认选择 `on_disconnect=continue`。`frontend/src/core/api/stream-mode.ts::sanitizeRunStreamOptions` 在真正发 HTTP 请求前删除 `streamResumable`，因为 Gateway 并不实现对应的请求选项，但保留已经算出的断线策略。真正的事件续接依赖 SSE 的 `Last-Event-ID`，不是后端接收一个 `stream_resumable=true` 开关。

### 刷新时先恢复基线，再恢复实时尾部

一次运行中刷新可以按下面的顺序理解：

```text
刷新前
  SDK 在 sessionStorage 保存 lg:stream:<thread_id> = <run_id>

页面重新挂载
  ├─ fetchStateHistory(limit=1) 读取最近 Checkpoint 状态
  ├─ useThreadHistory 读取持久消息 feed
  └─ reconnectOnMount 取出 run_id
        ├─ Run 已终态：清掉重连键，不再加入旧流
        └─ Run 仍活跃：GET .../runs/{run_id}/stream 加入观察流

加入后
  ├─ 接收新的 messages / updates / custom
  ├─ 若客户端掌握准确游标，可用 Last-Event-ID 续接
  └─ Run 终结后收到 end，再刷新最终状态
```

`frontend/src/core/api/api-client.ts::shouldSkipReconnect` 会先查询 Run。若它已经是 `success`、`error`、`timeout` 或 `interrupted`，包装器直接结束重连并清理旧键；历史消息和 Checkpoint 仍由持久读取恢复。这个预检防止一个已经终结、且 StreamBridge 已清理的 Run 把页面永久卡在加载状态。

若 Run 仍活跃，加入接口是观察面，而不是第二个创建请求。`backend/app/gateway/routers/thread_runs.py::_stream_existing_run` 调用：

```python
sse_consumer(
    bridge,
    record,
    request,
    run_mgr,
    apply_on_disconnect=False,
)
```

因此刷新后新观察连接再次断开，不会继承创建者的 `on_disconnect=cancel` 意图而取消 Run。当前 Web 的正常创建流本身使用 `on_disconnect=continue`，所以刷新只会丢掉旧观察连接，后台 Run 继续执行。

### 准确游标续接与整页刷新并不完全相同

SSE 协议允许掌握最近事件 ID 的客户端在重新加入时带上：

```text
Last-Event-ID: <最后一帧的 id>
```

`backend/app/gateway/services.py::sse_consumer` 把该值传给 `StreamBridge.subscribe()`；Memory 与 Redis Bridge 都会从它之后开始发送仍保留的帧。DeerFlow 的 `joinStream(..., {lastEventId})` 和 `gap` 恢复后的再次加入都能走这条路径。这是精确的增量重放；但是否在任意网络错误后自动发起该请求，还取决于客户端是否保存游标并执行重连，不能仅凭后端支持作保证。

整页刷新后，浏览器会话只保存 Run ID，并不保存刷新前最后一帧的准确游标。当前 SDK 的挂载重连会用 `-1` 作为“没有已知游标”的哨兵。Memory Bridge 对这种未知 ID 从仍保留的最早事件重放，Redis Bridge 通常从当前尾部继续等待；因此整页刷新不能只靠 SSE 还原刷新前的所有中间 UI。它必须先用最近 Checkpoint 和持久消息 feed 建立基线，再把重新加入的流当作实时尾部。重复到达的消息则继续依赖第 22 课的稳定消息身份去重。

这也解释了为什么“页面刷新后还能看到对话”不等于“所有 token 帧都被可靠重放”：持久消息与 Checkpoint 可以恢复已经提交的状态，但尚未进入 Checkpoint/事件存储的瞬时片段可能只存在于旧连接中。

## 缓冲区追不上时，`gap` 不是正常结束

StreamBridge 只保留有界事件。精确的 `Last-Event-ID` 已早于当前保留水位，或者一个慢消费者在运行中被缓冲区甩开时，Bridge 不会从中间随便补一段并伪装成完整重放，而是返回流缺口（stream gap）。`services.py::sse_consumer` 将它转换为一个没有事件 ID 的控制帧：

```text
event: gap
data: {
  "code": "stream_replay_gap",
  "run_id": "...",
  "requested_event_id": "...",
  "earliest_available_event_id": "...",
  "latest_available_event_id": "...",
  "recovery": "reload_durable_state"
}
```

这个分支刻意不取消 Run。`gap` 表示“观察过程不完整”，不是“执行失败”或“流已经正常结束”。

`frontend/src/core/api/api-client.ts::recoverStreamReplayGaps` 对初始创建流和加入流都做同一种恢复：

1. 把 `gap` 转成内部 `stream_replay_gap` custom 事件；
2. 清理旧的重连元数据；
3. 调用 `threads.getState(thread_id)` 读取持久 Checkpoint 状态；
4. 向 SDK 注入一个 `values` 快照，重新校准 UI；
5. 若服务端给出 `latest_available_event_id`，从该保留尾部之后再次加入；没有尾部则不带游标加入；
6. 最多恢复五次，避免持续 `gap` 形成无限循环。

`hooks.ts::onCustomEvent` 收到 `stream_replay_gap` 后，还会清空乐观消息、临时历史桥、待替换消息和子代理临时状态，刷新历史缓存，并提示用户已经从持久状态恢复。这里临时清理很关键：如果只注入一个 `values`，旧连接留下但无法证实的局部 UI 仍可能与权威快照混在一起。

恢复链因此是：

```text
准确游标仍在缓冲区
  → 只补发游标之后的 SSE

准确游标已经过期
  → gap
  → 清临时 UI
  → 读取持久 Checkpoint + 消息历史
  → 从保留尾部继续观察同一个 Run
```

## Checkpoint 恢复的是状态，不是执行现场

Checkpoint 保存图状态及其版本关系，例如消息、Artifact、Todo、Goal 和中间件扩展 channel。它不保存 Python `asyncio.Task` 的调用栈、正在等待的网络连接或已经进入第三方系统但尚未返回的请求。因此：

- 页面刷新时，旧 worker 若仍活着，就继续执行原 Run；新页面只是恢复状态并重新观察；
- Gateway 进程崩溃后，新进程没有办法从某条 Python 指令原地继续，遗留活跃 Run 会按第 21 课的孤儿规则收口；
- 从旧 Checkpoint 重新运行一定是一个新 Run，模型和工具可能产生不同结果；
- 已经发生的邮件发送、数据库写入等外部副作用不会因为 Checkpoint 回滚自动撤销；
- 已经通过 SSE 显示但尚未进入 Checkpoint 的局部模型文本，不保证能从 Checkpoint 找回。

当前代码要求线程状态读取经过 `backend/packages/harness/deerflow/runtime/checkpoint_state.py::CheckpointStateAccessor`。它负责按线程实际 schema 物化完整状态，并兼容 `full` 与 `delta` 两种 Checkpoint channel 模式；不能用原始 checkpoint blob 是否含有 `messages` 来判断状态是否完整。

## 普通取消与回滚取消保留什么

Gateway 的显式入口是：

```text
POST /api/threads/{thread_id}/runs/{run_id}/cancel
  ?action=interrupt|rollback
  &wait=true|false
```

当前 Web 的 Stop 按钮通过 SDK `thread.stop()` 走默认普通取消。`backend/packages/harness/deerflow/runtime/runs/manager.py::RunManager.cancel` 在本地拥有 Run 时设置 `abort_action` 与 `abort_event`，取消正在运行的 task，并把 Run 引向终态；worker 捕获取消后统一进入 `_finish_cancellation()`。重复取消一个已经 `interrupted` 的 Run 按成功处理，避免 Stop 重试变成 409。

两种动作的区别不是“是否停止”，而是“停止后哪个 ThreadState 成为最新状态”。

| 项目 | `interrupt` | `rollback` |
| --- | --- | --- |
| 目的 | 停止后保留本轮已经提交的状态 | 停止后尝试撤销本轮图状态 |
| Run 终态 | `interrupted` | 当前实现为 `error`，错误为 `Rolled back by user` |
| Checkpoint | 保留取消时已经持久化的最新状态 | 恢复 worker 在运行前捕获的完整状态 |
| 部分 SSE 文本 | 只有已进入持久状态/事件历史的部分可恢复 | 回滚后的权威状态不应保留本轮消息，但旧实时帧必须由最终状态校准 |
| 外部工具副作用 | 不撤销 | 也不撤销；只恢复 DeerFlow 的图状态 |
| 失败边界 | 仍需完成终结和 END | 捕获或恢复失败时保持失败结论，不用空/半状态冒充成功回滚 |

### 回滚点为什么必须在运行前完整捕获

`worker.py::_capture_rollback_point` 在图开始修改 ThreadState 前保存一个不可变 `RollbackPoint`：

```python
@dataclass(frozen=True)
class RollbackPoint:
    config: dict[str, Any]
    state_values: dict[str, Any]
    messages: tuple[Any, ...]
    metadata: dict[str, Any]
    pending_writes: tuple[tuple[str, str, Any], ...]
```

如果捕获抛错，worker 将 `snapshot_capture_failed=True`，后续回滚会拒绝恢复；它不会把“读不到旧状态”解释为“旧状态为空”。若新 Thread 在运行前根本没有 Checkpoint，回滚则删除本轮产生的线程 Checkpoint，把图状态重置为空。

真正恢复由 `_rollback_to_pre_run_checkpoint` 通过一个只写状态、不调度 Agent 的 mutation graph 完成。`full` 与 `delta` 模式采用不同路径：

- `full` Checkpoint 含完整 channel 值，可以从运行前 Checkpoint 建立恢复分支；`messages` 用 `Overwrite` 整体替换，其他 channel 从父 Checkpoint 继承；
- `delta` Checkpoint 的消息依赖祖先写入历史。直接从旧点分叉可能把已经放弃的兄弟分支写入重新播放回来，因此当前实现把运行前物化出的全部 channel 线性写到当前 head，reducer channel 同样使用替换语义，并重置只存在于新 head 的 channel。

两条路径都使用线程的有效 Agent schema，保留由 Middleware 扩展的 channel 和运行前 pending writes。回滚因此不是简单地“把 checkpoint_id 指针改回去”，而是写出一个新的、可物化且处于空闲状态的 head。

## 为什么取消后仍必须 Finalization

取消信号只说明“不再继续主执行”，并不说明系统已经留下可查询、可重连的一致结果。`worker.py::run_agent` 在 `finally` 中仍按适用路径完成这些工作：

1. 回滚取消执行状态恢复；失败的编辑重放也自动恢复运行前状态；
2. 刷新已产生的子代理与 RunJournal 事件；
3. 写交付收据、完成统计和最终 Run 状态；
4. 必要时为被中断的首轮对话补标题，并同步 Thread 元数据；
5. 调用 Run 完成钩子和任务停止观察者；
6. 清除进程内 `finalizing` 屏障；
7. 发布 `end`，最后清理 journal、sandbox lease、graph 和 Run 记录引用。

`RunRecord.finalizing` 仍只是进程内收尾屏障，不是持久 Run 状态。普通取消或回滚期间，后来的同 Thread Run 不能在旧 Run 恢复 Checkpoint、补标题或执行停止钩子时随意重叠。否则可能出现下面的竞态：

```text
旧 Run 收到 rollback
  ├─ 新 Run 先读取了尚未恢复的 head 并开始执行
  └─ 旧 Run 随后写回 pre-run state，覆盖新 Run 的起点或结果
```

API 的 `wait=false` 只表示取消请求已接受，通常返回 202；`wait=true` 才等待本地 task 或跨 worker 可观察的 END，完成后返回 204。无论调用者是否等待，worker 都不能跳过终结。前端也不能看到 Run 已是终态就自行伪造 `end`：最终恢复快照、标题和其他尾部事件可能仍在路上。

编辑并重试还有一条额外保证：如果新编辑 Run 失败、超时或被取消，worker 会恢复该 Run 之前的 Checkpoint，并在 `end` 前发布恢复后的 `values`。这样 UI 不会永久停在一个并未成功取代原回答的临时编辑分支上。

## 从历史消息重新生成，实际创建了什么

前端的普通重新生成先调用：

```text
POST /api/threads/{thread_id}/runs/regenerate/prepare
```

编辑并重试调用：

```text
POST /api/threads/{thread_id}/runs/edit-regenerate/prepare
```

两者都不是直接启动模型。Gateway 先计算并返回三类材料：清理后的 HumanMessage 输入、旧 Checkpoint 坐标，以及关联旧回答的 metadata；随后 `hooks.ts::submitPreparedReplay` 才把它们交给正常的 `thread.submit()`，创建一个新 Run。

普通重新生成只允许当前最新的可见 AssistantMessage。若一次响应在模型流式输出中被中断、局部文本尚未来得及进入 Checkpoint，后端还允许通过最新 HumanMessage 上由服务端写入的 `run_id` 验证它确实属于本线程且 Run 状态为 `interrupted`，再为这次最新的局部回答准备重生成。编辑并重试限制更严：只能编辑最新已完成的用户轮次，源 AssistantMessage 必须是无 tool call 的终结文本，源 Run 必须成功，而且线程不能有活跃 Goal。

### 为什么不能按全局时间随便挑一个旧 Checkpoint

一次重生成会产生 Checkpoint 分支。同一 Thread 的全局时间顺序里可能同时存在原回答分支、上一次重生成分支和当前 head。若只找“目标 HumanMessage 之前时间最近的 Checkpoint”，就可能选中兄弟分支。

`backend/app/gateway/checkpoint_lineage.py::find_checkpoint_before_message` 从当前 head 沿 `parent_config` 向上走，直到找到目标 HumanMessage 尚未出现的祖先。可用的重放基点还必须满足：

- 有可寻址的 `checkpoint_id`；
- 不是只写 Run duration 的元数据 Checkpoint；
- `next` 为空，即没有待执行节点，是已安定（settled）的状态；
- parent 链没有环、断链、目标错位或深度超限。

“已安定”条件防止选择运行中间的 Checkpoint。中间点可能已经拥有下一个节点即将消费的 pending writes；从那里重放会把本来要替换的用户输入再次写入。只有旧版本明确没有 `parent_config` 时，代码才使用有界的时间顺序兼容扫描；已存在但损坏的 lineage 会失败关闭，不会悄悄跳到另一个分支。

准备成功后，返回的数据近似为：

```json
{
  "input": {"messages": [{"type": "human", "id": "...", "content": "..."}]},
  "checkpoint": {"checkpoint_ns": "", "checkpoint_id": "..."},
  "metadata": {
    "regenerate_from_message_id": "...",
    "regenerate_from_run_id": "...",
    "regenerate_checkpoint_id": "..."
  },
  "target_run_id": "..."
}
```

HumanMessage 会恢复原始用户文本并移除服务端动态上下文的内部改写；若当前标题后来被用户手工重命名，准备结果还会把当前标题带入新 Run，避免从旧 Checkpoint 恢复出过期标题。前端在新 Run 期间乐观隐藏被替换的消息；准备失败或流错误会清理临时遮罩，完成后的持久可见性则由后端记录的新旧 Run 关系决定。

Checkpoint 模式仍影响新 Run 的接线方式：`full` 模式可按 LangGraph 的普通分支语义从旧 Checkpoint 启动；`delta` 模式会在 worker 开始图执行前，把选中的旧状态线性改写到当前 head，再移除 `checkpoint_id` 选择器。两者在用户层面的目标相同：新 Run 看到目标用户轮次之前的状态，但旧 Run 记录和历史 lineage 仍然存在。

## 四个场景的恢复行为对照

面试时可以用下面这张表快速说明边界：

| 操作 | Run 身份 | Run 结果 | 最终 ThreadState | SSE/前端恢复 | 主要风险 |
| --- | --- | --- | --- | --- | --- |
| 活跃时刷新页面 | 不变 | 继续走向原终态 | 由原 Run 继续推进 | 持久基线 + join；准确游标可重放，缺帧走 `gap` 恢复 | 把刷新误当取消，或只靠瞬时帧恢复 UI |
| 普通取消 | 原 Run 收口 | `interrupted` | 保留已提交的取消时状态 | 仍等待尾部事件与 `end`，再刷新历史 | 局部 token 未落盘；外部副作用不回退 |
| 回滚取消 | 原 Run 收口 | `error` | 尝试恢复运行前完整状态 | Finalization 中恢复，随后 `end` | 捕获/恢复失败；外部副作用仍存在 |
| 从旧回答重新生成 | 创建新 Run | 新 Run 独立判定 | 从安全旧基点产生新 head | 前端暂时遮罩旧回答，完成后用历史校准 | 选错兄弟/中间 Checkpoint，或把新 Run 误说成续跑旧 Run |

## 课程产出：恢复行为对比表

在本地启动 DeerFlow 后，完成四次实验，建议保存到 `learn/outputs/lesson-23-recovery-behavior.md` 或自己的学习记录。不要只记录最终页面截图；每个场景都要同时记录 Run、流和状态三层证据。

### 实验步骤

1. **运行中刷新**：提交一个会持续数十秒的任务，在至少收到两帧后刷新页面。记录刷新前 Run ID、刷新后的 Run 查询、加入流请求、`Last-Event-ID`（如有）和最终结果，确认没有创建第二个 Run。
2. **普通取消**：提交一个会调用工具的任务，出现部分进度后按 Stop。记录取消请求的 `action`、202/204、最终 Run 状态、取消前后 Checkpoint 消息，以及最后的 `end`。
3. **回滚取消**：通过 API 或支持该动作的客户端以 `action=rollback&wait=true` 取消。对比运行前、运行中和取消后的最新 Checkpoint，确认 Run 结论与状态恢复是两件事。只在可丢弃的本地实验 Thread 上进行；不要用有真实外部写入副作用的工具验证回滚。
4. **历史重新生成**：对最新回答执行重新生成，再对最新用户消息执行编辑并重试。记录 prepare 响应中的旧 Checkpoint、新 Run ID、metadata、旧/新 Run 状态和最终可见消息。再尝试重生成非最新回答，记录服务端拒绝而不是绕过限制。

产出表至少包含：

| 场景 | 原 Run ID/状态 | 是否新建 Run | 请求或重连游标 | 使用的 Checkpoint | 最终状态摘要 | 是否收到 `gap`/`end` | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 运行中刷新 | 实际值 | 否 | 实际值 | head/实际 ID | … | … | … |
| 普通取消 | 实际值 | 否 | `action=interrupt` | 取消时已提交 head | … | … | … |
| 回滚取消 | 实际值 | 否 | `action=rollback` | pre-run / 恢复后新 head | … | … | … |
| 重新生成 | 原值 + 新 Run ID | 是 | prepare metadata | 目标 Human 前 settled Checkpoint | … | … | … |

最后用不超过 300 字回答：“为什么 Checkpoint 能恢复 ThreadState，却不能恢复旧协程？”以及“为什么取消接口返回 202 后，系统仍不能允许同 Thread 的后续执行立刻覆盖正在 Finalization 的状态？”

## 可选代码验证

时间有限时，下面的局部测试足以验证本课的四条关键不变量，不需要真实模型或浏览器。

```powershell
cd backend
uv run pytest tests/test_stream_bridge.py -k "replays_after_last_event_id or evicted_last_event_id_yields_gap_before_partial_replay" -q
uv run pytest tests/test_gateway_services.py -k "sse_consumer_emits_gap_without_cancelling_run" -q
uv run pytest tests/test_run_worker_rollback.py -k "rollback_forks_pre_run_checkpoint_without_deleting_thread or rollback_linearizes_delta_restore_onto_cancelled_head or run_agent_rolls_back_failed_edit_replay_and_publishes_restored_values or interrupted_title_finalization_blocks_new_same_thread_run" -q
uv run pytest tests/test_thread_regenerate_prepare.py -k "returns_clean_input_and_base_checkpoint or prefers_checkpoint_lineage or skips_mid_run_replay_base_on_first_turn or supports_latest_interrupted_response_missing_from_checkpoint" -q

cd ../frontend
pnpm rstest run tests/unit/core/api/api-client.test.ts
```

这些测试分别证明：有效游标可续接、过期游标先报告 `gap` 而不是部分重放、`gap` 不取消 Run、full/delta 回滚产生可物化状态、失败编辑重放会在 END 前恢复 `values`、取消收尾会阻挡同 Thread 的危险重叠，以及重生成使用 lineage 上已安定的运行前基点。

代码深挖按收益排序：先读 `frontend/src/core/api/api-client.ts::recoverStreamReplayGaps/shouldSkipReconnect` 与 `frontend/src/core/threads/hooks.ts::useThreadStream`，确认刷新、缺帧和 UI 清理；再读 `backend/app/gateway/services.py::sse_consumer` 与 `backend/app/gateway/routers/thread_runs.py::_stream_existing_run/cancel_run`，确认加入流和取消的 HTTP 边界；然后读 `runtime/runs/worker.py::_finish_cancellation/_capture_rollback_point/_rollback_to_pre_run_checkpoint`，确认状态恢复与 Finalization；最后读 `checkpoint_lineage.py::find_checkpoint_before_message` 和 `thread_runs.py::_prepare_regenerate_payload`，确认历史重生成为什么不会随便选一个时间最近的 Checkpoint。无需在本课展开租约 CAS、跨 worker takeover 或完整事件审计，它们属于第 24、25 课。

面试验收：能在 3 分钟内区分“重连同一个 Run”“从持久状态重建 UI”“基于旧 Checkpoint 创建新 Run”；能解释准确 `Last-Event-ID`、`gap → durable state → rejoin` 和终态重连短路三条路径；能对比 `interrupt` 与 `rollback` 的 Run 状态、ThreadState 和副作用边界；能说明回滚点为什么必须运行前捕获、取消为什么仍需 Finalization；能沿 `prepare → lineage 上的 settled Checkpoint → clean HumanMessage → thread.submit → 新 Run` 讲清历史重生成，并指出 Checkpoint 绝不是协程快照。
