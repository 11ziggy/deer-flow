# 前端怎么看到完整的聊天记录？

这里正好能把你之前学的 **State / Checkpoint / Event** 全串起来。

前端看到的“完整聊天记录”，**主要不是从当前 `ThreadState.messages` 读出来的**，而是从另一套 **RunEventStore / 事件日志** 里恢复出来的。

你可以把 DeerFlow 理解成有三份不同用途的“消息表示”：

```
① RunEventStore / run_events
   ↓
   保存历史发生过的消息事件
   ↓
   给前端展示聊天记录

② ThreadState / Checkpoint
   ↓
   保存 Agent 当前可恢复的执行状态
   ↓
   可能已经压缩：
   summary_text + 最近 messages

③ ModelRequest
   ↓
   Middleware 基于 State 动态组装
   ↓
   真正发给 LLM
```

### 前端历史具体存在哪里？

我查了你 fork 的源码。

DeerFlow 有一个 `RunEventStore`。如果使用数据库实现，就是：

```
DbRunEventStore
```

它的源码注释直接写着：

> 事件持久化到 `run_events` 表。

而且每条记录会带这些核心信息：

```
thread_id
run_id
seq
event_type
category
content
metadata
created_at
```

其中聊天消息就是：

```
category = "message"
```

`list_messages()` 查询的也是：

```
RunEventRow.thread_id == thread_id
RunEventRow.category == "message"
```

然后按照 `seq` 排序分页读取。

所以你可以理解成：

```
run_events

seq  thread_id   run_id   category   content
1    thread-1    run-1    message    用户：你好
2    thread-1    run-1    message    AI：你好
3    thread-1    run-1    message    用户：帮我研究...
4    thread-1    run-1    message    AI：好的...
...
```

这个更像一个**==追加式的历史日志==**。

------

### 那前端重新打开聊天时怎么办？

Gateway 里有专门的：

```
read_visible_message_page()
```

它不是读取：

```
ThreadState["messages"]
```

而是调用：

```
event_store.list_messages(thread_id, ...)
```

然后按 `seq` 分页扫描出可见消息。

所以哪怕当前 State 已经从：

```
m1 m2 m3 ... m100
```

压缩成：

```
summary_text = m1~m70 的摘要

messages =
m71
...
m100
```

前端依然可以从 `run_events` 里查：

```
m1
m2
...
m100
```

因此：

> **上下文压缩不会等价于删除前端聊天历史。**

这是两套不同用途的数据。

------

不过你说的“前端能看到完整的所有信息”，这里要加一个限定：

**前端看到的是完整的“用户可见对话历史”，不是所有内部信息。**

DeerFlow 的 `conversation_reader` 会主动过滤一些消息。

比如源码里：

```
caller.startswith("middleware:")
```

的 Middleware 内部消息不会展示。

某些：

```
subagent 的内部 AI 消息
```

也会隐藏。

此外，如果用户做了“重新生成”“编辑后重跑”，被替代掉的旧 Run 也可以从默认聊天历史里隐藏。

所以实际结构更像：

```
                 RunEventStore
              完整运行事件日志
                     │
        ┌────────────┴────────────┐
        ↓                         ↓
内部事件/隐藏消息              用户可见消息
                                  │
                           conversation_reader
                                  │
                                  ↓
                                前端
```

------

## 这下整个设计就很漂亮了

之前我们说：

> State 解决“Agent 要记住什么”。

现在可以再补一句：

> **Event Store 解决“历史上到底发生过什么”。**

这两个需求完全不同。

比如上下文压缩：

```
历史上发生过：

User A
Assistant B
Tool C
Assistant D
...
```

这些记录可以继续存在 `run_events`。

但 Agent 为了继续执行，不需要把它们全部塞在 State：

```
ThreadState：

summary_text = "之前主要完成了……"

messages =
最近几轮消息
```

然后模型调用时：

```
Middleware：

summary
+ 最近 messages
+ skill
+ delegation
+ system prompt
...
        ↓
ModelRequest
```

所以现在你其实可以把 DeerFlow 的这三个概念非常清楚地区分开：

> **Event Store 是历史记录，Checkpoint / State 是当前执行状态，ModelRequest 是当前模型上下文。**

我觉得这句话你一定要记住。它也解释了你刚才最大的疑惑：**为什么 State 里的旧 messages 可以删掉，但用户刷新页面以后以前聊天记录还在。**