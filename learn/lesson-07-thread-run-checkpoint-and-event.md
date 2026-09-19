# 第 7 课：Thread、Run、Checkpoint 与 Event

第 6 课追踪了网页消息怎样进入 Gateway、启动 Agent 并通过流返回页面。本课回答接下来的问题：**同一段执行为什么还要分别保存会话、执行记录、状态快照和过程事件？** 本课位于“Gateway 创建 Run → 图更新状态并写入 Checkpoint → Event/SSE 展示过程”这段主线。读完应能用两轮对话画出四者的关系，知道每种数据能回答什么问题，以及不能从它推断什么。核心阅读约 25～35 分钟。

这里沿用第 5 课的最小 Agent 和第 6 课的请求链。必须掌握四种对象的身份、关系和读取入口；状态字段的合并规则留到第 8 课，Run 的完整状态机、Checkpoint 恢复、流式重连和多实例可靠性分别留到第 21～24 课。

## 一段对话，四类问题

设已有会话 `T`，用户先问“15 × 4 是多少”，得到“60”；随后问“再加 2 呢”，得到“62”。下面的 `T`、`R1`、`R2`、`C0` 等**仅是教学示意 ID**，不是实际接口生成格式；Checkpoint 数量也不是固定的。

```text
Thread T：这段会话的身份与元数据
  ├─ Run R1：处理第一轮输入的一次执行
  │    ├─ Checkpoint C1、C2：图执行中可能留下的状态版本
  │    └─ Event (T, seq=1…k)：这一轮留下的消息或执行记录
  └─ Run R2：处理第二轮输入的另一次执行
       ├─ Checkpoint C3、…：继续沿用 T 的状态历史
       └─ Event (T, seq=k+1…)：第二轮的记录
```

`C0` 甚至可以早于 `R1`：`app.gateway.routers.threads::create_thread` 创建会话元数据后，会以 `thread_id` 写入空 Checkpoint，让新 Thread 的状态接口立即可用。因此不能说“一个 Run 对应一个 Checkpoint”，也不能把 Checkpoint 当成 Run 记录。

如果想知道“这是哪个会话、归谁、显示什么元数据”，查 **Thread**；

想知道“某轮是否在执行或失败”，查 **Run**；解决这一次到底执行得怎么样，某一次具体执行现在是什么状态？

想知道“图此刻保留什么消息、下一步是什么”，查 **Checkpoint 的物化状态**；

想知道“这一轮按什么顺序留下了哪些消息和执行痕迹”，查 **Event**。四个问题不同，保存粒度也不同。

## Thread：跨轮使用的会话身份

> Thread 不是“一轮执行”，也不是“聊天消息本身”，而是跨多轮 Run 持续存在的会话身份。
>
> Thread = 一段连续对话的“主键 + 元数据”。

Thread 是后续 Run 共用的 `thread_id`，同时有一条会话元数据记录。

它不是单个模型调用，也不是把全部会话消息直接塞进元数据表。`backend/packages/harness/deerflow/persistence/thread_meta/model.py::ThreadMetaRow` 中，`thread_id` 是主键，记录还保存 `user_id`、`assistant_id`、`status`、`metadata_json` 与时间；这些字段用于身份、归属、列表和展示。

Gateway 的 `backend/app/gateway/routers/threads.py::create_thread` 展示了另一半：

```python
created_record = await thread_store.create(thread_id, ...)
config = {
    "configurable": {
        "thread_id": thread_id,
        "checkpoint_ns": ""
    }
}
await checkpointer.aput(
    config,
    empty_checkpoint(),
    ckpt_metadata,
    {}
)
```

片段省略了校验和异常处理。第一步登记 Thread，第二步给图建立空的根状态。`C0` 可以早于第一个 Run 出现。因为你**仅仅创建 Thread**，还没发第一条消息，DeerFlow 就已经先写了一个空 Checkpoint。

`get_thread` 同时读取 Thread 元数据与  Graph 的物化状态，再组合成响应。因此 API 返回的 Thread 可以含 `values`，但这些状态值来自 Checkpoint 读取路径，不能据此认为元数据行本身存着完整图状态。

两轮对话只要继续使用 `T`，就会落在同一条会话线上。跨用户访问还需通过 Gateway 的权限检查；知道 `thread_id` 不等于有权读取该会话。

==Thread 元数据本身并不是把完整会话消息或完整图状态直接塞进元数据表里==。

它回答的问题是：

这是哪段会话？
是谁的？
用的是哪个 assistant？
现在什么状态？
列表里该怎么展示？

