# 第 21 课：Run 生命周期

前四个阶段已经说明 Agent 怎样理解上下文、调用工具并管理复杂任务。本课进入运行时可靠性，回答一个更基础的问题：**一次 Run 从被 HTTP 请求接纳，到后台执行、终结并留下可查询记录，系统怎样保证每个阶段都有明确归宿？** 它位于请求主线的“Gateway 创建 Run → Runtime 后台执行 → 状态、事件与 Checkpoint 落地 → Run 结束”一段。核心阅读约 35～45 分钟。

前置知识是第 6 课的完整请求链和第 7 课的 Thread、Run、Checkpoint、Event 区分。本课必须掌握当前状态词汇、HTTP 生命周期与 Agent 生命周期的分离、`RunManager` 与 `RunStore` 的职责，以及正常完成、启动失败、执行失败、取消和进程重启分别怎样收口。SSE 帧格式、断线重连、Checkpoint 恢复、取消/回滚细节、幂等与多实例租约分别留到第 22～24 课；本课只讲这些机制在 Run 生命周期上的接口。

## 先校正状态词汇

大纲用 Pending、Running、Finalizing、Completed、Error、Interrupted 描述概念阶段；当前源码的真实枚举并不完全相同。`backend/packages/harness/deerflow/runtime/runs/schemas.py::RunStatus` 定义的是：

```python
class RunStatus(StrEnum):
    pending = "pending"
    running = "running"
    success = "success"
    error = "error"
    timeout = "timeout"
    interrupted = "interrupted"
```

因此，本课后面统一使用代码中的小写值。大纲的 Completed 对应成功终态 `success`，不是一个可查询的 `completed` 值；Finalizing 是“正在终结”的概念阶段，也不是 `RunStatus`。`RunRecord` 另有进程内布尔字段 `finalizing`，它主要为取消、编辑重放回滚等收尾建立同 Thread 屏障，不能把它当成持久状态 `finalizing`。枚举还保留 `timeout`；在本课核对的当前 `run_agent()` 显式分支中，正常成功写 `success`、异常写 `error`、普通中断写 `interrupted`，没有直接写 `timeout`，所以不要凭枚举补画一条当前并不存在的超时转换。

各状态的最小语义如下：

| 状态或阶段 | 当前含义 | 是否终态 |
| --- | --- | --- |
| `pending` | 已通过接纳并有 Run 记录，但后台执行器任务（worker）尚未越过启动屏障 | 否 |
| `running` | `try_start()` 已成功，worker 可以组装并执行 Agent | 否 |
| `success` | Agent 主流程成功，且最终交付检查没有把结果降级为错误 | 是 |
| `error` | 启动、执行、回滚、交付或孤儿恢复以失败收口；具体原因看 `error` 与 `stop_reason` | 是 |
| `interrupted` | 普通取消保留已写入的执行状态，并停止本次 Run | 是 |
| `timeout` | Run 状态词汇中的超时终态；本课所读 worker 没有展示进入它的主转换 | 是 |
| 终结阶段 | 刷新事件、写交付收据和完成统计、必要时补写 Checkpoint/标题、运行完成钩子、发布流结束 | 不是枚举状态 |

最小状态图应按代码画成下面这样。虚线表示“终态已经决定，但仍需做收尾”，不是另一个持久状态。

```text
                 worker 附着失败
              ┌──────────────────→ error
              │
创建并持久化 → pending ── try_start 原子成功 ──→ running
              │                                  ├── 正常完成 ──→ success
              └── 启动前取消 ──→ interrupted     ├── 异常/回滚 ─→ error
                                                 └── 普通取消 ──→ interrupted

success / error / interrupted
              ─ ─ → 终结阶段 ─ ─ → 发布结束标记（END）、延迟清理进程内记录
```

终态描述的是执行结论，不表示所有尾部动作已经完成。例如 Run 可以先在本地被判定为 `success`，随后才写交付收据和本轮耗时；如果产生了输出文件却无法证明已向用户呈现，收尾逻辑还会把本地结果降级为 `error`。客户端也不能仅凭一次状态查询推断最后的流事件已经全部发出。

## 为什么 HTTP 返回后 Agent 还在运行

