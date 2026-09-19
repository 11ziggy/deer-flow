可以。我们按**“先看整体，再逐块看代码，最后走一遍真实执行”**的顺序，把 3.3 吃透。你先不要纠结每个 import。

### 先看这份代码到底在干嘛

它实现的是一个最小 Agent：

```
用户输入
  ↓
model 节点
  ↓
模型决定：
  ├─ 要调用工具 → tools 节点 → 执行工具 → 回到 model
  └─ 不调用工具 → END
```

同时它还有：

```
State
→ 保存 messages、tool_call_count

Reducer
→ 决定 State 怎么更新

Checkpointer
→ 按 thread_id 保存 State

invoke
→ 第一轮一次性跑完

astream
→ 第二轮边跑边输出
```

这就是整份代码。

------

## ① 先定义 State

> LangGraph 规定“你要提供一个 State Schema”，但里面有哪些字段、每个字段什么含义，由你自己定义。
>
> 而 DeerFlow 也是在 LangChain 默认 Agent State 基础上继续扩展自己的 `ThreadState`。

```
class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    tool_call_count: Annotated[int, operator.add]
```

这里是在规定这张 Graph 的共享 State 长这样：

```
{
    "messages": [...],
    "tool_call_count": 0
}
```

两个字段：

```
messages
→ 保存 HumanMessage / AIMessage / ToolMessage

tool_call_count
→ 累计执行过多少次 Tool Call
```

这里同时把 Reducer 也定义了：==Reducer 不是绑在 Node 上，而是绑在 State 的字段上。看上面代码就知道==。

Reducer 绑定字段的是因为，一个字段最好有一种稳定的“合并语义”。不把变化修改的职责交给 Node，由 State 里面的字段就定下来行为。如果真的由复杂变化，还可以去做自定义的 Reducer。

```
messages → add_messages
tool_call_count → operator.add
```

```
messages 字段
→ 用 add_messages 合并

tool_call_count 字段
→ 用 operator.add 合并
```

所以以后 Node 返回：

```
{"tool_call_count": 1}
```

不是把旧值覆盖成 `1`，而是：

```
旧值 + 1
```

Node 返回：

```
{"messages": [new_message]}
```

也不会覆盖历史消息，而是合并进去。

------

## ② 定义两个 Tool

```
@tool
def add_numbers(left: float, right: float) -> float:
    """计算两个数字的和。"""
    return left + right


@tool
def multiply_numbers(left: float, right: float) -> float:
    """计算两个数字的乘积。"""
    return left * right
```

就是两个普通 Python 函数：

```
add_numbers
multiply_numbers
```

`@tool` 把它们包装成 LangChain Tool。

于是一个 Tool 同时有：

```
名字
描述
参数 Schema
真正的执行函数
```

然后：

```
TOOLS = [add_numbers, multiply_numbers]
```

告诉程序：

> 当前 Agent 允许使用这两个工具。

再建立一个：

```
TOOLS_BY_NAME = {item.name: item for item in TOOLS}
```

结果大概是：

```
{
    "add_numbers": add_numbers,
    "multiply_numbers": multiply_numbers
}
```

后面模型说：

```
我要调用 multiply_numbers
```

Runtime 就能通过名字找到真正的 Tool。

------

## ③ 创建 Model

```
model = ChatOpenAI(
    model=...,
    api_key=...,
    base_url=...,
    temperature=0,
    timeout=60,
)
```

这就是前面说的：

> **Model = LLM API 的适配对象。**

然后：

```
model_with_tools = model.bind_tools(TOOLS)
```

意思不是执行 Tool。

而是：

> 告诉模型：“你现在可以选择这两个工具。”

所以以后调用：

```
model_with_tools.invoke(messages)
```

模型可能返回：

```
AIMessage(content="...")
```

也可能返回：

```
AIMessage(
    tool_calls=[
        {
            "name": "multiply_numbers",
            "args": {"left": 15, "right": 4}
        }
    ]
)
```

这里只是**决策**。

------

## ④ 第一个 Node：`call_model`

```
def call_model(state: AgentState) -> dict:
    response = model_with_tools.invoke(
        [
            SystemMessage(...),
            *state.get("messages", []),
        ]
    )

    return {"messages": [response]}
```

这个 Node 很简单。

输入：

```
当前 State
```

先拿：

```
state["messages"]
```

比如现在：

```
HumanMessage("计算 15 × 4")
```

然后加一个 SystemMessage，一起给 Model：

```
SystemMessage
HumanMessage
```

模型做一次决策。

假设模型决定使用工具，得到：

```
AIMessage(
    tool_calls=[multiply_numbers(15, 4)]
)
```