而不是：

完整 messages
完整 Graph State
每轮执行细节

## Run：一次被准入的执行

> Run = 某个 Thread 上的一次具体执行尝试。Thread 跨多轮存在，而每一次真正让 Agent 开始工作，都会对应一次 Run。
> Thread 告诉你这是哪一段会话，Run 回答的是这一次具体执行有没有真正启动？现在还在跑吗？成功了还是失败了

第一轮和第二轮分别有 `R1`、`R2`。Run 描述“这一次执行”，而 Thread 描述“它属于哪段会话”。`backend/packages/harness/deerflow/runtime/runs/manager.py::RunRecord` 同时带有 `run_id`、`thread_id`、`status`、创建时间、错误信息等；其中 `task` 是本进程后台任务引用，不是持久会话状态。SQL 后端的 `persistence/run/model.py::RunRow` 也以 `run_id` 为主键，并保存 `thread_id` 与 `status`。

第 6 课看到的 `app.gateway.services::start_run` 调用 `RunManager.create_or_reject(...)` 登记 Run，再安排后台 Worker。查看 `GET /api/threads/{T}/runs` 能列出该 Thread 的 Run；`GET /api/threads/{T}/runs/{R1}` 则查指定一次执行（实现：`app.gateway.routers.thread_runs::list_runs`、`get_run`）。目前 `RunStatus` 包含 `pending`、`running`、`success`、`error`、`timeout` 和 `interrupted`。这里先把状态看作执行结果或当前进度，不展开状态转移条件。

**不能只看 Thread 状态来判断某轮 Run 的结局。** 第二轮已经成功时，Thread 可能显示空闲，但 `R1`、`R2` 仍各有自己的执行记录。反过来，Run 成功也不意味着“只写过一个 Checkpoint”或“所有 SSE 帧都持久保存了”。

### 1. 为什么标题叫“一次被准入的执行”

这里“被准入”其实挺关键。并不是用户一发请求，就默认一定已经进入真正的 Agent 执行。

文档提到，第 6 课里的 `start_run` 会先调用：

```
RunManager.create_or_reject(...)
```

去登记或者拒绝这一次 Run，然后才安排后台 Worker。所以逻辑更像：

```
用户请求
   ↓
尝试创建 Run
   ↓
create_or_reject
   ↓
允许？
├── 否 → 拒绝
└── 是 → 创建 Run
          ↓
       后台 Worker 执行
```

> **Run 是系统正式接受下来、准备追踪其生命周期的一次执行任务。**

这就是“被准入”的意思。

------

### 2. Run 里面最重要的是哪些字段

文档提到 `RunRecord` 里面会有：

```
run_id
thread_id
status
created_at
error
...
```

SQL 持久化里的 `RunRow` 也会保存 `run_id`、`thread_id`、`status`。

你可以理解成：

```
Run R1

run_id    = R1
thread_id = T
status    = running
error     = null
```

这里两个 ID 一定要分清：

```
thread_id
```

回答：

> 这次执行属于哪段会话？

而：

```
run_id
```

回答：

> 这到底是哪一次执行？

所以以后排查问题的时候，通常不能只说：

```
Thread T 出错了
```

因为 Thread T 下面可能已经跑过：

```
R1 success
R2 success
R3 error
R4 success
```

真正出错的是某一次 Run。

------

### 3. Run 的 status 是它存在的核心意义之一

目前文档列出的 RunStatus 有：

```
pending
running
success
error
timeout
interrupted
```

所以一次 Run 大概会经历类似：

```
pending
   ↓
running
   ↓
success
```

也可能：

```
pending
   ↓
running
   ↓
error
```

或者：

```
running
   ↓
timeout
```

这里这一课还没要求你研究完整状态机，文档明确说完整状态转移后面再讲。

现在只要理解：

> **Run 是 Agent 一次任务执行生命周期的载体。**

也就是：

```
有没有开始？
是不是还在执行？
成功了吗？
失败了吗？
是不是超时了？
是不是被中断了？
```

这些问题都应该查 Run。

------

### 4. 为什么 Thread 状态不能替代 Run 状态

这是这一节很重要的点。

假设：

```
Thread T
├── R1 → error
└── R2 → success
```

现在整个 Thread 可能已经处于：

```
idle
```

或者某种非执行状态。

如果你只看 Thread：

```
Thread T：现在没在运行
```

那你根本不知道：

```
R1 曾经失败过
R2 后来成功了
```

