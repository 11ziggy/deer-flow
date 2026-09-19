# 第 8 课：ThreadState 与 Reducer

> 这节课可以把它定位成：**学习 DeerFlow 的“状态更新规则”这一层。**
>
> 前面你已经知道了 Thread 是一次长期会话，Run 是一次执行，Checkpoint 是某个时刻保存下来的图状态。那接下来马上会遇到一个问题：
>
> **图里的状态到底是怎么一步一步变成现在这个样子的？**
>
> 因为 Agent 执行时，每个节点通常不会返回一份完整 State，而只是返回自己改动的那几个字段。比如一个节点新增了一条消息，另一个节点新增了一个 artifact，还有一个节点更新了 delegation 状态。此时 LangGraph 必须知道：**新值来了以后，到底应该覆盖旧值、追加到旧值，还是和旧值按某种规则合并。** 这就是这一课讲的核心。
>
> **Reducer** 解决“每次节点产生局部更新以后，旧状态和新状态怎么合并”。



第 7 课区分了 Thread、Run、Checkpoint 与 Event。本课接着回答：**同一个 Thread 的图状态收到多次局部更新时，为什么有的字段要追加，有的要替换，有的要保留旧值？** 它位于“模型或工具产生更新 → 图合并 ThreadState → Checkpoint 保存状态”这一段请求主线。核心阅读约 30～40 分钟。

前置知识是第 5 课的 State、Node、Reducer 和第 7 课的 Checkpoint。必须掌握字段的合并语义、消息按 ID 更新的规则，以及 `full` / `delta` 两种消息持久化方式的区别。本课只讨论**状态值怎样形成**；消息如何通过 SSE 展示、Checkpoint 如何回滚或恢复，留给第 22～23 课。

## 一次局部更新，为什么不能统一使用“新值覆盖旧值”

图节点和工具通常只返回自己改变的字段，而不是每次重写完整会话状态。假设已有以下状态（ID 均为教学示意）：

```text
messages    = [用户消息 h1, 助手消息 a1]
artifacts   = ["/outputs/report.md"]
todos       = [{"id": "t1", "done": false}]
goal        = {"objective": "完成报告", "status": "active"}
delegations = [{"id": "call-1", "status": "in_progress", ...}]
```

随后两个节点分别提交 `{"artifacts": ["/outputs/chart.png"], "todos": None}` 和 `{"delegations": [{"id": "call-1", "status": "completed", ...}]}`。期望结果是报告与图表都保留、待办不被无关节点清掉、同一委派记录从进行中更新为已完成。一个统一的“覆盖”规则会丢掉报告和待办；一个统一的“拼接”规则则会产生重复的委派记录。

**归约器（reducer）**是某个状态字段的合并函数：输入已有值和本次写入，输出该字段的新值。LangGraph 根据状态类型上的 `Annotated[..., reducer]` 选择它；未声明自定义归约器的普通字段采用单值更新语义(默认覆盖)。`backend/packages/harness/deerflow/agents/thread_state.py::ThreadState` 的关键声明如下，片段只摘取本课字段：

```python
class ThreadState(AgentState):
    artifacts: Annotated[list[str], merge_artifacts]
    todos: Annotated[list | None, merge_todos]
    goal: Annotated[GoalState | None, merge_goal]
    delegations: Annotated[list[DelegationEntry], merge_delegations]

class DeltaThreadState(ThreadState):
    messages: DELTA_MESSAGES_FIELD
```

这段声明证明合并规则是**按字段绑定**的。`ThreadState` 继承 `AgentState` 的 `messages`；只有 `delta` 模式的 `DeltaThreadState` 覆盖其消息通道。一个节点返回 `{"todos": None}` 与完全没有返回 `todos` 在输入形式上不同；但 `merge_todos` 把 `None` 解释为“不更改”，把 `[]` 解释为“明确清空”。这一约定来自 DeerFlow 的归约函数，不能推广为所有字段的通用规则。

> `ThreadState` 自己虽然没有显式写：messages: ... 但它继承自 `AgentState`，所以本来就已经有 `messages` 字段，而且默认使用 `AgentState` 里定义好的消息合并规则。
> 而 `DeltaThreadState` 里面自己定义了规则。意思是其他字段规则都沿用 `ThreadState`，但 `messages` 这一项我要换一种实现。
>
> 所以 `full / delta` 的差别主要落在 `messages` 这个字段上，而不是重新设计整套 ThreadState。文档后面会详细讲。

==在 Python 里类的继承==：

```
class Xxx(Aaa):
    ...
```

表示 **`Xxx` 继承 `Aaa`**。

***

这一部分其实是在回答一个很基础、但很关键的问题：

**既然节点每次只提交自己改动的字段，那这些“局部更新”进入 ThreadState 时，为什么不能简单地用新值把旧值覆盖掉？**

文档先给了一个已有状态的例子：`messages` 里有历史消息，`artifacts` 里已经有报告文件，`todos` 里有一个未完成待办，`goal` 有当前目标，`delegations` 里还有一条正在执行的委派记录。

然后假设某个节点只返回：

```
artifacts = ["/outputs/chart.png"]
todos = None
```

另一个节点又返回：

```
delegations = [
  {"id": "call-1", "status": "completed"}
]
```

如果你规定所有字段都采用：

> 新值来了，就直接覆盖旧值

马上就会出问题。

最直观的是 `artifacts`。

原来是：

```
["/outputs/report.md"]
```

新节点返回：

```
["/outputs/chart.png"]
```

如果直接覆盖，结果就变成：

```
["/outputs/chart.png"]
```

那之前的 `report.md` 就莫名其妙丢了。

但业务上你真正想要的是：

```
[
  "/outputs/report.md",
  "/outputs/chart.png"
]
```

也就是说，`artifacts` 的语义明显不是“覆盖”，而是更接近：

> **在已有结果上继续累积，而且避免重复。**

再看 `todos`。

原来有：

```
[{"id": "t1", "done": false}]
```

这次节点返回：

```
todos = None
```

这里的 `None` 在 DeerFlow 里并不是“请把 todos 清空”，而是：

> **这个节点没有要修改 todos。**

所以正确结果应该继续保留原来的 `t1`。

如果统一采用覆盖：

