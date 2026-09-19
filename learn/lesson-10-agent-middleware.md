# 第 10 课：Agent Middleware

第 9 课已经把配置、模型、工具和提示组装成 Lead Agent Graph。本课回答：**图开始执行后，DeerFlow 怎样在模型与工具调用的关键时点加入通用行为，而不把每种行为写进工具或 Agent 主循环？** 它位于“Runtime 组装 Middleware → 模型决定是否调用工具 → 工具结果回到状态”这一段请求主线，也是第 6～10 课请求链的最后一环。核心阅读约 35～45 分钟。

前置知识是第 5 课的 Agent Loop、第 8 课的状态更新和第 9 课的 `create_agent()` 输入。本课必须掌握钩子何时运行、状态更新与调用包装的区别，以及为什么列表顺序会改变结果。摘要质量、长期记忆策略和完整故障处理分别留到第 18、19、25 课；工具的 Schema 与运行上下文留到第 11 课。
***

## 引入

可以。你先把 Middleware 理解成 **Agent Runtime 的“统一拦截与扩展层”**，后面这一课会顺很多。

### 1. 中间件是什么？

在 DeerFlow 里，Middleware 是一组**按顺序注册到 Agent 上的组件**。它们不负责 Agent 的核心推理，也不等同于 Tool，而是在 Agent 执行到某些关键时点时自动介入。

最核心的执行位置是：

```
进入 Agent
   ↓
before_agent
   ↓
before_model
   ↓
wrap_model_call → LLM
   ↓
after_model
   ↓
wrap_tool_call → Tool
   ↓
回到 before_model
   ↓
...
   ↓
after_agent
```

也就是说，可以把它想成：

> **Agent 主流程旁边预留了一系列“插槽”，Middleware 可以在这些插槽里加入通用逻辑。**

而且并不是所有 Middleware 都经过同一个地方。有的关心模型调用，有的关心工具调用，有的只在 Agent 开始或结束时运行。如果类比 Java，你可以暂时把它理解成 **Filter + Interceptor + AOP 的综合体**，只是它专门围绕 Agent 生命周期设计。

### 2. 为什么需要 Middleware？

> [!NOTE]
>
> 我理解 Agent Middleware 的核心，不是因为这些逻辑不能直接写在 Agent Loop 或 Graph 节点里，而是为了让 Agent 的核心执行流程和各种运行策略解耦。
>
> 最基础的 Agent Loop 其实很简单，就是模型决定是否调用工具，工具结果回来之后再交给模型继续判断，也就是一个“模型 ↔ 工具”的循环。
>
> 但实际做复杂 Agent 时，会不断往这个循环里加入很多逻辑，比如上下文压缩、异常重试、循环检测、权限控制、日志、长期记忆等。如果这些逻辑全部直接写进 Loop 或各个节点里，核心流程会越来越臃肿，不同 Agent 之间也很难复用。
>
> 所以 LangChain 提供 Middleware 作为 Agent Loop 的扩展机制，在模型调用前后、工具调用边界、Agent 开始和结束等关键位置预留 Hook。框架负责在正确时机触发这些 Hook，上层应用只需要实现自己的策略。
>
> 放到 DeerFlow 里，就是：LangChain 提供 Middleware 机制，LangGraph 负责运行整个 Graph，DeerFlow 则把摘要、循环检测、工具异常处理、长期记忆等具体能力实现成 Middleware，再组装到 Agent 上。
>
> 所以我会把它总结成一句话：
>
> **Middleware 的价值，是在不重写核心 Agent Loop 的情况下，让上层应用能够以可插拔、可复用的方式扩展 Agent 的运行行为。**



核心原因是：

> **Agent 有大量“横切逻辑”，它们很重要，但又不属于 Agent Loop、LLM 或某个具体 Tool。**
>
> **横切逻辑**，就是：
>
> > **它不属于某一个核心业务步骤，但会“横跨”很多步骤、很多模块反复出现的通用逻辑。**
> >
> > 不属于 Agent 核心业务流程本身，但需要在核心流程的多个关键位置统一执行的通用规则。

这里真正的“主业务逻辑”是：**模型思考、决定调用工具、拿到结果、继续思考。**