所以文档特别强调：

> **不能只看 Thread 状态判断某轮 Run 的结局。** lesson-07-thread-run-checkpoint-and-event.mdMD

因为 Thread 描述的是：

```
整段会话
```

而 Run 描述的是：

```
某一次执行
```

粒度不一样。

------

### 5. Run 也不能替代 Checkpoint

这一点也特别容易混。

假设：

```
Run R1 = success
```

这只表示：

> 这次执行成功结束了。

它不能告诉你：

```
Graph 中途经过了哪些状态？
最后 messages 是什么？
next 是什么？
状态历史长什么样？
```

这些属于 Checkpoint。

所以：

```
Run
```

回答：

> 这次执行怎么样？

而：

```
Checkpoint
```

回答：

> Graph 状态是什么？

所以即使：

```
Run R1 success
```

也完全可能：

```
R1 执行期间产生多个 Checkpoint
```

文档明确提醒：

> Run 成功不意味着“只写过一个 Checkpoint”。

------

### 6. Run 也不能等价于 SSE

还有一个边界。

用户在浏览器里可能看到：

```
思考中...
正在调用工具...
正在生成答案...
```

这些是流式输出过程。

但：

```
Run
```

是一个执行记录。

所以：

```
Run = 一次执行任务的生命周期
SSE = 这次执行过程中往客户端推送的数据流
```

不能因为：

```
Run success
```

就认为：

> 所有 SSE 帧一定都持久保存了。

文档同样明确指出了这个边界。

------

你现在可以把 Run 想成后端里非常常见的“任务实例”。

比如你有一个：

```
视频转码任务
```

用户点一次“开始转码”，你会建：

```
Job 001
status = running
```

再点一次：

```
Job 002
status = pending
```

不会只用：

```
video_id
```

去表示所有执行历史。

DeerFlow 这里类似：

```
Thread ≈ 业务对象 / 会话
Run ≈ 一次 Job / Task 实例
```

## Checkpoint：图状态的一个版本

> Checkpoint 不是“一次 Run 的记录”，而是某个 Thread 在某个时刻的 Graph State 快照。

Checkpoint 是 LangGraph 图状态的保存点。它让后续读取能取得该 Thread 的状态，也给历史与恢复提供依据；**保存状态**不等于**记录一次执行的生命周期**。DeerFlow 提供 Checkpointer，并在运行配置中传递 `thread_id`。读取时，`backend/packages/harness/deerflow/runtime/checkpoint_state.py::CheckpointStateAccessor.aget` 调用图的 `aget_state(...)`，`ahistory` 调用 `aget_state_history(...)`；图与 Checkpointer 负责状态的保存和物化，Gateway 负责把结果投影成接口响应。

`backend/app/gateway/routers/threads.py::get_thread_state` 从状态快照取 `checkpoint_id`、`parent_checkpoint_id`、`values`、`next` 等，返回 `GET /api/threads/{T}/state`；`get_thread_history` 对应 `POST /api/threads/{T}/history`。最小关系是：

```text
同一 thread_id → 可以有多个 checkpoint_id
一个 Checkpoint → 一份特定时刻的图状态与父快照线索
最近的状态 → 可供下一轮 Run 继续使用
```

`backend/tests/test_threads_router.py::test_update_thread_state_inserts_new_checkpoint_each_call` 验证了同一 Thread 每次手动状态更新会产生不同的 `checkpoint_id`，历史中保留这些版本。这项测试不是“每轮 Run 必有几个快照”的证明，恰好说明 Checkpoint 也可能由 Run 之外的状态写入产生。第 8 课再讨论字段怎样合并、Full 与 Delta 如何表示同一逻辑状态；第 23 课再讨论具体恢复行为。

### 1. 为什么叫“图状态的一个版本”

这里的“图状态”就是 LangGraph 的 State。

你前面学过，Graph 运行时不是只有 messages，还可能有：

```
messages
plan
artifacts
当前步骤
工具结果
next
其他业务字段
```

节点执行以后会更新 State。

所以从概念上看：

```
节点执行
   ↓
State 发生变化
   ↓
形成新的状态版本
   ↓
Checkpointer 保存
```

于是 Checkpoint 就像 Git commit。

比如：

```
Git：
commit1 → commit2 → commit3

LangGraph：
checkpoint1 → checkpoint2 → checkpoint3
```

每一个都代表：

> **“当时系统状态长什么样。”**

这就是“版本”两个字的含义。

------

### 2. Checkpoint 最主要解决什么问题？