然后 Node 返回：

```
{"messages": [response]}
```

注意：

**它只返回新产生的 AIMessage。**

不是：

```
return 整个 State
```

Reducer `add_messages` 会负责把新消息合并回去。

于是：

```
旧 State：

HumanMessage

        ↓ call_model

新增：

AIMessage(tool_call)

        ↓ Reducer

新 State：

HumanMessage
AIMessage(tool_call)
```

这就是我们前面说的：

> Node 产生增量，Reducer 负责合并。

------

## ⑤ 第二个 Node：`execute_tools`

这个稍微复杂一点。

```
def execute_tools(state: AgentState) -> dict:
    last_message = state["messages"][-1]
```

先拿最后一条消息。

为什么？

因为正常情况下现在最后一条就是：

```
AIMessage(tool_calls=[...])
```

然后：

```
for tool_call in last_message.tool_calls:
```

遍历模型要求执行的工具。

例如：

```
tool_call = {
    "name": "multiply_numbers",
    "args": {
        "left": 15,
        "right": 4
    },
    "id": "call_123"
}
```

先拿名字：

```
tool_name = tool_call["name"]
```

再找到真正的 Tool：

```
selected_tool = TOOLS_BY_NAME.get(tool_name)
```

然后才真正执行：

```
result = selected_tool.invoke(tool_call["args"])
```

这一步才发生：

```
15 × 4 = 60
```

注意前面 Model 完全没有执行这个函数。

------

工具执行完还不能直接结束。

要创建：

```
ToolMessage(
    content="60",
    tool_call_id=tool_call["id"],
    name=tool_name,
)
```

这相当于告诉模型：

```
你刚刚请求的 call_123
已经执行完了，
结果是 60。
```

最后 tools Node 返回：

```
{
    "messages": results,
    "tool_call_count": len(last_message.tool_calls),
}
```

例如：

```
{
    "messages": [ToolMessage("60")],
    "tool_call_count": 1
}
```

Reducer 再合并：

```
messages → 追加 ToolMessage

tool_call_count
0 + 1 → 1
```

这就是 Tool Node 的全部职责。

------

## ⑥ 条件 Edge：`route_after_model`

```
def route_after_model(state):
    last_message = state["messages"][-1]

    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "tools"

    return END
```

这个函数**什么都不执行**。

只回答：

> 下一步去哪？

如果 Model 返回：

```
AIMessage(tool_calls=[...])
```

那么：

```
→ tools
```

如果 Model 返回：

```
AIMessage(content="最终结果是 60")
```

并且没有 `tool_calls`：

```
→ END
```

所以这是：

```
控制流判断器
```

不是 Node 的业务处理逻辑。

------

## ⑦ 把 Node 和 Edge 组装成 Graph

现在组件都有了：

```
State
Model Node
Tool Node
条件判断
```

开始搭图。

```
builder = StateGraph(AgentState)
```

创建一张：

> 使用 `AgentState` 的状态图。

加入两个 Node：

```
builder.add_node("model", call_model)
builder.add_node("tools", execute_tools)
```

得到：

```
model
tools
```

定义入口：

```
builder.add_edge(START, "model")
```

即：

```
START → model
```

然后定义：

```
builder.add_conditional_edges(
    "model",
    route_after_model,
    {"tools": "tools", END: END},
)
```

也就是：

```
model
 ↓
route_after_model

有 tool_calls → tools
无 tool_calls → END
```

再：

```
builder.add_edge("tools", "model")
```

工具执行完必须回 Model：

```
tools → model
```

最终：

```
START
  ↓
model
  ↓
有 tool_calls？
 ├─ YES → tools
 │          ↓
 │        model
 │          ↓
 │         ...
 │
 └─ NO → END
```

这就是一个最小 ReAct / Tool Calling Agent Loop。

------

## ⑧ 接入 Checkpointer

```
checkpointer = InMemorySaver()
```

创建一个内存版 Checkpointer。

然后：

```
graph = builder.compile(
    checkpointer=checkpointer
)
```

意思是：

> 把刚刚定义好的图编译成真正可运行的 Graph，同时接入状态保存组件。

再定义：

```
config = {
    "configurable": {
        "thread_id": "calculator-demo"
    }
}
```

以后所有使用这个 config 的 Run，都属于：

```
Thread: calculator-demo
```

所以 Checkpointer 能知道：

> 这些 State 应该保存到哪个 Thread 下。

### Checkpointer 何时发挥作用

> 每一次经过一个节点然后 State 更新之后，Checkpointer 就会保存一次 Checkpoint