```
old todos
        ↓
new = None
        ↓
todos = None
```

那一个根本没想碰待办列表的节点，就把整个待办状态给干掉了。

所以这里已经出现两种完全不同的更新语义：

```
artifacts
旧值 + 新值 → 合并

todos
新值是 None → 保留旧值
```

再看 `delegations`，问题又不一样。

原来：

```
[
  {"id": "call-1", "status": "in_progress"}
]
```

新节点返回：

```
[
  {"id": "call-1", "status": "completed"}
]
```

你当然不能简单地把两条都拼进去，否则就会变成：

```
[
  {"id": "call-1", "status": "in_progress"},
  {"id": "call-1", "status": "completed"}
]
```

同一个委派 `call-1` 出现两份。

它真正需要的语义是：

> **按 id 找到原来的记录，然后把它更新掉。**

最后应该还是一条：

```
[
  {"id": "call-1", "status": "completed"}
]
```

文档这里其实想让你发现一个规律：

**既不能统一覆盖，也不能统一追加。**

因为不同字段代表不同类型的状态。

你可以这样理解。

`artifacts` 是“成果集合”，所以适合累计。

`todos` 更像“当前完整待办列表”，所以新的非 `None` 值适合整体替换。

`delegations` 是“带唯一 ID 的记录集合”，所以要按 ID 更新。

`messages` 又更特殊：新 ID 是追加，同 ID 是修改原消息，而不能无脑 append。文档后面会专门讲。

所以这里才引出了 **Reducer**。

Reducer 本质上就是：

> **告诉 LangGraph：某个字段收到一次局部写入时，旧值和新值到底应该怎么算出最终值。**

可以把它想象成：

```
Reducer(旧值, 本次新值) -> 最终值
```

例如：

```
merge_artifacts(
    [report.md],
    [chart.png]
)

→ [report.md, chart.png]
```

而不是：

```
artifacts = newValue
```

DeerFlow 就是在 `ThreadState` 的字段定义上，为不同字段绑定不同 Reducer：

```
class ThreadState(AgentState):
    artifacts: Annotated[list[str], merge_artifacts]
    todos: Annotated[list | None, merge_todos]
    goal: Annotated[GoalState | None, merge_goal]
    delegations: Annotated[list[DelegationEntry], merge_delegations]
```

也就是说，**合并规则是字段级别的，而不是整个 State 共用一个规则。** 

你现在可以先把这一部分压缩成一句特别重要的话：

> **因为 LangGraph 节点产生的是局部状态更新，而不同字段本身具有不同的业务语义，所以不能统一用“新值覆盖旧值”；必须为不同字段定义不同的 Reducer，决定它们是覆盖、保留、追加还是按 ID 合并。**

你如果把这句话真正理解了，后面 `merge_artifacts`、`merge_todos`、`merge_delegations` 基本就只是具体例子了。

***

## 我的两个疑惑

> 1. 给 Agent 看的上下文，和全部历史消息应该是分开存储的吧，怎么存储的？
> 2. 这里在 State，定义了 message 的 Reducer，和它绑定了，但是如果后续要压缩上下文之类的，应该不是简单的追加吧，这种是怎么处理的呢？

你这个疑问其实碰到了这套设计最核心的一层。**但第 1 点有一个地方需要修正：DeerFlow 并不是“完整历史消息一份 + 给 Agent 的压缩上下文另一份”这样简单地存两套消息。**

首先，这个状态里面的 messages 列表，并不就是模型最终看到的信息；同时它也不是一个全部历史消息记录。这颠覆了我的理解。其实是分层次的：前端展示的完整消息记录，用的是 RunEventStore，也就是前面学的事件日志存储，它保存历史上发生过的运行事件，其中包括前端可见的消息记录（前端不一定展示所有的运行事件）。这个 State 专门保存 Agent 需要用到的、需要记住的东西（当前 ThreadState 中保留下来的、==参与 Agent 后续执行的==消息状态），经过上下文压缩之后，里面的 messages 就会改变。但是它也并不是最终 LLM 看到的东西，还需要经过 Middleware 一系列中间件的组装。后续课程会讲到这个中间件。

### 1. DeerFlow 实际上分的是「持久状态」和「模型请求上下文」

可以先用这张图理解：

```
             Thread 的持久状态
        ┌────────────────────────┐
        │ messages               │  当前保留的消息
        │ summary_text           │  被压缩历史的摘要
        │ delegations            │
        │ skill_context          │
        │ ...                    │
        └───────────┬────────────┘
                    │
                    │ Middleware 组装
                    ↓
        ┌────────────────────────┐
        │ 本次真正发给 LLM 的上下文 │
        │                        │
        │ System Prompt          │
        │ 历史摘要 summary       │
        │ 当前保留的 messages    │
        │ skill / delegation...  │
        └────────────────────────┘
```

也就是说：

> **State 是 Agent 的持久状态，解决“Agent 要记住什么”；ModelRequest 是这一刻真正给模型看的上下文。**
>
> DeerFlow 的持久状态保存的是**当前需要跨节点、跨轮次继续保留的 Agent 状态**；模型真正看到的上下文，则由 Middleware 在调用模型前，基于这些状态再动态组装出来。
>
> ```python
> 持久状态（State / Checkpoint）
>         ↓
> Middleware 读取、筛选、补充、重组
>         ↓
> ModelRequest.messages
>         ↓
> LLM
> ```
>
> 但是，==持久状态 ≠ 所有原始信息的完整备份。这个持久化的 State 状态只用于 Agent 的执行，而不是前端能看到的所有历史消息；那是另外的一套存储==。
>
> 前端看到的“完整聊天记录”，**主要不是从当前 `ThreadState.messages` 读出来的**，而是从另一套 **RunEventStore / 事件日志** 里恢复出来的。[前端如何展示完整的记录](.\Notes\lesson-08-full-history)

这两个不是一回事。

源码里的 `DurableContextMiddleware` （把“长期需要记住的信息”从 State 里拿出来，在每次调用模型前重新注入上下文）非常直接：在真正调用模型之前，它从 State 里拿出：

```
state.get("summary_text")
state.get("delegations")
state.get("skill_context")
...
```

然后构造成一个隐藏的 `HumanMessage`，插入这次 `ModelRequest.messages`。