它首先解决的是：

> **下一轮 Run 怎么知道上一轮留下了什么状态？**

比如第二轮用户只说：

```
再加 2 呢？
```

如果没有之前状态：

```
AI：60
```

这句话就没有上下文。

但因为第二轮还是：

```
thread_id = T
```

Graph 可以沿着这个 Thread 找到最近状态：

```
messages = [
  user: 15 × 4 是多少？
  assistant: 60
]
```

然后继续执行。

所以文档总结得很直接：

```
同一个 thread_id
        ↓
可以对应多个 checkpoint_id
        ↓
最新 Checkpoint
        ↓
给下一轮 Run 继续使用
```

------

### 3. `checkpoint_id` 和 `thread_id` 是什么关系？

这里一定要分清。

```
thread_id
```

表示：

> 哪一段会话。

而：

```
checkpoint_id
```

表示：

> 这段会话里的哪一个状态版本。

所以关系大概是：

```
Thread T

├── Checkpoint C0
├── Checkpoint C1
├── Checkpoint C2
├── Checkpoint C3
└── Checkpoint C4
```

也就是：

> **一个 Thread 可以有很多 Checkpoint。**

这也是为什么状态接口会返回：

```
checkpoint_id
parent_checkpoint_id
values
next
```

文档明确说，`GET /api/threads/{T}/state` 会从状态快照中取这些信息。 

------

### 4. `parent_checkpoint_id` 又是什么？

这个东西很好理解。

比如：

```
C1
↓
C2
↓
C3
```

那么：

```
C2.parent_checkpoint_id = C1
C3.parent_checkpoint_id = C2
```

所以它保存的是：

> **这个状态版本是从哪个上一个状态版本演化来的。**

这样你就能看到状态历史链。

可以理解成：

```
C1
messages = [...]

↓ 执行某个节点

C2
messages = [...]
tool_result = ...

↓ 执行下一个节点

C3
messages = [...]
tool_result = ...
answer = ...
```

这也是为什么它不仅能提供“当前状态”，还能支持后面的：

```
状态历史
恢复
回溯
```

不过这节文档只让你知道它“提供依据”，具体恢复机制留到后面的课。 

------

### 5. Checkpoint 和 Run 最关键的区别

这一点你一定要说顺。

假设：

```
Run R1
```

执行过程中：

```
Node A
↓
Node B
↓
Node C
```

State 可能多次变化：

```
C1
↓
C2
↓
C3
```

所以可能是：

```
一个 Run
↓
多个 Checkpoint
```

但更重要的是：

> **Checkpoint 根本不严格属于 Run。**

因为上一节已经看到：

```
创建 Thread
```

的时候，就可以直接写：

```
empty_checkpoint()
```

此时：

```
Run 甚至还不存在
```

另外文档还专门提到测试：

```
test_update_thread_state_inserts_new_checkpoint_each_call
```

它验证了：

> 手动更新 Thread State，也可以产生新的 checkpoint_id。

也就是说：

```
Run 外的状态写入
```

一样可以制造 Checkpoint。 

所以千万不要建立：

```
Run R1 → Checkpoint C1
Run R2 → Checkpoint C2
```

这种一一对应认知。

正确的是：

```
Thread T
│
├── Run R1
├── Run R2
│
├── C0
├── C1
├── C2
├── C3
└── C4
```

它们都围绕同一个 Thread，但不是一一对应。

------

### 6. DeerFlow 里是谁真正负责保存 Checkpoint？

这里文档的表述很重要。

DeerFlow 会提供：

```
Checkpointer
```

运行 Graph 时传：

```
thread_id
```

而读取状态的时候：

```
CheckpointStateAccessor.aget
```

会调用 LangGraph 的：

```
aget_state(...)
```

读取历史则调用：

```
aget_state_history(...)
```

所以职责可以理解成：

```
LangGraph + Checkpointer
负责：
保存 / 读取 / 物化 Graph State

Gateway
负责：
把这些状态整理成 API Response
```

这里“物化状态”你可以先简单理解为：

> **根据保存的 Checkpoint，得到一份当前可以直接使用的完整 State。**

第 8 课会继续讲 Full / Delta，这里不用提前钻进去。

------

### 7. Checkpoint 和 Event 的区别

这个也非常值得提前分清。

假设执行过程是：

```
调用模型
调用搜索工具
搜索返回结果
模型生成答案
```

Event 更像：

```
10:00 模型开始
10:01 调用 search
10:02 search 完成
10:03 输出答案
```