```
Node 执行
→ Node 返回 State 更新
→ Reducer 合并
→ 得到新的 State
→ Checkpointer 保存一个 Checkpoint
→ 继续下一个 Node
```

所以像：

```
model → 保存
tools → 保存
model → 保存
```

>下一次 Run 开始：按 `thread_id` 读档。Checkpointer 会先加载上一轮保存的 State。

然后下一轮 Run 开始时，如果还是同一个：

```
thread_id = "calculator-demo"
```

Runtime 会：

```
根据 thread_id
→ 找到这个 Thread 之前保存的状态
→ 加载历史 State
→ 再把这次新的 input 合进去
→ 开始本轮 Run
```

所以你的理解可以压成一句：

> **Run 内：不断存档；下一次 Run 开始：按 `thread_id` 读档。**



------

# ⑨ 第一轮：`run_first_turn()`

现在真正开始执行。

```
final_state = graph.invoke(
    {
        "messages": [
            HumanMessage("计算 15 × 4，必须使用工具。")
        ],
        "tool_call_count": 0,
    },
    config=config,
)
```

初始 State：

```
messages:
  HumanMessage("计算 15 × 4")

tool_call_count:
  0
```

然后 `invoke()` 启动一次 Run。

### 第一步

```
START → model
```

`call_model()` 读取：

```
HumanMessage("15 × 4")
```

模型返回：

```
AIMessage(
    tool_calls=[multiply_numbers(15,4)]
)
```

State 变成：

```
HumanMessage
AIMessage(tool_call)
```

### 第二步

Edge 判断：

```
有 tool_calls
→ tools
```

### 第三步

`execute_tools()` 真正执行：

```
multiply_numbers(15, 4)
```

得到：

```
60
```

返回：

```
ToolMessage("60")
tool_call_count += 1
```

State：

```
HumanMessage
AIMessage(tool_call)
ToolMessage("60")

tool_call_count = 1
```

### 第四步

固定 Edge：

```
tools → model
```

再次进入 `call_model()`。

这次 Model 看到完整 messages：

```
HumanMessage: 15 × 4
AIMessage: 我要调用 multiply
ToolMessage: 60
```

模型于是返回：

```
AIMessage(content="60")
```

### 第五步

Edge 判断：

```
没有 tool_calls
→ END
```

Run 结束。

最终 State：

```
HumanMessage
AIMessage(tool_call)
ToolMessage("60")
AIMessage("60")

tool_call_count = 1
```

所以：

```
final_state["messages"][-1].content
```

就是：

```
60
```

而：

```
final_state["tool_call_count"]
```

就是：

```
1
```

这就是第一轮完整执行过程。

------

# ⑩ 第二轮为什么能知道“刚才结果”？

接下来：

```
async for chunk, metadata in graph.astream(
    {
        "messages": [
            HumanMessage("在刚才结果上加 5，必须使用工具。")
        ]
    },
    config=config,
    stream_mode="messages",
):
```

注意：

这里**只传了最新的一条 HumanMessage**：

```
在刚才结果上加 5
```

没有手动重新传：

```
15 × 4
60
```

为什么还能知道？

因为还是：

```
config=config
```

而这个 config 中：

```
thread_id = calculator-demo
```

Checkpointer 会先加载上一轮保存的 State。

所以实际 State 变成：

```
第一轮完整 messages
+
新的 HumanMessage("在刚才结果上加 5")
```

模型因此知道：

```
刚才结果 = 60
```

然后继续：

```
Model
→ add_numbers(60, 5)

Tool
→ 65

Model
→ "65"

END
```

最终：

```
tool_call_count = 2
```

因为第一轮一次 + 第二轮一次。

------

# 最后把整个文件压成一张图

你现在重新看这个 `.py`，其实只有这条主线：

```
定义 State
  ↓
定义 Tool
  ↓
创建 Model + bind_tools
  ↓
定义 model Node
  ↓
定义 tools Node
  ↓
定义条件 Edge
  ↓
StateGraph 把它们连起来
  ↓
接 Checkpointer
  ↓
compile 成 graph
  ↓
invoke / astream 启动 Run
```

运行时则是：

```
Thread State
    ↓
Model Node
    ↓
AIMessage
    ↓
Edge 判断
    ↓
Tool Node
    ↓
ToolMessage
    ↓
Reducer 更新 State
    ↓
Model Node
    ↓
...
    ↓
END
```

你现在如果能把这两张图自己复述出来，**3.3 的主体已经吃透 80% 以上了**。剩下比较细的 `AIMessageChunk`、`metadata`、`asyncio` 都属于流式输出细节，不影响你理解最小 Agent 的核心。