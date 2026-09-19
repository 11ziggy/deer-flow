你这个反馈是对的。上一版最大的问题是：我从 `tool_groups / skills / allowed_subagents` 这些“配置项”开始讲了，但没有先告诉你——**为什么代码走到这里突然要处理这些东西，它在整个组装流程里到底处于什么位置。**

这次我们只沿着一条主线走。

------

## 先把第二步放回整个流程里

第一步结束时，DeerFlow 已经知道了：

```
这次跑哪个 Agent
这次用哪个 Model
thinking / reasoning 等参数是什么
```

但是这时候的 Agent 其实还是个“空壳”。

因为一个 Agent 不只是：

```
LLM
```

它至少还需要知道：

```
这个 LLM 能调用什么工具？
能使用什么 Skill？
能不能委派给 Sub-agent？
```

所以第二步真正解决的问题是：

> **给已经确定好的 Agent，装上它这次 Run 可以使用的能力。**

这才是第二步的主线。文档把它叫做“划定可用能力，再组装工具与技能”。

你可以把整个过程理解成：

```
第一步：
Agent 是谁？模型是谁？
        ↓

第二步：
这个 Agent 能做什么？
        ↓

第三步：
它运行时怎么被 Middleware / Prompt / State 驱动？
        ↓

create_agent()
```

------

# 划定能用什么

 DeerFlow 现在已经准备“装能力”了。

但有一个问题：

> 系统里可能有很多能力，不应该每个 Agent 全部拥有。

例如整个 DeerFlow 系统可能有：

```
搜索工具
文件工具
MCP 工具
记忆工具

research Skill
coding Skill
finance Skill

researcher Sub-agent
coder Sub-agent
```

但你创建了一个 Custom Agent：

```
finance-agent
```

你可能不希望它：

```
什么 Tool 都能用
什么 Skill 都能学
什么 Sub-agent 都能委派
```

所以在真正拿工具之前，DeerFlow 必须先问：

> **这个 Agent 的能力边界是什么？**

于是 `AgentConfig` 里才会出现：

```
tool_groups
skills
allowed_subagents
```

它们不是三个突然冒出来的配置。

它们是在回答同一个问题：

> **这个 Custom Agent 允许拥有哪些能力？**

------

# 第一小步：先确定“能力边界”

所以第二步并不是一上来就在造 Tool。

它先从第一步加载好的：

```
AgentConfig
```

里面读取能力限制。

例如：

```
research-agent

tool_groups = ["search", "browser"]
skills = ["research"]
allowed_subagents = ["researcher"]
```

这句话实际上是在声明：

```
这个 Agent 的能力边界是：

工具方面：
主要允许 search / browser 这类配置工具

Skill 方面：
允许 research Skill

Sub-agent 方面：
只能委派 researcher
```

所以你可以把这一阶段叫：

> **能力策略解析。**

注意，它还没有得到最终 `final_tools`。

只是先回答：

```
“理论上这个 Agent 被允许拥有什么能力？”
```

------

# 第二小步：根据能力边界，真正去“拿工具”

现在 DeerFlow 知道：

```
这个 Agent 允许哪些能力
```

接下来才开始真正组装 Tool。

例如：

```
tool_groups
    ↓
get_available_tools(...)
```

这里的意思非常自然：

> 既然这个 Agent 只允许某些工具组，那我现在去整个系统的工具目录里，把符合条件的工具取出来。

所以你应该把：

```
get_available_tools(...)
```

理解成：

> **从系统的工具仓库中，为这次 Agent 挑工具。**

这已经是真正的“组装”动作了。

------

但为什么文档一直强调：

> `tool_groups` 不是最终 Tool 白名单？

因为 DeerFlow 的 Tool 不只来自 `config.yaml`。

除了这些配置工具之外，Agent 在运行时还可能需要系统自动补充一些基础能力，比如：

```
内置 Tool
MCP Tool
记忆 Tool
describe_skill
task continuity
update_agent
...
```

所以真实过程不是：

```
tool_groups
↓
final_tools
```

而是：

```
tool_groups
↓
从配置工具里筛一批

再加上
内置工具 / MCP / Memory / Skill discovery 等
↓

候选 Tool 集合
```

文档强调的就是这个边界。

------

# 第三小步：Skill 为什么没有直接变成 Tool？

这又是一个很容易失去主线的地方。

现在系统也已经读到了：

```
skills = ["research"]
```

于是 DeerFlow 要处理：

> 这个 Agent 能使用哪些 Skill？

它会从系统所有已启用 Skill 中筛选：