它回答：

> **发生了什么？**

而 Checkpoint：

```
messages = [...]
search_result = [...]
next = ...
```

回答：

> **现在状态是什么？**

所以：

```
Event = 时间线 / 过程记录

Checkpoint = State 快照
```

你不能简单地认为：

> “我把 Event 重放一遍，就一定能恢复完整 Graph State。”

文档下一节会明确强调这个边界。

## Event：按时间记录发生过什么

这里的 Event 指 `RunEventStore` 中可查询的**运行事件记录**，不是第 6 课 `StreamBridge` 发送的每一帧 SSE。`backend/packages/harness/deerflow/runtime/events/store/base.py::RunEventStore` 定义了 `put`、`list_messages`、`list_events` 等接口：消息与执行痕迹可共用该存储，用 `category` 区分。`backend/packages/harness/deerflow/runtime/events/store/memory.py::MemoryRunEventStore._put_one` 构造的事件至少含 `thread_id`、`run_id`、`event_type`、`category`、`content`、`metadata`、`seq`、`created_at`。其 `seq` 在同一 Thread 内递增，而 `list_events(thread_id, run_id, ...)` 按一次 Run 查询。

实际接口是 `GET /api/threads/{T}/runs/{R1}/events`，实现位于 `app.gateway.routers.thread_runs::list_run_events`。一条消息事件可以告诉你某轮输出了什么；一条执行痕迹可以帮助定位过程。它们不代替 Checkpoint：事件序列不是现成的图状态快照，不能假设把事件重放就一定得到图的完整当前状态。

SSE 是“正在把执行过程送给客户端”的传输流。`backend/packages/harness/deerflow/runtime/stream_bridge/base.py::StreamEvent` 使用流事件 `id`、`event`、`data`；持久 Event 使用 Thread 内的 `seq` 和 Run 关联键。两者面向不同读取需求，不能把 SSE `id` 当成持久事件的 `seq`。事件存储的实际持久性还取决于配置：`config/run_events_config.py::RunEventsConfig` 支持 `memory`、`db`、`jsonl`；默认 `memory` 在进程重启后不保留数据。所以“能查到 Event”也不等于“任何部署下都永久保留”。第 22 课再分析 SSE 协议与前端消费。

这一节最核心的一句话是：

> **Event 记录的是“这次执行过程中，按时间顺序发生了什么”。**
> 它不是状态快照，也不是浏览器收到的每一帧 SSE。

你可以先把它理解成：

> **Event = 一次 Run 的过程记录 / 时间线。**

比如一次 Run 里，可能发生：

```
Run R1

Event 1：收到用户消息
Event 2：Agent 开始执行
Event 3：调用工具
Event 4：工具返回结果
Event 5：模型输出答案
Event 6：执行结束
```

所以它回答的问题不是：

> “现在状态是什么？”

而是：

> **“刚刚按什么顺序发生了哪些事情？”**

------

### 1. Event 里面大概存什么

文档里提到，`RunEventStore` 的事件至少会有：

```
thread_id
run_id
event_type
category
content
metadata
seq
created_at
```

这里几个字段很好理解。

`thread_id` 表示：

> 这条事件属于哪段会话。

`run_id` 表示：

> 它属于哪一次执行。

`event_type` 表示：

> 发生的是什么类型的事情。

`content` 表示：

> 具体内容是什么。

而 `seq` 很重要，它表示：

> **事件在同一个 Thread 里的顺序号。**

比如：

```
Thread T

seq=1  属于 Run R1
seq=2  属于 Run R1
seq=3  属于 Run R1

seq=4  属于 Run R2
seq=5  属于 Run R2
```

所以虽然查询时可以按某个 Run 查：

```
GET /api/threads/{T}/runs/{R1}/events
```

但事件本身的顺序编号，是沿着 Thread 递增的。

------

### 2. 为什么需要 Event，Checkpoint 不够吗？

这是最关键的问题。

假设现在最终状态是：

```
messages = [...]
plan = [...]
next = []
```

Checkpoint 能告诉你：

> Agent 最后状态是什么。

但它不一定能很好回答：

```
模型先做了什么？
什么时候调用了工具？
工具什么时候返回？
中间发生了哪些执行痕迹？
```

这些属于 Event。

所以两者的区别可以压成：

```
Checkpoint
= 某个时刻“状态是什么”

Event
= 一路上“发生过什么”
```

比如你排查线上问题：

```
用户：
“为什么这个 Agent 最后回答错了？”
```