关键是源码注释明确写了：

> ```
> never written back to state
> ```

也就是**只是临时注入模型上下文，不重新写进 ThreadState.messages**。

所以你之前学上下文工程时的理解，在这里是对得上的：

```
存储状态
≠
模型上下文
```

模型上下文是从持久状态中**加工出来的一个视图**。

------

但这里还有一个很重要的反直觉点：

### DeerFlow 的 `messages` 也不是“永远保存全部历史消息”

在还没有发生上下文压缩的时候，可以近似理解成：

```
messages =
[m1, m2, m3, m4, m5, ...]
```

它不断积累。

但是当上下文太长，触发 `DeerFlowSummarizationMiddleware` 后，它会把历史分成两部分：

```
messages_to_summarize   # 老消息，准备压缩
preserved_messages      # 最近消息，继续保留
```

然后用模型把前者生成：

```
summary_text
```

源码的 `ContextCompactionResult` 就直接有这三个字段：

```
summary_text
messages_to_summarize
preserved_messages
```

接下来最关键的一步来了。

压缩成功之后，Middleware 返回的状态更新实际上是：

```
{
    "messages": [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        *result.preserved_messages,
    ],
    "summary_text": result.summary_text,
}
```

也就是说：

> **把 messages 里的旧历史清掉，只留下需要保留的近期消息；被清掉历史的信息则进入 `summary_text`。**

所以假设原来：

```
messages:

m1
m2
m3
m4
m5
m6
m7
m8
```

触发压缩，假设：

```
m1 ~ m5 → 总结

m6 ~ m8 → 保留
```

最终 State 更接近：

```
summary_text:
    "之前用户要求研究 DeerFlow，已经讨论了……"

messages:
    m6
    m7
    m8
```

而不是：

```
all_messages:
    m1 ... m8

context_messages:
    summary + m6 ... m8
```

这是你第一个问题里最值得修正的地方。

**DeerFlow 当前 ThreadState 本身并没有把“完整原始消息历史”永远留在 `messages` 里。压缩之后，旧消息会被从当前状态删除。**

源码甚至明确注释：

> summary call 返回后，`messages_to_summarize` 会从 state 中消失，留下的是生成的 summary。

当然，旧的 Checkpoint、task history、durable memory 等机制还可能保存其他形式的历史信息，但那是另外一层，**不能把它理解成 `ThreadState` 里还有一个 `all_messages` 字段。**

------

### 2. 那你第二个问题就很好解释了：Reducer 根本不是“只能追加”

你刚才的疑问是：

> messages 已经绑定 Reducer 了，那压缩上下文的时候，肯定不能只追加啊？

完全正确。

所以 `messages` 的 Reducer 从一开始就不是：

```
old + new
```

这么简单。

第 8 课已经强调了，==它支持几种操作==：

```
新 ID
→ 追加

相同 ID
→ 更新原消息

RemoveMessage(id=x)
→ 删除指定消息

RemoveMessage(id=REMOVE_ALL_MESSAGES)
→ 清空已有消息
```

所以 Reducer 更准确的理解应该是：

> **==Reducer 定义的是“消息写入操作如何作用到现有消息状态上”==，而不是“永远 append”。**

> [!NOTE]
>
> `messages` 的 Reducer  支持几种操作。我还以为，一个字段，就绑定一种操作。那么怎么路由具体的操作的呢，agent 怎么知道什么时候用哪种？[Reducer 怎么决定如何更新](.\Notes\lesson-08-how-reducer-routed)

上下文压缩正好就是这个设计的典型用途。

压缩 Middleware 不需要自己去：

```
state["messages"].clear()
state["messages"].extend(...)
```

它只需要提交一次**局部状态更新**：

```
messages = [
    RemoveMessage(REMOVE_ALL_MESSAGES),
    m6,
    m7,
    m8,
]
```

然后 messages Reducer 去解释这次写入：

```
原状态：

m1 m2 m3 m4 m5 m6 m7 m8

          ↓ Reducer

RemoveMessage(REMOVE_ALL_MESSAGES)
→ 全部删除

m6
→ 加回来

m7
→ 加回来

m8
→ 加回来

最终：

m6 m7 m8
```

与此同时：

```
summary_text = 新摘要
```

又因为 `summary_text` 没有自定义 Reducer，所以它走普通单值语义：

```
旧 summary_text
        ↓
新 summary_text
        ↓
直接覆盖
```

课程里的字段表也专门指出了 `summary_text` 属于普通单值更新字段。

而且它不是每次把以前的摘要忘掉重新总结。

源码在构造下一次摘要时，会同时拿：

```
previous_summary
+
本轮准备被压缩的 messages
```

重新生成新的 `summary_text`。所以可以想象成：

```
第一次压缩：

m1 ~ m20
      ↓
summary_v1

State:
summary_text = summary_v1
messages = m21 ~ m30
```

继续聊：

```
summary_v1
messages = m21 ~ m50
```

第二次又太长了：

```
summary_v1
+
m21 ~ m40
      ↓
summary_v2

State:
summary_text = summary_v2
messages = m41 ~ m50
```

这样历史信息就被逐步浓缩进一个长期摘要。

------

### 最后，把你两个问题合起来

我建议你脑子里形成这样一个模型：

```
               ┌──────────────────┐
               │   ThreadState    │
               │                  │
               │ summary_text     │ ← 旧历史的浓缩
               │ messages         │ ← 当前活跃消息尾部
               │ artifacts        │
               │ todos            │
               │ ...              │
               └────────┬─────────┘
                        │
                 Middleware 加工
                        │
                        ↓
               ┌──────────────────┐
               │   ModelRequest   │
               │                  │
               │ System Prompt    │
               │ summary          │
               │ active messages  │
               │ durable context  │
               └────────┬─────────┘
                        ↓
                       LLM
```

而当 State 太大的时候：

```
ThreadState.messages 太长
        ↓
SummarizationMiddleware
        ↓
老 messages → summary_text
        ↓
提交状态更新：

messages:
  REMOVE_ALL
  + preserved_messages

summary_text:
  new_summary
        ↓
Message Reducer 执行删除 + 保留
        ↓
Checkpoint 保存新的 ThreadState
```

所以你刚才其实抓到了一个很重要的点：