```
所有 enabled Skill
        ↓
AgentConfig.skills 限制
        ↓
这个 Agent 可用的 Skill
```

所以这里也是在做：

> **能力装配。**

但 Skill 跟 Tool 不一样。

Tool 是：

```
模型可以直接调用的函数能力
```

Skill 更像：

```
一套可按需加载的能力说明 / 方法 / 工具策略
```

所以 DeerFlow 不会在构图时：

```
把所有 SKILL.md 全文
↓
一次性塞进 Prompt
```

而是先把：

```
Skill 名称 / 索引 / 元信息
```

提供给 Agent。

等运行过程中真正需要某个 Skill 时，再由：

```
SkillActivationMiddleware
```

去激活它。

所以这里你要抓住一句：

> **第二步不是“执行 Skill”，而是确定这个 Agent 有资格发现和使用哪些 Skill。**

文档这里明确区分了：

```
可发现
已激活
可执行某个工具
```

这三件事情不是一回事。

------

# 第四小步：Sub-agent 其实也是一种“能力”

这时候 `allowed_subagents` 就很好理解了。

如果 Lead Agent 可以：

```
把任务委派给 researcher
```

那“委派”本身就是一种能力。

所以第二步也必须确定：

```
这个 Agent 能不能委派？
能委派给谁？
```

这里有两个配置一起控制：

```
subagent_enabled
```

回答：

> 这次 Run 要不要开启委派能力？

而：

```
allowed_subagents
```

回答：

> 如果允许委派，那能委派给哪些类型？

所以：

```
subagent_enabled = 开关

allowed_subagents = 能力边界
```

例如：

```
subagent_enabled = true

allowed_subagents = ["researcher"]
```

意思就是：

> 这次允许委派，但只能委派 researcher。

而：

```
allowed_subagents = []
```

则表示 Custom Agent 明确禁止委派。

所以即使 Run 请求：

```
subagent_enabled = true
```

也不能突破这个 Agent 自己的能力边界。

------

# 第五小步：为什么工具拿出来以后还不能直接用？

现在 DeerFlow 已经有一批：

```
候选 tools
```

但还差最后两层。

首先是：

```
apply_tool_authorization()
```

意思就是：

> Agent 配置说“可以用”，不代表当前用户一定有权限用。

例如系统里有某个敏感工具：

```
AgentConfig 允许
```

但当前用户角色：

```
没有权限
```

那还是得删掉。

所以：

```
能力配置
≠
权限
```

这是两个维度。

------

然后还有：

```
assemble_deferred_tools()
```

这解决的是另外一个工程问题：

> MCP 工具可能很多，没必要一开始把所有 Tool Schema 都暴露给模型。

于是部分工具先：

```
不直接暴露
```

等运行中通过工具搜索再发现。

这叫：

```
deferred tool discovery
```

所以最终流程是：

```
AgentConfig
↓
确定能力边界

↓
从系统能力池里取 Tool / Skill / Sub-agent 能力

↓
补充系统运行所需工具

↓
权限过滤

↓
延迟工具处理

↓
final_tools
```

这时候，才真正得到后面：

```
create_agent(
    tools=final_tools
)
```

要使用的工具集合。文档也是在这里才把它称为 `final_tools`。

------

## 所以第二步到底在做什么？

现在你重新看标题：

> **划定可用能力，再组装工具与技能**

它其实非常准确。

“划定能力”：

```
AgentConfig

tool_groups
skills
allowed_subagents
```

回答：

> 这个 Agent 被允许拥有什么能力？

然后“组装能力”：

```
加载 Tool
加载 Skill 索引
配置 Sub-agent 范围
补系统工具
权限过滤
Deferred Tool 处理
```

最终回答：

> **这一次真正交给 Agent 的能力是什么？**

------

你可以把第一步和第二步连起来：

```
第一步：确定“谁来干”
--------------------------------
哪个 Agent？
哪个 Model？
模型怎么运行？

↓

第二步：确定“它能干什么”
--------------------------------
允许哪些 Tool？
允许哪些 Skill？
允许哪些 Sub-agent？

然后真正把这些能力装出来
↓
final_tools + available skills + delegation boundary
```

这才是这两步之间真正的逻辑关系。

而且有一个特别重要的理解：

> **DeerFlow 不是先造一个什么都能干的 Agent，再运行时随便限制；而是在构图阶段就根据 Custom Agent 配置和 Run 配置，把这次 Agent 的能力范围确定下来，再生成真正要交给 `create_agent()` 的工具集合。**

这就是第二步的主干。