Gateway 的统一入口是 `backend/app/gateway/services.py::start_run`。它完成请求校验、上下文准备和 Run 接纳后，把真正执行包装成 `run_after_metadata(record)`，再执行：

```python
worker = run_after_metadata(record)
record.task = asyncio.create_task(worker)
```

这里的后台任务（background task）是当前 Gateway 事件循环中的 `asyncio.Task`，不是持久任务队列。接纳成功后，创建接口可以立即把 `RunRecord` 返回；流式接口订阅 `StreamBridge`，`/wait` 则消费同一座 bridge 直到结束。三类 HTTP 交互复用同一个后台 Run，而不是各自实现一套 Agent 执行逻辑。

分离解决了两个时间尺度不一致的问题：HTTP 处理器只负责接纳、响应或观察，Agent 却可能经历多轮模型调用、长工具调用和终结写入。若把 Agent 完全绑在创建请求的调用栈上，普通创建接口必须一直占住连接，客户端超时也容易被误判为 Agent 已停止。当前设计允许 `POST /runs` 返回后继续执行，也允许其他请求按 `run_id` 查询、加入流或取消。

但“后台”不等于“不受连接影响”。创建流或 `/wait` 会根据 `on_disconnect` 决定创建者断开时取消还是继续；只读的加入流（join）观察者断开不能取消 Run。`services.py::sse_consumer` 和 `wait_for_run_completion` 负责这条 HTTP 边界，第 22、23 课再展开流与取消语义。本课只需记住：**连接是 Run 的控制/观察渠道，Run 才是执行生命周期的主体。**

这种进程内任务也不等于崩溃后可以从 Python 协程原地继续。Gateway 重启会丢失 `record.task`、锁和事件对象；持久层只能告诉新进程“曾有一个未终结 Run”。恢复逻辑会把已经失去所有者的 `pending`/`running` 行收口为 `error`，而不是假装复活旧协程。Checkpoint 能否用于新的恢复或重放，是第 23 课的问题。

## 一次成功 Run 的五个边界

把一轮普通对话沿时间顺序压缩为五步，能看清每层真正保证什么。

### 1. 先接纳并留下 `pending` 记录

生产入口调用 `RunManager.create_or_reject()`。它先通过 `RunStore.create_thread_operation_atomic()` 完成持久接纳，再把 `RunRecord` 注册进本进程；如果持久写失败，不暴露一个只存在于内存的成功 Run。`services.py::start_run` 在接纳与 `record.task` 附着之间不执行 `await`，避免 Run 已经可见却没有 worker 的窗口；若创建任务本身失败，`fail_start_if_pending()` 将其写成 `error`。

`pending` 因而不是“请求还没校验”，而是“Run 已被系统接纳，等待 worker 越过启动屏障”。它可能很短，但取消、数据库故障和进程退出都可能恰好落在这个阶段，所以不能省略。

### 2. 用启动屏障从 `pending` 进入 `running`

`backend/packages/harness/deerflow/runtime/runs/worker.py::run_agent` 在创建 Agent 前调用 `RunManager.try_start()`。后者用 `record.start_lock` 串行化本地启动，并让 `RunStore.start_run()` 只在持久行仍为 `pending` 时原子改成 `running`。如果取消已经获胜或持久行不再是 `pending`，返回 `RunStartOutcome.cancelled`，worker 不再构造 Agent。

这条不变量可以概括为：**只有成功认领同一个 pending Run 的 worker 才能执行它，取消不能被迟到的启动重新覆盖。** `backend/tests/test_run_manager.py::test_try_start_respects_durable_and_racing_cancels` 同时覆盖了持久取消先发生和本地取消与启动竞态两种情况。

### 3. `running` 期间执行图并持续产生状态

启动成功后，worker 才把 Thread 元数据投影为 `running`，建立运行上下文、读取 Checkpoint、组装 Agent，并通过图的异步流执行模型与工具。过程中消息进入 ThreadState/Checkpoint，流事件进入 `StreamBridge`，启用 RunJournal 时还会持续汇总 Token、消息和工具活动。它们都是本次 Run 的相关证据，但职责不同：Run 状态回答“这次执行走到哪里”，Checkpoint 保存对话状态，Event/Bridge 保存可观察过程。

一个 Run 在 `running` 时仍可能长时间没有文本输出，例如正在等待模型或工具。反过来，客户端已经收到一部分文本也不代表 Run 成功；后续工具、交付检查或最终持久化仍可能失败。