> **Reducer 负责的是“持久状态怎么演化”；上下文压缩 Middleware 决定“我要把状态改成什么样”；真正调用模型之前，又可以基于 State 临时组装 ModelRequest。**

这三层要分开。

我觉得这里理解清楚以后，你对 DeerFlow 的 `State → Middleware → Model` 整条链路会一下清晰很多。



## 字段与合并规则对照表

下表依据 `thread_state.py::ThreadState`、`AgentState` 继承关系及同文件的归约函数。`NotRequired` 说明字段可不出现，**并不**表示出现时自动保留旧值。

| 字段 | 规则 | 本课需要记住的边界 |
| --- | --- | --- |
| `messages` | `full` 模式沿用 `AgentState` 的 `add_messages`；`delta` 模式以 `DeltaChannel(merge_message_writes)` 折叠写入 | 新 ID 追加，同 ID 更新；不能当作普通列表相加。 |
| `artifacts` | `merge_artifacts`：按首次出现顺序合并并去重 | `None` 不更改；`[]` 不会清空已有列表。 |
| `todos` | `merge_todos`：新值非 `None` 时整体替换 | `None` 保留，`[]` 清空；不是逐项按待办 ID 合并。 |
| `goal` | `merge_goal`：新值非 `None` 时整体替换 | `None` 保留；清除目标由目标写入路径处理，不应把 `None` 猜成清除命令。 |
| `delegations` | `merge_delegations`：按 `id` 追加或更新 | 同 ID 不重复；终态不被非终态覆盖；最多保留最近 50 条。 |
| `sandbox` | `merge_sandbox`：相同 `sandbox_id` 的写入幂等 | 不同 ID 冲突时抛错，避免静默切换 Sandbox。 |
| `viewed_images` | `merge_viewed_images`：按路径合并，重复键以新值为准 | `None` 保留，空字典 `{}` 明确清空。 |
| `promoted` | `merge_promoted`：同一工具目录哈希下合并名称 | 目录哈希改变时替换旧集合，防止旧名称指向不同工具。 |
| `skill_context` | `merge_skill_context`：按路径去重并刷新最近读取顺序 | 只保存 Skill 引用，最多 8 条；不存正文。 |
| `task_notes` | 自定义 `TaskNotesChannel` 与 `merge_task_notes` | 属于任务连续性专用通道；其内部规则留给相关专题。 |
| `thread_data`、`title`、`uploaded_files`、`task_history`、`summary_text`、`background_tasks` | 本类型未声明自定义归约器，按普通单值通道更新 | 不应假定它们具有追加或去重行为。 |

`backend/tests/test_thread_state_reducers.py::TestThreadStateAnnotations` 还直接检查 `todos`、`goal`、`artifacts`、`sandbox`、`delegations`、`skill_context` 的 `Annotated` 绑定；这比只测试函数本身多保护了一层：函数即使仍然正确，字段忘记绑定它，图执行时仍会采用错误的合并方式。

***

这一部分最重要的不是把表背下来，而是看懂：**==不同字段代表不同业务语义，所以不能用同一种合并规则==。** 这张表其实是在把 DeerFlow 的状态字段分成几类。

你可以这样理解。

第一类是 **“消息流”**，也就是 `messages`。它不是简单的列表追加，而更像“按消息 ID 做 upsert”：新 ID 就追加，同 ID 就更新原消息。比如：

```
原来：
[h1, a1]

写入：
[a2]

结果：
[h1, a1, a2]
```

但如果写入的是“修改后的 a1”，结果不是再多一个 a1，而是：

```
[h1, 新的a1]
```

所以 `messages` 最核心的语义是：

> **消息是有身份的，不是无脑 append。**

`full` 模式使用 LangGraph 的 `add_messages`，`delta` 模式换成 DeerFlow 自己的 `merge_message_writes`，但它们最终都要保持“新 ID 追加、同 ID 更新”这个语义。

------

第二类是 **“集合型追加”**，典型就是 `artifacts`。

比如当前：

```
artifacts = [
  "/outputs/report.md"
]
```

新节点返回：

```
[
  "/outputs/report.md",
  "/outputs/chart.png"
]
```

最后不是简单拼成：

```
report
report
chart
```

而是：

```
report
chart
```

也就是：

> **保留已有内容 + 加入新内容 + 去重。**

而且这里有一个很容易混淆的边界：

```
artifacts = None
```

表示不改；

但：

```
artifacts = []
```

也不会清空已有列表。

所以 `artifacts` 本质上是一个“只增不随便删”的累积集合。

------

第三类是 **“整体值替换”**，典型是 `todos` 和 `goal`。

比如：

```
todos = [
  t1,
  t2
]
```

如果新节点返回：

```
todos = [
  t3
]
```

结果不是：

```
t1, t2, t3
```

而是：

```
t3
```

因为 DeerFlow 没有把 todo 当成“逐项增量数据库”来维护，而是把这次写入理解为：

> **这是新的完整 todo 列表。**

所以 `todos` 的规则其实非常简单：

```
new is None
    → 保留旧值

new 不是 None
    → 整体替换
```

因此：

```
todos = None
```

是“不修改”，而：

```
todos = []
```

是“明确清空”。

这个区别非常重要。`goal` 也是类似逻辑：`None` 是保留旧目标，新 Goal 是整体替换旧 Goal。

这里你可以顺便建立一个很重要的意识：

> `None` 到底表示“不修改”还是“清空”，**不是 Python/LangGraph 自动决定的，而是 Reducer 的业务约定。**

------

第四类是 **“按业务 ID 更新”**，最典型的是 `delegations`。

这个比 `todos` 稍复杂。

假设原来：

```
[
  {
    id: "call-1",
    status: "in_progress"
  }
]
```

后来又写入：

```
[
  {
    id: "call-1",
    status: "completed"
  }
]
```

不能直接 append，否则会变成：

```
call-1 in_progress
call-1 completed
```

同一个委派出现两份。

所以 DeerFlow 会按照 `id` 找到旧记录，然后更新：

```
call-1 completed
```

而且它还有业务保护：

> 如果已经进入终态，就不能被一个迟到的非终态更新回去。

比如：

```
completed
```

之后又来了一个迟到的：

```
in_progress
```

Reducer 会阻止状态倒退。