你看 Checkpoint，可能只看到最终状态。

但看 Event，你可能发现：

```
先调用 search
↓
search 返回空结果
↓
随后模型仍然生成答案
```

这时 Event 就非常有价值。

------

### 3. Event 不是完整 Graph State

这一点文档特别强调。

你不能想当然地认为：

```
把 Event 从头 replay 一遍
=
一定能恢复当前 Graph State
```

不是。

因为 Event 是：

> **过程记录。**

而 Checkpointer 才是专门负责：

> **状态保存与状态物化。**

所以：

```
Event ≠ Checkpoint
```

文档明确说：

> 事件序列不是现成的图状态快照，不能假设把事件重放就一定得到完整当前状态。

这个边界非常重要。

------

### 4. 最容易混淆的地方：Event ≠ SSE

这个是这一节最值得你记住的。

你第 6 课刚学过：

```
Worker
↓
StreamBridge
↓
SSE
↓
浏览器
```

所以很容易觉得：

> “SSE 推了一条东西，不就是一个 Event 吗？”

但这节专门告诉你：

**不是同一个东西。**

------

SSE 的定位是：

> **传输。**

它解决的是：

```
Agent 正在运行
↓
怎么把实时结果推给前端
```

比如：

```
data: 正在搜索...
data: 找到结果...
data: 正在生成回答...
```

它面向的是：

> **实时客户端。**

------

而这里说的 Event 是：

> **==RunEventStore 中可以查询的运行记录==。**

它解决的是：

```
这次 Run 已经执行过了
↓
以后我怎么查询当时发生了什么
```

所以二者可以这样记：

```
SSE
= 实时传输通道

Event
= 可查询的过程记录
```

------

### 5. SSE 的 `id` 和 Event 的 `seq` 也不是一回事

文档还特意提醒：

```
SSE id
≠
Event seq
```

SSE 那边有自己的：

```
id
event
data
```

而持久 Event 这边有：

```
seq
thread_id
run_id
...
```

所以你不能看到：

```
SSE id = 10
```

就认为：

```
RunEvent.seq = 10
```

二者属于不同系统、不同用途。

------

### 6. 为什么会同时有 `thread_id` 和 `run_id`

因为 Event 同时有两种查询需求。

第一种：

> 这条事件属于哪段会话？

所以要有：

```
thread_id
```

第二种：

> 这条事件属于这段会话里的哪次执行？

所以还要有：

```
run_id
```

于是你可以得到：

```
Thread T

Run R1
├── Event seq=1
├── Event seq=2
└── Event seq=3

Run R2
├── Event seq=4
└── Event seq=5
```

这也解释了文档为什么说：

> 事件同时标记 Thread 与 Run，并在 Thread 内排序。

------

### 7. Event 还有一个很重要的工程边界：不一定永久保存

这个点很容易漏。

DeerFlow 的 Event Store 配置支持：

```
memory
db
jsonl
```

其中如果是：

```
memory
```

那进程一重启：

```
Event 就没了
```

所以：

> “接口现在能查到 Event”

不等于：

> “这些 Event 永远都会持久保存。”

所以你面试时如果说：

> DeerFlow 的 Event 都会持久化保存。

这是不严谨的。

更准确应该说：

> **Event Store 提供可查询的运行记录，但实际是否跨进程重启保留，取决于底层使用 memory、db 还是 jsonl。**

------

你现在可以把四个概念彻底压缩成：

```
Thread
= 这是谁的一段会话

Run
= 这次执行怎么样

Checkpoint
= 当前/历史状态是什么

Event
= 执行过程中发生了什么
```

而这一节最重要的两个边界，就是：

```
Event ≠ Checkpoint
Event ≠ SSE
```

如果面试问你 Event 的作用，我建议你这样说：

> **Event 用来记录一次 Run 执行过程中产生的消息和执行痕迹，每条事件关联 thread_id 和 run_id，并通过 seq 保证同一 Thread 内的顺序。它主要用于过程查询和排障，不负责保存完整 Graph State；同时它和 SSE 也不同，SSE 是实时传输机制，而 Event 是 RunEventStore 中可查询的运行记录。**

这段已经非常接近可以直接拿去讲的版本了。

**SSE 是“实时往前端推”，Event 是“把发生过的事情记录下来”。**

比如 Agent 执行时产生一条“调用了搜索工具”的信息：

```
Worker 产生执行信息
├── 通过 SSE → 立刻推给浏览器看
└── 写入 Event Store → 之后还能查询这次 Run 发生过什么
```