但你还会有很多附加需求：

- 每次调用 LLM 前检查上下文是否过长
- 每次调用 Tool 时统一处理异常
- 每次 LLM 返回后检测是否陷入循环
- 整个 Agent 结束后写入长期记忆
- 给模型调用统一加日志、监控、重试

这些逻辑不是“搜索工具自己的事情”，也不是“LLM 自己的事情”，但它们会作用在很多地方，所以叫**横切逻辑**。

例如 DeerFlow 有：

- 上下文注入
- 历史消息摘要
- LLM 异常重试
- Tool 异常处理
- 循环调用检测
- 自动生成标题
- 长期记忆写入

这些东西如果没有 Middleware，很容易变成：

```python
while agent_running:

    # 注入上下文
    ...
    
    # 检查是否摘要
    ...

    try:
        response = model(...)
    except:
        # 模型异常处理
        ...

    # 循环检测
    ...

    for tool in response.tool_calls:
        try:
            result = tool(...)
        except:
            # 工具异常
            ...

    # 记忆处理
    ...
```

Agent 主循环会越来越臃肿。

所以 Middleware 实际解决的是一个很经典的工程问题：

> **把“核心流程”和“通用运行策略”分离。**

Agent Loop 只负责：

```
模型 → 工具 → 模型 → 工具……
```

而 Middleware 负责：

```
这个模型调用之前要不要摘要？
这个工具失败了怎么办？
模型是不是陷入循环？
任务结束后要不要写长期记忆？
```

课本列出的 DeerFlow 实例正好说明了这一点。

------

### 3. 它在代码架构和执行流程的哪里？

这里最好分成 **“组装阶段”和“运行阶段”** 看。

#### 组装阶段

你上一课学的是 Agent 组装：

```
Model
Tools
System Prompt
State
Middleware
       ↓
create_agent(...)
       ↓
Agent Graph
```

我刚刚也核对了你 fork 的 DeerFlow 源码，在：

```
backend/packages/harness/deerflow/agents/lead_agent/agent.py
```

大致就是：

```
middlewares = build_middlewares(...)

graph = create_agent(
    model=...,
    tools=final_tools,
    middleware=middlewares,
    system_prompt=...,
    state_schema=...
)
```

所以 **Middleware 本身也是 Agent Graph 的一个组装输入**。

具体 Middleware 实现主要放在：

```
deerflow/agents/middlewares/
```

例如：

```
summarization_middleware.py
loop_detection_middleware.py
memory_middleware.py
title_middleware.py
tool_error_handling_middleware.py
...
```

课本也明确说明：`build_middlewares()` 先构造 Middleware 列表，最后交给 `create_agent()`。

#### 运行阶段

Agent 真正运行以后，LangChain 把这些 Middleware 接进 Agent 的执行生命周期。

比如一次：

```
用户：帮我搜索资料并总结
```

可能变成：

```
进入 Graph
   ↓
DynamicContextMiddleware
补日期、记忆等上下文
   ↓
SummarizationMiddleware
检查历史是否太长
   ↓
LLMErrorHandlingMiddleware
包住这次模型调用
   ↓
LLM
   ↓
LoopDetectionMiddleware
检查模型是不是一直调用同一个工具
   ↓
ToolErrorHandlingMiddleware
包住 Tool 调用
   ↓
Tool
   ↓
再次调用 LLM
   ↓
...
   ↓
MemoryMiddleware
任务结束后提交长期记忆
```

这就是 Middleware **真正发挥作用的位置**。课本第二部分实际上就是沿一次请求把这件事情展开。

这里还有一个非常重要的区分：

```
before / after
→ 更像生命周期 Hook
→ 经常读取 State、返回 State 更新

wrap_model_call / wrap_tool_call
→ 更像拦截器
→ 真正包住一次调用
→ 可以修改请求、处理异常，甚至不调用 handler
```

这个区别是本课最值得理解的东西之一。

------

### 4. 这份课本的推进逻辑怎么样？

整体上我认为**逻辑是好的，而且比前面几课更有整体视角**。

它实际走的是：

