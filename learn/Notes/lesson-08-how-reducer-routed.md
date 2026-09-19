`messages` 的 Reducer  支持几种操作。我还以为，一个字段，就绑定一种操作。那么怎么路由具体的操作的呢，agent 怎么知道什么时候用哪种？

***

对，你之前“一个字段绑定一种操作”的理解差了一点点。

更准确是：

> **一个字段绑定一个 Reducer；**
>
> **但这个 Reducer 本身可以识别多种“写入指令”，然后执行不同操作。**

`messages` 绑定的并不是一个简单的：

```
append(old, new)
```

而更像一个“小型解释器”：

```
def message_reducer(old_messages, writes):
    for write in writes:

        if write 是普通消息:
            if id 不存在:
                追加
            else:
                替换同 id 消息

        elif write 是 RemoveMessage:
            删除对应 id

        elif write 是 RemoveMessage(REMOVE_ALL_MESSAGES):
            清空全部消息

    return new_messages
```

所以不是：

```
messages
 ├─ append reducer
 ├─ delete reducer
 └─ replace reducer
```

而是：

```
messages
    ↓
一个 message reducer
    ↓
根据“这次写进来的东西是什么”
决定 append / replace / remove / clear
```

### 那“操作”是怎么路由的？

**操作实际上编码在节点写入的值里面。**

比如普通 Agent 节点产生了一条新消息：

```
return {
    "messages": [
        AIMessage(
            id="m3",
            content="分析结果..."
        )
    ]
}
```

Reducer 一看：

```
普通 Message
+
m3 这个 id 以前不存在
```

于是：

```
→ append
```

如果写：

```
AIMessage(
    id="m2",
    content="修改后的回答"
)
```

而 `m2` 已经存在：

```
→ replace m2
```

如果写：

```
RemoveMessage(id="m1")
```

Reducer 一看这不是普通消息，而是删除指令：

```
→ delete m1
```

压缩上下文时 Middleware 会故意返回：

```
{
    "messages": [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        *preserved_messages,
    ]
}
```

那么同一个 Reducer 就会解释成：

```
REMOVE_ALL_MESSAGES
→ 清空

preserved_messages
→ 再加入保留消息
```

------

所以你问：

> Agent 怎么知道什么时候用哪种？

这里要特别注意：

**通常不是 LLM 自己在决定“我要调用 append 还是 delete”。**

而是不同的代码路径产生不同的状态写入。

例如：

```
模型正常回答
    ↓
产生 AIMessage
    ↓
Reducer → append / replace
```

而：

```
SummarizationMiddleware 判断上下文过长
    ↓
产生 RemoveMessage(REMOVE_ALL_MESSAGES)
    ↓
Reducer → clear
    ↓
再加入 preserved_messages
```

也就是说：

> **业务代码 / Middleware 决定“我要提交什么状态变化”，Reducer 负责解释“这个变化如何作用到旧状态”。**

这和我们前面讲的那句话正好对应：

> **==节点负责描述发生了什么变化，Reducer 负责决定这个变化如何落到最终 State==。**

所以你现在可以把 `Reducer` 理解得再深一层：

**==Reducer 不是“一种操作”，而是一个字段的“写入协议”==。**

对于 `messages` 来说，这个协议规定了：

```
普通新 id Message     → 新增
同 id Message         → 更新
RemoveMessage(id)     → 删除
REMOVE_ALL_MESSAGES   → 清空
```

这就是为什么 `messages` 一个字段只需要绑定一个 Reducer，却仍然能完成很多种状态变化。