此外 `delegations` 最多保留最近 50 条。

所以这个 Reducer 已经不只是“数据结构合并”，而是在实现**业务状态机约束**。

------

第五类是 **“资源身份必须保持一致”**，例如 `sandbox`。

它的逻辑不是“哪个好就用哪个”，而是更严格：

```
旧 sandbox_id == 新 sandbox_id
    → 幂等，允许

旧 sandbox_id != 新 sandbox_id
    → 报错
```

为什么？

因为 Sandbox 是一个运行环境。如果同一个 Thread 跑着跑着，Reducer 悄悄把 Sandbox 从 A 切到 B，之前的文件、命令结果、工作目录可能全都不对应了。

所以这里的设计思想是：

> **有些字段不是“怎么合并”的问题，而是“禁止非法状态迁移”。**

------

剩下几个你现在不用死记，但可以理解它们为什么存在。

`viewed_images` 类似 Map：

```
path → image metadata
```

同路径，新值覆盖旧值；`None` 保留，`{}` 明确清空。

`promoted` 是和“工具目录版本”绑定的。如果工具目录 hash 没变，就合并；如果 hash 变了，就不能继续使用旧工具名集合，因为同一个名字可能已经代表不同工具了。

`skill_context` 是最近读取过的 Skill 引用，按路径去重，同时维护最近访问顺序，而且最多 8 条。它保存的是引用，不是整个 Skill 正文。

`task_notes` 则是更专门的任务连续性状态，现在先知道它有自己的 Channel / Reducer 就够了。

------

还有最后一类非常重要：

```
thread_data
title
uploaded_files
task_history
summary_text
background_tasks
```

这些字段在这里**没有自定义 Reducer**。

所以它们不是自动 append，也不是自动去重，而是普通单值更新语义。lesson-08-thread-state-and-reducers.mdMD

你可以先把整张表压缩成这几个脑图：

```
messages
→ 按 ID 更新 / 追加

artifacts
→ 去重追加

todos / goal
→ None 保留，否则整体替换

delegations
→ 按业务 ID 更新 + 状态保护

sandbox
→ 身份一致，否则报错

其他无 Reducer 字段
→ 普通覆盖
```

这其实就是这张表真正想教你的东西：

> **Reducer 不是为了“合并列表”而存在的。Reducer 的本质，是把字段的业务更新语义编码进 State。**

所以以后你设计 Agent State 时，看到一个字段，首先不应该问“它是什么类型”，而应该问：

> **这个字段下一次被节点写入时，我希望新值和旧值是什么关系？覆盖、追加、去重、按 ID 更新，还是禁止冲突？**

这才是这部分最值得你带走的东西。

## 用一条写入链看清五种主要语义

> [!WARNING]
>
> 这里可以作为细节的了解，其实没有什么可说的点.

下面是**示意推演**，不是一次真实运行日志。每一行的右侧是应用该字段归约规则后的结果；没有写入的字段保持原值。

| 写入 | 合并后的关键结果 | 原因 |
| --- | --- | --- |
| `messages=[助手消息 a2]` | `h1, a1, a2` | 新消息 ID 追加。 |
| `messages=[内容修正后的 a1]` | `h1, 修正后的 a1, a2` | 相同消息 ID 在原位置替换，不再追加一个 `a1`。 |
| `artifacts=["/outputs/report.md", "/outputs/chart.png"]` | 报告、图表各一份 | 按路径去重，同时保留首次出现顺序。 |
| `todos=None`，之后 `todos=[]` | 先保留 `t1`，再变成空列表 | `None` 是无变更，空列表是明确更新。 |
| `goal=None`，之后写入新 Goal | 先保留原目标，再整体换为新目标 | Goal 不是逐属性补丁。 |
| `delegations=[{"id":"call-1", "status":"completed", ...}]` | `call-1` 仍只有一条，状态变为 `completed` | 按 ID 更新，不按列表位置盲目追加。 |

> **`delegations` = 主 Agent 对外委派出去的任务及其执行状态。**

消息的规则来自框架，但 `delta` 模式为节省持久化成本使用了 DeerFlow 自己的 `thread_state.py::merge_message_writes`。它先规范化消息，再按 ID 建索引，依次处理写入；遇到 `RemoveMessage` 时删除对应 ID，遇到 `REMOVE_ALL_MESSAGES` 时清空此前消息并保留该标记之后的写入。`backend/tests/test_delta_channel_state.py::test_merge_message_writes_matches_sequential_add_messages` 将它与 LangGraph 的 `add_messages` 逐次折叠结果比较；同文件的随机差分测试覆盖更广的写入组合。因此两种模式应得到相同的**物化消息列表**，存储方式却不同。

> 第一段讲的是 **`messages` 的 full / delta 两种实现为什么可以认为是等价的**。`full` 模式直接沿用 LangGraph 的 `add_messages`；`delta` 模式为了减少持久化开销，自己实现了 `merge_message_writes`。虽然“怎么存”不一样，但 DeerFlow 要保证最终恢复出来的完整消息列表一样。
>
> full 模式可以理解成每一步都直接维护完整结果。
>
> delta 模式更像只记录这些“操作”,等读取时再折叠这些写入，最终还原成完整消息列表。
>
> **物化消息列表**你就理解成,把所有增量操作真正算完以后，最终得到的完整 `messages`。
>
> 测试 `test_merge_message_writes_matches_sequential_add_messages` 做的事情，就是拿 DeerFlow 的 delta 合并结果，和 LangGraph 原本的 `add_messages` 一步一步算出来的结果进行比较。最终结果必须一样。

对于上面的委派记录，`thread_state.py::merge_delegations` 实际先按 `id` 建表，再让较新的同 ID 记录取代旧记录，同时阻止终态被非终态降级。`backend/tests/test_thread_state_reducers.py::TestMergeDelegations` 分别验证同 ID 更新、终态保护和容量上限。这是 DeerFlow 对委派台账的业务规则，LangGraph 不会替它猜出这些约束。