但要注意，**==不是每一帧 SSE 都一定对应一个持久 Event==，也不能把 SSE 的 `id` 当成 Event 的 `seq`**。它们是两套不同用途的机制：SSE 偏实时传输，Event 偏过程记录与查询。

## 为什么不能合成一张记录

四者的关系可记为：**一个 Thread 容纳多次 Run；同一 Thread 上可以有多个 Checkpoint；事件同时标记 Thread 与 Run，并在 Thread 内排序。** Checkpoint 与 Run 不保证一一对应：创建 Thread 就会写空快照，运行过程中可产生多次状态版本，独立的状态更新也能产生快照。

若只保留 Thread，就难以分别回答两轮执行各自的状态；若只保留 Run，就没有连续对话所需的图状态版本；若只保留 Checkpoint，就没有完整的执行尝试和按序过程记录；若只保留 Event，就需要把事件流当成状态机来重建图状态，而当前系统明确另用 Checkpointer 管这件事。这些拆分让“会话身份、执行尝试、可继续的状态、可查询的过程”各有明确查询入口和生命周期，也带来多处数据需要关联的成本。

## 可选验证：给两轮对话画关联图

在已配置并启动的本地服务中，沿用第 6 课的网页会话发送两轮消息。使用浏览器 Network 面板或带认证的 API 客户端，记录同一个 `thread_id`，再读取以下接口；浏览器经过 Nginx 时可见 `/api/langgraph/*` 前缀改写，以下写的是 Gateway 原生路径：

| 查询 | 记录什么 | 预期发现 |
| --- | --- | --- |
| `GET /api/threads/{T}` | `thread_id`、元数据、当前 `values` | 一段会话身份；状态值由后端组合返回 |
| `GET /api/threads/{T}/runs` | 每个 `run_id`、`thread_id`、`status` | 两次普通发送各对应一次 Run；若存在重试等额外操作，以实际记录为准 |
| `GET /api/threads/{T}/state` | 当前 `checkpoint_id`、`parent_checkpoint_id`、`values` | 当前图状态及快照标识 |
| `POST /api/threads/{T}/history`，请求体 `{"limit": 10}` | 各条历史的 `checkpoint_id` | 同一 Thread 可有多个快照，数量不要求等于 Run 数 |
| `GET /api/threads/{T}/runs/{R}/events` | `run_id`、`seq`、`category`、`event_type` | 该 Run 的事件；内容和数量取决于执行及事件存储配置 |

产出一张四列术语表：对象、身份键、回答的问题、不能替代谁。真实 Run 数或事件数若与示意不同，先检查是否发生重试、再生成、存储配置或权限问题，不要为凑示意图修改结果。此练习用于验证理解；核心结论已经在正文给出。

## 面试验收

可以这样讲：**Thread 是跨轮会话身份；Run 是一次执行尝试；Checkpoint 是该 Thread 的图状态版本；Event 是按 Thread 排序、可按 Run 查询的消息与过程记录。** 两轮对话通常共用 `thread_id`，各有自己的 `run_id`；Checkpoint 数量不与 Run 数量绑定。若被追问证据，可指出 `create_thread` 同时写会话元数据和空 Checkpoint，`RunRecord` 同时持有 `run_id` 与 `thread_id`，`RunEventStore` 的记录同时含两者及 `seq`。若被追问边界，要说明持久 Event 与 SSE 帧不同，事件存储是否跨重启保留取决于后端配置。

***

> [!IMPORTANT]
>
> 它们属于 ==Agent Runtime 的“运行时状态与执行生命周期管理”==。
>
> 再具体一点，可以拆成两条线：
>
> - **状态线**：Thread + Checkpoint，解决多轮会话如何连续、状态如何保存。
> - **执行线**：Run + Event，解决一次 Agent 执行如何追踪、过程如何记录。
>
> 多轮 Agent 怎么保持连续状态？
>
> 一次 Agent 执行怎么独立管理？
>
> 执行出了问题，怎么知道中间发生过什么？
>
> **Agent 执行失败怎么排查？**
>
> 先通过 Run 定位哪一次执行失败，再通过 Event 看执行过程；如果关心执行到哪个状态，则查 Checkpoint。

- 设计 Agent Runtime 状态管理机制，将跨轮会话、单次执行、图状态版本和运行事件解耦，实现多轮状态延续、执行生命周期追踪与过程可观测。