```
① Middleware 插在哪里
        ↓
② 沿一次真实请求看它怎么运行
        ↓
③ Middleware 顺序为什么重要
        ↓
④ 自己实现一个最小 Middleware
        ↓
⑤ 面试验收
```

也就是：

> **位置 → 运行 → 细节规则 → 实践 → 输出**

这个顺序是合理的。尤其第二部分“沿一次请求看状态和调用边界”很重要，因为它把前面的抽象 Hook 放回了真正的 Agent Loop 里。

不过我觉得有一个小问题：

**第一部分一下子给了六种 Hook、正序/反序、包装嵌套，信息密度有点高。**

所以我们实际学习时，我建议稍微改一下顺序：

> **先建立 Middleware = Agent Runtime 拦截层这个整体模型 → 再沿一轮 Agent Loop 看六个位置 → 然后一个 Hook 一个 Hook 学 → 最后再研究顺序。**

这样你不会一开始就在 `before_agent / before_model / after_model / wrap_tool_call` 这些名字里迷路。

你现在只需要先记住这一张总图：

```
                  Middleware
                      ↓
用户 → Agent Graph → LLM → Tool → LLM → ... → 结束
          ↑           ↑      ↑                ↑
       进入处理     模型处理 工具处理        退出处理
```

**Middleware 不改变 Agent“LLM ↔ Tool”的核心循环，而是在这个循环的关键边界上插入运行规则。**

这个理解站稳以后，后面学六种 Hook 就只是回答一个问题：

> **“我想插入的这段逻辑，应该插在哪个时点？”**

这其实就是第 10 课真正的主线。

## 中间件插在哪里

Agent Middleware 是一组挂在 Agent 执行流程上的通用组件。DeerFlow 会先把这些 Middleware 按顺序组装好，再通过 `create_agent(middleware=...)` 交给 LangChain。

Agent 运行时，LangChain 会在几个关键时点自动触发对应的 Middleware：例如进入 Agent、调用模型前后、执行工具时、Agent 结束时。DeerFlow 则负责在这些时点实现具体策略，比如上下文注入、摘要、循环检测、错误处理和记忆写入。

因此可以把 Middleware 理解成：**DeerFlow 定义规则，LangChain 负责把这些规则插入 Agent 的执行流程中。** 模型提供商只负责处理实际发送给它的模型请求，并不知道这些 Middleware 的存在。

一次正常的 Agent 调用大致会经过下面这些位置；如果模型连续调用工具，中间的“模型 → 工具 → 模型”部分会循环执行：

```text
before_agent（进入图时一次）
  → before_model（每次模型调用前）
  → wrap_model_call → 模型 → 返回 AIMessage
  → after_model（每次模型响应后、工具执行前）
  → 若有 tool_calls：wrap_tool_call → 工具 → ToolMessage → 回到 before_model
  → 若无待执行工具：after_agent（正常离开图时一次）
```

这张图只表示默认流程；

中间件可以返回状态更新、跳转，或在包装钩子中短路调用。`before_agent` / `after_agent` 的“一次”指一次图调用，不应推断为跨中断、重试和恢复的整个业务任务恰好一次。

异常也可能让执行无法走到正常退出钩子。

> **钩子（hook）**：程序提前留好的“可插入位置”。当执行流程走到这里时，框架会自动调用你挂在这里的逻辑。

| 钩子 | 看到什么、能做什么 | 本课实例 |
| --- | --- | --- |
| `before_agent` | 进入图时读取当前状态和运行上下文；可返回字段更新 | `DynamicContextMiddleware` 注入本轮日期及可用记忆。 |
| `before_model` | 每次模型调用前读取状态；返回的字典按第 8 课的字段合并规则进入状态 | `DeerFlowSummarizationMiddleware` 达到触发条件时压缩历史。 |
| `wrap_model_call` | 收到模型请求和 `handler`；可修改本次请求、调用或不调用 `handler`、处理响应与异常 | `LLMErrorHandlingMiddleware` 处理模型调用错误。 |
| `after_model` | 模型的 `AIMessage` 已进入状态，工具尚未执行；可检查或更新状态 | `TitleMiddleware` 尝试写标题；`LoopDetectionMiddleware` 检查重复工具请求。 |
| `wrap_tool_call` | 每个待执行工具调用各经过一次；可放行、拒绝或改写结果 | `ToolErrorHandlingMiddleware` 将普通工具异常转为错误 `ToolMessage`。 |
| `after_agent` | 图正常结束时读取最终状态 | `MemoryMiddleware` 在启用且满足条件时把对话交给记忆管理器。 |