### 4. 先决定结果，再完成终结写入

`run_agent()` 的主路径把结果分成三类：正常完成写 `success`，异常或错误回退写 `error`，取消调用 `_finish_cancellation()`；普通中断（interrupt）写 `interrupted`，回滚取消（rollback）当前写 `error` 并尝试恢复运行前 Checkpoint。这里仅记录状态差异，取消与回滚的具体状态恢复留到第 23 课。

当配置了事件存储时，worker 先只在本地 `RunRecord` 暂存终态，让持久 Run 行继续保持活跃（active）；`finally` 中按顺序刷新事件、写幂等的 `run.delivery` 交付收据、保存完成统计和成功 Run 的耗时 Checkpoint，然后才持久化终态。这样另一个 worker 不会在最终 Checkpoint 尚未写完时，把这个 Thread 当成已经完全空闲。`backend/tests/test_run_worker_delivery.py::test_delivery_is_durable_before_terminal_run_status` 固定了“交付收据先于持久终态”的顺序。

这就是广义的终结阶段。窄义的 `record.finalizing` 只是一道进程内收尾屏障：例如取消后补标题或编辑重放回滚期间，后来的同 Thread Run 需要等待它释放。普通成功收尾也存在，但不必把这个布尔值设为 `True`。因此面试时应说“系统有终结阶段”，不要说“Run 会进入持久化的 finalizing 状态”。

### 5. 发布结束，再清理本地对象

尾部写入与完成钩子结束后，worker 清除必要的 `finalizing` 标记，调用 `bridge.publish_end(run_id)`，再安排 StreamBridge 和 `RunManager` 中进程内记录的延迟清理。RunStore 中的历史行不会随这次本地清理一起删除。

这个顺序解释了一个重要边界：**终态和流结束不是同一个信号。** 状态告诉查询者执行结果；END 告诉流消费者该 Run 不会再由这个发布者发送正常尾部事件。第 22 课将继续分析事件协议。

## `RunManager`、`RunStore` 和另外两类存储

名称相近的对象不能合并理解：

| 对象 | 保存什么 | 生命周期与作用域 | 不负责什么 |
| --- | --- | --- | --- |
| `RunManager` | 活跃 `RunRecord`、`asyncio.Task`、启动锁、取消事件、`finalizing`、所有权栅栏，以及对 Store 的协调 | Gateway 进程内；能优先返回拥有真实 task 的本地记录 | 不能靠自身跨进程重启保存历史 |
| `RunStore` | 可序列化的 Run 行：ID、Thread、状态、错误、时间、模型、用量、所有权等 | `MemoryRunStore` 只在进程内；配置数据库时由 SQL `RunRepository` 持久化 | 不保存 Python task、锁、Event，也不保存完整对话状态 |
| Checkpointer | ThreadState、消息、Goal 等图状态及其版本 | 按 Thread/Checkpoint 持久化 | 不等同于 Run 历史，也不能证明某个 worker 仍活着 |
| StreamBridge / RunEventStore | 在线流帧，以及启用时的持久运行事件 | 面向观察、重放和审计 | 不替代 Run 状态或 Checkpoint |

Gateway 启动时在 `backend/app/gateway/deps.py::langgraph_runtime` 选择实现：有数据库 session factory 时使用 SQL `RunRepository`，否则使用 `MemoryRunStore`，然后把它注入一个 `RunManager`。`RunManager.get()` 先返回本地记录，保留 task 与取消能力；本地没有时再从 Store 水合一个 `store_only=True` 的只读快照。这使当前 worker 可以控制自己拥有的 Run，也使历史查询在持久 Store 上跨重启存在。

不能笼统说“RunStore 是数据库”。它是接口；`MemoryRunStore` 的内容也会随进程消失。也不能笼统说“RunManager 是内存缓存”：它还负责接纳、启动、状态持久化、取消协调、终结竞争和孤儿恢复。二者的关键边界是：Manager 持有活的执行控制对象，Store 持有可共享或可恢复判断的序列化事实。

## 五条故障路径怎样收口

可靠的状态机不只画成功箭头，还要回答失败发生在哪个边界。