Agent 真正运行起来以后，我觉得一个比较关键的问题是，不能把“会话”“一次执行”“当前状态”和“执行过程”混在一起，所以 Runtime 层把它们拆成了不同的数据模型：Thread 管会话，Run 管单次执行，Checkpoint 管状态版本，Event 管执行过程。

它们其实是在解决一段 Agent 对话里的四类不同问题。

**首先是 Thread**。

**Thread 表示的是一段跨多轮的会话身份**。比如用户第一轮问“15×4是多少”，第二轮又问“再加2呢”，这两轮虽然是两次独立执行，但它们会共用同一个 thread_id，所以系统知道第二轮是在上一轮的上下文上继续。

Thread 本身主要保存的是会话级的元数据，比如 thread_id、user_id、assistant_id、status、metadata 等。它并不直接保存完整的 Graph State。DeerFlow 创建 Thread 时，会先创建 Thread 元数据，同时通过 Checkpointer 写入一个空的 Checkpoint，所以甚至在第一次 Run 之前，就可能已经存在一个初始状态快照。fileciteturn0file0L25-L39

**第二个是 Run**。

**Run 表示某个 Thread 上的一次具体执行尝试**。比如用户发第一条消息产生 Run R1，第二轮消息产生 Run R2。

Run 主要负责记录一次执行的生命周期，比如 run_id、thread_id、status、错误信息等。状态可以是 pending、running、success、error、timeout 或 interrupted。

DeerFlow 在真正启动后台 Worker 之前，会先通过 RunManager 对这次执行进行登记和准入，所以 Run 更像后端系统里的一个任务实例。它回答的是：“这一次执行现在进行到哪里了，最后成功还是失败了？”

所以 Thread 和 Run 的区别是，Thread 管的是长期会话，而 Run 管的是单次执行。即使同一个 Thread 当前已经空闲，之前的某个 Run 仍然可能是失败状态，这也是为什么不能只看 Thread 的状态判断某次执行结果。fileciteturn0file0L41-L47

**第三个是 Checkpoint**。

**Checkpoint 是 LangGraph 图状态的一个版本**。

Agent 在执行过程中，节点会不断更新 Graph State，比如 messages、plan、工具结果或者 next 等字段。每次状态被保存下来，就形成一个状态版本，也就是 Checkpoint。

同一个 thread_id 可以有多个 checkpoint_id，Checkpoint 之间还可以通过 parent_checkpoint_id 形成状态历史。后续新的 Run 仍然使用同一个 thread_id 时，就可以继续读取这个 Thread 当前的状态并继续执行。

这里最容易混淆的是，Checkpoint 和 Run 并不是一一对应的。

一个 Run 执行过程中可能产生多个 Checkpoint；而且 Run 之外的状态更新也可以产生 Checkpoint。比如创建 Thread 时写入的空 Checkpoint，就是在任何 Run 之前产生的。

所以我会把 Run 理解成“执行生命周期”，而 Checkpoint 理解成“状态生命周期”。

**第四个是 Event**。

**Event 记录的是一次 Run 执行过程中，按时间顺序发生了什**么。

比如产生了什么消息、发生了什么执行动作，都可以记录成 Event。Event 会关联 thread_id 和 run_id，并通过 seq 在同一个 Thread 内排序，所以它更像执行过程的时间线或者流水账。

它和 Checkpoint 的区别是，Checkpoint回答“当前状态是什么”，Event回答“过程中发生了什么”。因此不能假设把 Event 从头重放一遍，就一定能够恢复完整 Graph State，状态恢复还是由 Checkpointer 负责。

这里还有一个很容易混淆的点，就是 Event 和 SSE 不是一回事。

SSE 主要负责把 Agent 正在发生的执行过程实时推给浏览器，它是传输机制；而这里的 Event 是 RunEventStore 中可以后续查询的运行记录。

所以不是每一帧 SSE 都一定对应一个持久 Event，SSE 的 id 也不能直接理解成 Event 的 seq。另外 Event 是否真正跨进程重启持久保存，还取决于底层使用 memory、db 还是 jsonl。

**所以最后我会把这四个对象总结成四句话**：

Thread 解决“这是哪一段会话”；

Run 解决“这一次执行怎么样”；

Checkpoint 解决“这个 Thread 当前或历史状态是什么”；

Event 解决“这一次执行过程中发生了什么”。

这样拆分之后，==会话身份、执行生命周期、Agent 状态以及执行过程都有自己独立的存储和查询入口。代价是需要维护它们之间的关联，但好处是职责和生命周期都比较清晰==。