> `delegations` 的合并规则是 DeerFlow 自己定义的业务规则。设想这样的场景：
>
> ```python
> 旧：
> call-1 → in_progress
> 
> 新：
> call-1 → completed
> ```
>
> `merge_delegations` 会先按 `id` 建一个索引，然后发现都是 `call-1`，于是更新原来的记录，而不是追加第二条。
>
> 同时它还有一个额外规则,不能让状态倒退.比如说 completed → in_progress   ❌
>
> 然后测试又分别验证：
>
> ```
> 同 ID 是否正确更新
> 终态是否不会倒退
> 是否最多保留 50 条
> ```

## 合并规则出错会怎样

归约器错误不一定马上抛异常，更危险的是写入看似成功、下一次读取时数据已经错了：

1. **重复消息**：如果把 `messages` 当列表拼接，同一个 ID 的修正写入会变成两条；后续模型可能同时看到旧答案和新答案。`add_messages` / `merge_message_writes` 的同 ID 替换规则避免这种重复。
2. **状态丢失**：若 `todos=None` 被当作清空，未触碰待办的节点也会抹掉已有计划。`test_thread_state_reducers.py::TestMergeTodos::test_none_new_preserves_existing` 正是这一边界的回归测试。
3. **错误状态延续**：若委派终态可被迟到的 `in_progress` 写入覆盖，之后的状态会倒退；`TestMergeDelegations::test_same_id_terminal_status_is_not_downgraded` 固定了相反的规则。

这里的判断要分两层：先看**函数是否算对**，再看**字段是否真的绑定了该函数**。仅测 `merge_todos(existing, None)` 而不检查 `ThreadState.todos` 的注解，无法证明图实际使用了它。并发节点对同一个普通单值字段写入时还可能发生图级写入冲突；不能以为给字段写了任意一个 reducer，就自动解决了业务冲突。

## Full 与 Delta：状态语义相同，消息保存方式不同

> [!IMPORTANT]
>
> 这里思想很像 Redis 持久化里面的 AOF 和 RDB，然后最终采用混合持久化的思想。注意关联。

第 7 课说 Checkpoint 是图状态的持久版本。这里的 `full`、`delta` 指**消息通道的 Checkpoint 表示方式**，不是两套不同的 Thread 对象。`backend/packages/harness/deerflow/config/database_config.py::DatabaseConfig.checkpoint_channel_mode` 默认 `full`；`thread_state.py::get_thread_state_schema` 在 `full` 时返回 `ThreadState`，在 `delta` 时返回 `DeltaThreadState`。

| 模式 | `messages` 通道 | 读取时得到什么 | 代价 |
| --- | --- | --- | --- |
| `full` | Checkpoint 保存完整消息通道值 | 直接从匹配的完整通道值读取 | 对长对话反复序列化较大的消息列表。 |
| `delta` | `DeltaChannel` 保存逐步写入，并按设定频率保存消息快照 | 沿通道历史折叠，物化为完整消息列表 | 每步写入较小，但读取需要重建，且依赖正确的历史链与通道定义。 |

`backend/tests/test_delta_channel_checkpointers.py::test_delta_storage_shape_and_snapshot_cadence` 的存储断言很直接：非快照 Checkpoint 的原始 `channel_values` 没有完整 `messages`；同一测试通过 `CheckpointStateAccessor.aget` 仍读到 `m1, m2`。`backend/tests/test_threads_checkpoint_mode.py::test_delta_thread_avoids_per_step_full_message_blobs` 则比较两种模式的消息 Blob 数量。这些证据说明 `delta` 改的是持久化形状，不能把“原始 Checkpoint 中看不到完整消息”误判为“消息已丢失”。

`database.checkpoint_delta.snapshot_frequency` 默认是 `10`，控制 `delta` 通道定期保存完整消息快照的频率；本课只需知道频率会影响写入体积与重建成本。`backend/packages/harness/deerflow/runtime/checkpoint_state.py::CheckpointStateAccessor` 用已编译图的通道定义读取物化状态；直接读原始 saver 数据不适合判断 `delta` Thread 的完整消息。切换模式涉及兼容性检查和进程重启；迁移、分支与回滚的操作细节留到第 23 课。

***

这一部分其实在回答一个很具体的问题：

> **同一个 Thread 的 `messages`，Checkpoint 到底是“每次都把完整消息列表存一遍”，还是“只存这一步新增/修改的消息”？**

DeerFlow 支持两种方式：`full` 和 `delta`。但这两种模式**改变的是消息怎么持久化，不是 ThreadState 的业务语义**。也就是说，无论你选哪一种，最后读取出来给 Agent 使用的，都应该是同一份完整消息列表。

先看 `full`。

假设一段对话逐渐变成：

```
第 1 步：
[h1]

第 2 步：
[h1, a1]

第 3 步：
[h1, a1, h2]

第 4 步：
[h1, a1, h2, a2]
```

`full` 模式可以粗略理解成：**每次 Checkpoint 都保存当时完整的 `messages`。**

所以存储形态像这样：

```
Checkpoint 1:
[h1]

Checkpoint 2:
[h1, a1]

Checkpoint 3:
[h1, a1, h2]

Checkpoint 4:
[h1, a1, h2, a2]
```

优点非常直接：

> 读某个 Checkpoint 时，完整消息已经在那里了，直接拿出来就行。

缺点也明显：

> 对话越来越长以后，每走一步都要重复序列化前面所有历史消息。

比如已经有 1000 条消息，这一步只新增 1 条，却可能又要把 1001 条重新保存一遍。文档里说的就是“长对话反复序列化较大的消息列表”。

------

而 `delta` 模式的思想就是：

> **既然这一轮通常只改了很少的消息，那就别每次把完整历史重复存一遍，只记录变化。**

还是刚才那段对话，存储可以粗略理解成：

```
Checkpoint 1:
+h1

Checkpoint 2:
+a1

Checkpoint 3:
+h2

Checkpoint 4:
+a2
```

需要完整消息时，再沿着历史把这些增量折叠起来：

```
+h1
+a1
+h2
+a2
    ↓
[h1, a1, h2, a2]
```

这个过程就是前面提到的**物化**。

所以：

```
磁盘 / Checkpoint 里
可能只保存 delta

        ↓ 重建

程序真正读到的 ThreadState.messages
仍然是完整 messages
```

这就是“**状态语义相同，消息保存方式不同**”最核心的意思。

------

但如果永远只保存增量，又会有一个问题。

假设已经有：