| 故障或控制动作 | 当前收口 | 为什么不能伪装成功 |
| --- | --- | --- |
| 接纳持久化失败 | 不注册可见的本地 Run，创建请求失败 | 否则会出现只能在本进程短暂查询、重启即消失的“幽灵 Run” |
| 已接纳但 worker 无法附着 | `pending → error` | Run 已有 ID，必须留下明确失败结果而不是永久 pending |
| Agent 构造、Checkpoint 或执行抛异常 | `running → error`，并尽力发布错误事件与 END | 已输出的局部文本不等于完整任务成功 |
| 普通取消 | `pending/running → interrupted`，随后仍执行终结阶段 | 消费者、Thread 状态和历史都需要看到明确终点 |
| Gateway 崩溃或 worker 失联 | 新进程仅对无本地活任务且租约已失效的活跃行做原子接管，写 `error` 与 `stop_reason=orphan_recovered` | 新进程没有旧协程，不能把遗留 `running` 行当成仍在执行或擅自标成功 |

最后一条由 `RunManager.reconcile_orphaned_inflight_runs()` 和 Gateway 启动接线共同完成。单 worker 的无租约活跃行会在重启时被回收；多 worker 时，租约仍有效说明可能由另一活 worker 拥有，不能抢占。具体租约（lease）、跨 worker 取消和原子并发控制留到第 24 课。`backend/tests/test_run_manager.py::test_reconcile_orphaned_inflight_runs_marks_stale_rows_error` 证明失去所有者的 pending/running 变成 error，而原有 success 保持不变。

进程正常关闭还有一条较温和的路径：`RunManager.shutdown()` 先取消并限时等待活跃 task，让它们在 Checkpointer 等资源关闭前完成终结；超出预算仍未收口的任务才被标为 `interrupted`。这不是“所有关闭都算失败”，也不是无限等待。

## 课程产出：一张双泳道 Run 生命周期图

画一张包含“HTTP/Gateway”和“后台 worker/持久层”两条泳道的状态图，保存到 `learn/outputs/lesson-21-run-lifecycle.md` 或你自己的学习记录。图中至少标出：

1. 请求校验、原子接纳并写 `pending`；
2. HTTP 创建接口返回，与 `asyncio.Task` 继续执行的分叉；
3. `try_start()` 的 `pending → running` 启动屏障；
4. `success`、`error`、`interrupted` 三条真实主终态路径；
5. 终态决定、持久终态、END 三个不同时间点；
6. worker 附着失败和进程重启孤儿恢复两条异常路径；
7. `RunManager` 的进程内对象与 `RunStore` 的序列化记录边界。

每条箭头旁写出一个源码符号或测试作为依据，而不是只抄本课文字。图后用不超过 200 字回答：“为什么查询到终态仍不等于已经收到全部流事件？”没有模型或数据库也能完成这项产出。

可选快速验证：在 `backend/` 运行下面的局部测试，分别观察正常状态、启动取消和重启孤儿收口；它们验证状态机局部不变量，不是完整的多实例或恢复测试。

```powershell
uv run pytest tests/test_run_manager.py -k "status_transitions or try_start_respects_durable_and_racing_cancels or reconcile_orphaned_inflight_runs_marks_stale_rows_error" -q
uv run pytest tests/test_run_worker_delivery.py -k "delivery_is_durable_before_terminal_run_status" -q
```

可选代码深挖按收益排序：先读 `runs/manager.py::RunRecord`、`create_or_reject`、`try_start` 和 `reconcile_orphaned_inflight_runs`，确认控制状态与持久状态的边界；再读 `services.py::start_run`，确认接纳和后台 task 如何接线；最后只读 `worker.py::run_agent` 的启动、结果判定和 `finally` 三段，核对终结顺序。无需逐行读完整 worker，也不要提前展开 SSE 帧、回滚算法或 lease 心跳。

面试验收：能在 2～3 分钟内画出 `pending → running → success/error/interrupted`，准确说明 `finalizing` 为什么不是持久状态、`success` 为什么不等于 END；能沿 `start_run → create_or_reject → asyncio.create_task → run_agent → try_start → finally` 讲清 HTTP 与 Agent 生命周期怎样分离；能区分 RunManager、RunStore、Checkpointer 和 StreamBridge，并解释 worker 附着失败与进程重启后为什么必须留下明确的错误或中断结论。