当前环境安装的 LangChain 1.3.14 在 `langchain.agents.factory.create_agent` 中把这些钩子接成图：`before_agent` 和 `before_model` 按列表正序执行，`after_model` 和 `after_agent` 按反序执行；包装钩子按“列表靠前者包住靠后者”嵌套。外部版本的行为可能变化，本课也用 [LangChain 官方中间件说明](https://docs.langchain.com/oss/python/langchain/middleware/custom) 核对。`backend/packages/harness/deerflow/agents/lead_agent/agent.py::build_middlewares` 则先取共享运行中间件，再附加 Lead Agent 的组件；最终列表传入 `create_agent()`。所以**列表位置决定执行关系，钩子种类决定执行时点**，不能把列表理解为所有方法共用的一条正序流水线。

***

`build_middlewares()` 会先把 DeerFlow 需要的各种 Middleware 按顺序组装成一个列表，再把这个列表传给 `create_agent()`。

但这个列表并不是简单地“从前到后全部执行一遍”。不同类型的 Hook，有不同的执行规则：

- `before_agent`、`before_model`：按列表顺序执行
- `after_model`、`after_agent`：按列表反向执行
- `wrap_model_call`、`wrap_tool_call`：像一层层套起来的包装器，列表靠前的在外层

所以可以记一句：

> **==Middleware 在列表里的位置，决定它和其他 Middleware 的先后关系；Hook 的类型，决定它具体在 Agent 流程的哪个时点执行==。**

因此，看 Middleware 顺序时，不能只看它在列表里排第几，还要先看它使用的是哪一种 Hook。

## 沿一次请求看状态和调用边界

> **先明确一个边界**：
>
> LangChain 负责把 Middleware 接进 Agent Graph，LangGraph 负责运行这个 Graph；真正被调用的 Middleware 逻辑，是 DeerFlow 自己实现的代码。

假设用户请求：“查找资料并写一段摘要”。

Agent 进入 Graph 后，首先会执行 `before_agent` 阶段的 Middleware，为这一轮运行准备上下文。例如：

- `DynamicContextMiddleware`：补充当前日期、已召回的记忆等动态上下文；
- `ThreadDataMiddleware`：准备当前 Thread 对应的路径；
- `UploadsMiddleware`：检查这一轮是否有上传文件。

这些准备完成后，Agent 才进入第一次模型调用。

在模型真正调用之前，会执行 `before_model`。这里一个重要的例子是 `DeerFlowSummarizationMiddleware`：

> 如果历史消息已经过长并达到摘要条件，它会先压缩历史，再让模型继续运行。

它返回的状态更新大致是：

```json
{
    "messages": [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        *result.preserved_messages
    ],
    "summary_text": result.summary_text,
}
```

这里做了两件事：

1. 用压缩后需要保留的消息替换原来的长历史；
2. 把生成的摘要写入 `summary_text`。

这些内容不会直接修改 State，而是作为一次 State 更新返回，之后仍按照第 8 课讲过的 Reducer / 状态合并规则写入。

如果当前历史还没有达到摘要条件，或者没有得到可用摘要，这个 Middleware 可以直接返回 `None`，State 不发生变化。

这一段最重要的是理解：

> **上下文摘要是 DeerFlow 在** `**before_model**` **阶段主动完成的，然后才把处理后的上下文发送给模型；并不是模型提供商自动帮它缩短上下文。**

假设模型一次返回了两个 `tool_calls`。

这时会先进入 `after_model` 阶段。要注意：

> **此时模型已经提出了工具调用请求，但工具还没有真正执行。**

所以 `after_model` 看到的是“模型想调用哪些工具”，而不是工具执行结果。

DeerFlow 会在这个阶段做一些检查。例如：

- `TitleMiddleware`：在满足条件时尝试生成会话标题。它不代表整个任务已经结束，因为当前这条助手消息仍可能正在请求工具。
- `LoopDetectionMiddleware`：检查模型是否反复请求相同工具。如果只是达到警告条件，就先记录一个提醒，等下一次模型调用前再加入上下文；如果达到硬停止条件，则直接移除这次工具调用，阻止 Graph 继续进入工具执行。

这里最重要的是：

> `**after_model**` **处理的是“模型刚刚做出的决定”，而不是工具执行后的结果。** fileciteturn0file0L46-L46

接下来，如果仍然存在 `tool_calls`，Graph 才真正开始执行工具。

每个工具调用都会经过 `wrap_tool_call`。DeerFlow 的 `ToolErrorHandlingMiddleware` 会把工具执行包起来，大致逻辑是：

```python
try:
    result = handler(request)
except GraphBubbleUp:
    raise
except Exception as exc:
    return build_error_tool_message(exc)

return normalize_tool_result(result)
```

可以把它理解成：

```
Middleware
   ↓
尝试调用 Tool
   ↓
成功 → 整理成标准 ToolMessage
失败 → 转成错误 ToolMessage
```

普通工具异常不会直接让整个 Agent 崩掉，而是会被转换成带原 `tool_call_id` 的错误 `ToolMessage`，再交回模型。这样模型还能知道“刚才这个工具调用失败了”，并决定接下来怎么处理。

但有一类异常不能这样处理：LangGraph 用来控制执行流程的中断异常，例如 `GraphBubbleUp`。这类异常必须继续向上抛，因为它表示的是“Graph 执行流程需要中断或切换”，而不是普通工具执行失败。

所以这里要区分两层：

```
模型调用失败
→ LLMErrorHandlingMiddleware 处理

工具调用失败
→ ToolErrorHandlingMiddleware 处理
```

两者虽然都是异常处理，但发生在不同调用边界，不能混在一起理解。

工具执行完成后，Graph 会把每个工具调用对应的结果追加到消息序列中。这里需要特别注意消息配对关系：

```
助手消息：tool_calls = [call_1, call_2]
工具消息：tool_call_id = call_1
工具消息：tool_call_id = call_2
```

也就是说，助手发出的工具请求和工具返回的结果必须保持对应关系，不能在中间把它们拆开。尤其是循环工具调用检测产生的警告，不应直接插入到助手工具请求和对应工具结果之间，否则可能破坏模型所需的消息结构。也就是工具调用产生的时候你可能就已经用 `after_model` 检测到它重复调用了，但是必须要在工具结果之后再拼接上这个给 LLM 的反馈。

因此，警告不会在 `after_model` 阶段立即插入当前消息序列，而是先保存下来，等到后续的 `wrap_model_call` 中，在完整的工具结果已经追加之后，再把警告加入下一次模型调用的上下文。这样既能让模型看到警告，又能保持：

```
助手工具请求
→ 对应工具结果
→ 警告信息
→ 下一次模型调用
```

这也是为什么“记录警告”和“把警告加入模型上下文”是两个不同的时机：前者发生在 `after_model`，后者要等工具结果完整返回后，在下一次 `wrap_model_call` 中完成。

当模型最终不再请求工具，Graph 会正常结束，并进入 `after_agent` 阶段。

DeerFlow 的 `MemoryMiddleware.after_agent` 会在这里检查：

- 长期记忆功能是否开启；
- 当前是否有有效的 Thread ID；
- 是否存在需要处理的对话消息。

条件满足后，它会把当前用户和这轮对话消息交给记忆管理器：

```
Agent 正常结束
      ↓
MemoryMiddleware.after_agent
      ↓
检查是否需要处理记忆
      ↓
memory_manager.add(...)
```

这里最重要的一点是：

> `**after_agent**` **做的是“把这轮对话提交给记忆系统处理”，并不代表长期记忆已经完成抽取，更不代表以后一定会被召回。**

也就是说，本课只需要理解 **MemoryMiddleware 在 Agent 生命周期的最后阶段负责触发记忆更新**。至于记忆具体如何提取、保存，以及以后什么时候能召回，是后面的第19课长期记忆课程再讨论的内容。

## 顺序为什么会改变行为

`build_middlewares()` 最终返回的是一个有序列表，但不同 Hook 对“前后顺序”的解释并不一样。

先看 `before_*`。

`ThreadDataMiddleware` 排在 `UploadsMiddleware` 前面，因此进入 Agent 时，会先准备当前 Thread 的路径，再由 `UploadsMiddleware` 去检查上传文件。也就是说，后面的 Middleware 可以依赖前面的 Middleware 已经准备好的状态。

再看 `wrap_*`。

工具调用的 Middleware 是一层层包起来的。比如：

```
ToolProgressMiddleware
    ↓
ToolErrorHandlingMiddleware
    ↓
真正执行 Tool
```

`ToolProgressMiddleware` 在外层，`ToolErrorHandlingMiddleware` 在内层。

调用工具时，执行顺序是：

```
进入 ToolProgress
→ 进入 ToolErrorHandling
→ 执行 Tool
→ ToolErrorHandling 处理结果
→ 回到 ToolProgress
```

所以内层可以先把工具结果整理成标准 `ToolMessage`，外层再根据这个结果判断工具是否持续没有进展。

因此，Middleware 列表中的“排在前面”，不能简单理解为“一定先执行完”：

- `before_*`：排在前面的先执行；
- `after_*`：排在前面的反而后执行；
- `wrap_*`：排在前面的在外层，**先进入、最后返回**。

所以调整 Middleware 顺序时，真正要考虑的是：

> **这个 Middleware 运行时依赖哪些输入？它产生的结果，又希望哪些 Middleware 能看到？**

先确定这种依赖关系，再决定它应该放在列表的什么位置，而不是只看类名顺序。

## 可选实践：统计每次模型响应请求的工具数

大纲建议做第一个带测试的 DeerFlow 功能改动。可以选择一个范围很小的实现：在 Lead Agent 中加入 `ToolCallCountMiddleware`，于 `after_model` 读取最后一条 `AIMessage.tool_calls`，把数量写入新增的可选状态字段 `last_model_tool_call_count`。这里统计的是**模型本次请求的数量**，不是工具成功数；被授权中间件拒绝或执行失败的调用也可能已计入。若希望统计实际执行数，应改用 `wrap_tool_call` 并另行定义并发与短路的计数规则。

局部实现可从下列骨架开始；异步 Gateway 路径也要实现对应的 `aafter_model`，状态字段应声明在中间件的 `state_schema` 中，并确认与 `ThreadState` 的组合可用：

```python
from typing import NotRequired

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage


class ToolCallCountState(AgentState):
    last_model_tool_call_count: NotRequired[int]


class ToolCallCountMiddleware(AgentMiddleware[ToolCallCountState]):
    state_schema = ToolCallCountState

    def after_model(self, state, runtime):
        last = state["messages"][-1]
        return {"last_model_tool_call_count": len(last.tool_calls) if isinstance(last, AIMessage) else 0}

    async def aafter_model(self, state, runtime):
        return self.after_model(state, runtime)
```

测试至少覆盖同一次图调用中“请求两个工具 → 下一次模型响应不请求工具”时字段由 `2` 变 `0`，以及异步钩子与同步钩子结果一致。再用假模型编译一张最小图，确认字段确实进入状态，而不是只证明直接调用方法会返回字典。`backend/CONTRIBUTING.md` 的自定义中间件示例和 `backend/tests/test_middleware_documentation.py` 可用于核对注册与当前钩子签名。若把它做成真实产品能力，还需决定统计值在 Run、Thread 或可观测事件中的归属；这个实验不要求改动生产配置或接入真实模型。

面试验收：能沿上面的请求路径说清四种生命周期时点和两种包装边界；能解释 `after_model` 为什么先于工具结果、状态更新为什么要经过 reducer；能用目录依赖或工具包装案例说明顺序改变会造成什么；能指出摘要、标题、记忆、循环检测与错误处理各处于哪个时点，以及当前课程没有证明的任务成功与长期记忆效果。下一课再深入工具本身的 Schema、参数验证和运行上下文。