```
delta1
delta2
delta3
...
delta1000
```

现在读取最新状态，如果每次都必须从 `delta1` 开始一路重放到 `delta1000`，读取成本也会越来越高。

所以 DeerFlow 的 `delta` 不是纯粹永远只存增量，而是会**定期保存完整消息快照**。

文档里给出的默认参数是：

```
snapshot_frequency = 10
```

也就是大致可以理解成每隔一定频率保存一次完整消息状态。

例如：

```
Checkpoint 10:
完整快照 [m1...m10]

Checkpoint 11:
+m11

Checkpoint 12:
+m12
```

那读 Checkpoint 12 时，不需要从最开始重放：

```
快照 m1...m10
   +
delta m11
   +
delta m12
   ↓
完整状态
```

所以它是在权衡两个成本：

```
full
写入更重
读取简单

delta
写入更轻
读取需要重建
```

DeerFlow 再通过“定期快照”避免 delta 的读取成本无限增长。

主要就是在权衡 **写入成本** 和 **恢复成本**。简单说：

- 值更小，比如 `5`：快照更频繁，**写入更重、存储更多，但 Run 第一次读取或者恢复更快**。
- 值更大，比如 `50`：快照更少，**写入更轻、存储更省，但读取时要重放更多 delta**。

> 它主要决定的是“从持久化 Checkpoint 重新物化出完整 State 时”的速度，而不是一次 Run 正常执行过程中每一步读取 State 的速度。因为你一开始运行的时候要读到一个正确的 State 嘛，你就得去遍历增量写入的消息。但是你拿到之后，你后续再改这个 State，你顺便去改你内存里面现在的 State，直接就拿到最新的状态，不用再去遍历增量写入了。所以不是每一步都需要遍历增量写入来构建这个状态的。

> [!IMPORTANT]
>
> **怎么确定快照频率**
>
> 我不会把完整快照频率定成一个固定经验值，而是结合业务里的写入频率和状态恢复成本来权衡。
>
> 如果 snapshot_frequency 比较小，就意味着更频繁地保存完整 messages，优点是后续新 Run 加载已有 Thread、或者任务失败恢复时，需要重放的 delta 更少，恢复会更快；代价是==每隔几步就要重新保存一次越来越大的完整消息列表==，写放大会比较明显。
>
> 反过来，如果 snapshot_frequency 比较大，平时大多数 Checkpoint 只保存增量，写入成本更低，但恢复时可能要从最近一次 snapshot 开始重放更多 delta。
>
> 所以我会结合业务场景来看。
>
> 比如一个 Run 本身是长时间运行的 Agent 任务，而且中间会产生很多 Checkpoint，那么正常执行过程中 State 已经在内存里流转，并不需要每一步都重新从持久化状态恢复。这种情况下，可以适当放大 snapshot 间隔，减少频繁保存完整消息带来的写放大。
>
> 但如果这个任务本身失败概率比较高，或者经常需要进程重启、断点恢复，那 snapshot 也不能拉得太大。否则虽然正常运行时省了写入成本，一旦恢复，就要重放很长的 delta 链，恢复时间反而会变长。
>
> 所以本质上我会看两个指标：一是完整 snapshot 的写入成本有多高，二是业务对恢复延迟有多敏感。然后通过压测或者运行指标，选择一个折中值，而不是认为 10 或 20 本身有什么特殊含义。

------

还有一个特别容易误解的地方。

在 `delta` 模式下，你直接去看某个底层 Checkpoint 的原始 `channel_values`，**可能根本看不到完整的 `messages`**。

这不代表消息丢了。

因为底层存的是：

```
增量 + 历史链 + 快照
```

真正读取 ThreadState 时，`CheckpointStateAccessor` 会按照通道定义，把它重新物化成完整消息列表。文档里的测试也专门验证了：原始 Checkpoint 里可能没有完整 `messages`，但通过正确的状态读取路径，仍然能得到 `m1, m2`。

所以这里有一个很重要的分层：

```
Checkpoint 原始存储形态
≠
Agent 最终看到的 State
```

这和数据库有点像。

你底层怎么做 WAL、页、索引，是一种存储实现；业务代码最后 `SELECT` 出来的逻辑数据，是另一层。

------

你现在可以把整个部分压缩成这张图：

```
              messages
                 │
        ┌────────┴────────┐
        │                 │
      full              delta
        │                 │
每步保存完整列表      每步保存消息增量
        │                 │
读取简单              读取时需要重建
写入越来越大          写入更轻
                          │
                    定期保存快照
                          │
                    控制重建成本
```

但无论哪条路：

```
最终物化得到的 messages
应该完全一样
```

这就是这一节真正要掌握的核心。

如果以后面试里聊到 **长对话状态持久化 / Checkpoint 存储优化**，这里其实是一个挺能讲的工程点：

> 随着会话增长，如果每个 Checkpoint 都保存完整消息列表，会产生大量重复序列化和存储，因此可以把消息通道改成增量持久化，再配合周期性快照，在写入体积和状态恢复成本之间做权衡。

这个就比前面 `delegations` 那些 reducer 细节更值得你记。

**这个类比非常好用**。

你可以把 DeerFlow 的 `full / delta` 粗略类比成 Redis 的：

- `full` ≈ **RDB / 全量快照**
- `delta` ≈ **AOF / 增量日志**
- `delta + 定期 snapshot` ≈ **AOF + 周期性重写/快照**

DeerFlow 这里的核心也是同一个权衡：如果每次 Checkpoint 都把完整 `messages` 存一遍，读取简单，但随着历史越来越长，重复写入很多；如果只存每一步消息变化，写入更轻，但恢复时要重放历史，所以再定期插入完整快照，控制恢复成本。

不过要注意一个边界：**只是思想类似，不是实现等价。**

Redis 的 RDB/AOF 是整个数据库层面的持久化机制；DeerFlow 这里说的是 **LangGraph Checkpoint 中 `messages` 这个状态通道怎么表示和保存**。而且 DeerFlow 的 delta 还要处理“同 ID 消息更新、删除消息”等消息语义，不只是机械地记录一条日志。

所以面试时你甚至可以这样理解：

> 这个设计本质上和 Redis 的全量快照与增量日志是同一类工程权衡：全量保存读取简单但写放大明显，增量保存降低写入成本，但需要重放，因此再通过周期性快照限制恢复链长度。

这个类比比死记 `DeltaChannel` 好理解多了。

## 可选验证与课程产出

本课的实践产出就是上面的**字段—归约器对照表**。读者可用一个小测试验证边界，而不必搭建完整 Gateway：在 `backend/tests/test_thread_state_reducers.py::TestMergeViewedImages` 中补充“相同图片路径的新元数据覆盖旧元数据，同时其他路径仍保留”的测试。预期结果可直接由 `thread_state.py::merge_viewed_images` 的字典合并规则推出；现有测试已覆盖不同路径合并与 `{}` 清空，新增测试检验的是另一条边界。

若继续做代码深挖，建议按顺序只看三处：

1. `backend/packages/harness/deerflow/agents/thread_state.py::ThreadState` 与五个主要归约函数：确认表中的字段绑定与返回值。
2. `backend/tests/test_thread_state_reducers.py::TestThreadStateAnnotations`：观察“函数正确但未接入图”的风险如何被测试拦住。
3. `backend/tests/test_delta_channel_state.py::test_merge_message_writes_matches_sequential_add_messages`：确认 Delta 消息重建与 `add_messages` 的结果一致。

面试验收：能用“修正同 ID 消息、追加一个 Artifact、无关节点写入 `todos=None`、更新同 ID 委派”这四步，说明为什么不能统一用覆盖或拼接；能指出 `ThreadState` 的字段注解和测试分别证明什么；能解释 `full` 与 `delta` 的消息表示不同而物化结果应相同。下一课再看这些状态字段如何与模型、工具、Prompt 和 Middleware 一起进入 Lead Agent 的组装过程。

***

> [!NOTE]
>
> 这一节我主要关注的是 DeerFlow 里 ThreadState 的状态合并机制，也就是：一个 Thread 在执行过程中，会被不同节点不断产生局部更新，这些更新最后是怎么合并成完整 State 的。
>
> 这里最核心的问题是，不能把所有字段都用同一种规则处理。
>
> 因为 LangGraph 的节点通常只返回自己修改的字段，而不会每次返回一份完整状态。比如一个节点新增了一条消息，另一个节点新增了 Artifact，还有一个节点更新了待办或者子任务状态。这时候如果统一采用“新值覆盖旧值”，就可能把历史状态丢掉；但如果统一使用列表追加，又可能产生重复数据。
>
> 所以 DeerFlow 的做法是给不同 State 字段绑定不同的 Reducer，也就是不同的合并语义。
>
> 比如 messages 是按消息 ID 来合并的。新 ID 就追加，如果是已经存在的 ID，就更新原来的消息，而不是再追加一份。所以 messages 不能简单理解成普通 List 的 append。
>
> artifacts 则属于累积型状态，它的逻辑是保留已有 Artifact，再把新的 Artifact 合并进来，同时按照路径去重。
>
> todos 和 goal 又不一样。它们采用的是整体替换语义。比如 todos 新值不是 None，就直接替换原来的待办列表；如果是 None，则表示这次节点不修改 todos。这里空列表和 None 的含义也是不一样的，空列表表示明确清空，而 None 表示保持原值。
>
> delegations 可以理解为 Agent 的任务委派台账。它不是简单追加，而是根据委派 ID 更新同一条记录，比如一个子任务从 in_progress 变成 completed，它仍然是同一条 delegation，只是状态发生了变化。同时 DeerFlow 还会防止已经完成的任务被迟到的 in_progress 状态重新覆盖，这里 Reducer 实际上已经包含了一部分业务状态约束。
>
> 所以我对 Reducer 的理解是，它不只是一个“列表合并函数”，而是把某个状态字段的业务更新语义编码进 State。
>
> 也就是说，以后设计 Agent State 的时候，我看到一个字段，不应该只考虑它是什么数据类型，而应该考虑：下一次节点写入这个字段时，新值和旧值到底应该是什么关系，是覆盖、追加、去重、按 ID 更新，还是要禁止某些非法状态变化。
>
> 这一节还有一个比较重要的设计是 messages 的 Full 和 Delta 持久化模式。
>
> Full 和 Delta 并不是两套不同的 ThreadState 业务语义，它们最终恢复出来的完整 messages 应该是一样的，区别只是 Checkpoint 怎么保存消息。
>
> Full 模式比较直接，每次 Checkpoint 都保存当前完整的消息列表。优点是读取简单，但随着对话越来越长，每次 Checkpoint 都重复序列化完整历史，会产生比较明显的写放大。
>
> Delta 模式则只保存这一步产生的消息增量，在重新加载 State 的时候，再从历史增量中把完整 messages 重建出来。这样平时写入会更轻，但是恢复的时候需要额外做增量重放。
>
> 为了避免 Delta 链无限增长，DeerFlow 还会周期性保存完整消息快照，默认 snapshot_frequency 是 10。这样恢复的时候，不需要从最开始重放全部历史，只需要从最近一次完整快照开始，再应用后面的几个 delta。
>
> 所以这里本质上是一个很经典的工程权衡，和全量快照与增量日志的思想比较类似：
>
> 完整快照保存得越频繁，平时写入成本越高，但是 Run 首次加载已有 Thread、进程重启或者故障恢复时，需要重放的增量更少；
>
> 反过来，如果快照间隔比较大，正常运行时写入成本更低，但是状态恢复时要重放更长的 delta 链。
>
> 因此如果面试官问我 snapshot_frequency 怎么设置，我不会说固定设置成 10，而是会根据业务来权衡。一方面看完整 messages 已经有多大、Checkpoint 写入有多频繁；另一方面看业务对故障恢复或者新 Run 加载旧 Thread 的延迟有多敏感，然后通过实际运行指标或者压测找到一个折中值。
>
> 所以这一节我最终总结成两点。
>
> 第一，Agent 的 State 不是普通对象，它是多个节点不断提交局部更新形成的共享状态，因此不同字段必须根据业务语义定义自己的 Reducer。
>
> 第二，状态的逻辑语义和持久化表示可以分开。Full 和 Delta 最终得到的完整 messages 应该一致，只是在 Checkpoint 层通过“全量保存”和“增量保存加周期快照”去权衡写入成本和恢复成本。

