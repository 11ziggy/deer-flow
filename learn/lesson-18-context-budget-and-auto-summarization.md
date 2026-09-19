# 第 18 课：上下文预算与自动摘要

第 16 课讨论委派，第 17 课讨论规划、持续目标与人工参与。本课只回答一个问题：**对话越来越长时，DeerFlow 怎样缩短下一次模型请求，又尽量不丢掉继续完成任务所需的信息？** 它位于“Message / ThreadState 更新并写入 Checkpoint → 下一次模型调用”之间。核心阅读约 30～40 分钟；跳过末尾实验和代码深挖，也应能解释压缩链路与代价。

本课需要第 8 课的状态与消息合并规则、第 10 课的 `before_model` 时点，以及对 Skill 和 Sub-agent 的基本认识。必须掌握触发条件、近期消息与摘要的分工、手动压缩的边界、摘要误差如何延续。长期记忆的抽取与召回留给第 19 课，检索和引用留给第 20 课；Token 超限后的完整故障处理留给第 25 课。这里的“上下文”专指**后续模型请求可见的内容**，不等于界面上可翻看的聊天记录。

## 一个长对话为什么不能只保留原文

假设用户先要求分析一个项目，后来纠正“预算从 800 改为 1200”，Agent 又读取了 Skill、委派子任务并得到工具结果。每轮新的用户消息、助手回复和工具结果都会进入会话状态。若每次把全部原文送给模型，请求会越来越大，成本和延迟上升，也可能越过模型的输入窗口。只截掉最旧消息则可能丢掉“预算已更正”这样的关键约束。

DeerFlow 用**近期原文 + 旧对话摘要**继续执行。两者承担不同职责：近期消息保留精确措辞及工具调用关系；摘要用较短文本承接旧轮次的重要事实。它是有损压缩，不能保证每个旧细节都可恢复。下图是教学示意，不是运行日志：

```text
压缩前的 ThreadState.messages
  旧请求、旧回复、工具结果 ... | 最近消息、当前请求
                 ↓ 生成摘要并替换状态
压缩后的 ThreadState
  summary_text = “已确认预算为 1200；……”
  messages     = [最近消息、当前请求]
                 ↓ 下一次模型请求
  系统提示 + 隐藏的持久上下文数据 + 保留的消息
```

这里有两个不能混用的 Token 数字。**累计消耗**是已经执行的模型调用所花的 Token，压缩不会把它减回去；**下一次输入量**是当前将送给模型的内容，压缩主要改善这一项。`backend/app/gateway/context_usage.py::build_context_usage` 给界面提供的 `context_usage.token_count` 仅近似统计最新 Checkpoint 中的 `messages`，没有计入 `summary_text`、系统提示、工具定义等，因此它可以观察消息列表缩短，不能当作下一次请求的完整 Token 数，也不能当作质量指标。

## 自动压缩在模型调用前做了什么

当前 `config.example.yaml` 的示例配置启用摘要，以 **32,000 tokens** 为触发阈值，保留最近 **10 条消息**，并把送入摘要模型的原始片段预算设为 **15,564 tokens**。这是仓库示例值；`SummarizationConfig` 的类默认值 `enabled=False`、`keep=20 条消息`。实际运行应以自己的 `config.yaml` 为准，不能把示例值说成所有部署的固定行为。

`backend/packages/harness/deerflow/agents/lead_agent/agent.py::build_middlewares` 先放入 `DurableContextMiddleware`，再放入 `DeerFlowSummarizationMiddleware`。前者先把可保留的委派与 Skill 引用记入状态；后者的 `before_model` / `abefore_model` 在每次模型调用前检查历史。核心判断可缩成以下**源码摘录**（`summarization_middleware.py::_prepare_compaction`）：

```python
trigger_messages = self._messages_for_trigger_count(messages, previous_summary)
total_tokens = self.token_counter(trigger_messages)
if not force and not self._should_summarize(trigger_messages, total_tokens):
    return None
cutoff_index = self._determine_cutoff_index(messages)
if cutoff_index <= 0:
    return None
```

触发器可按 `tokens`、`messages` 或模型窗口的 `fraction` 配置，多条条件任一满足即可；已有 `summary_text` 也会作为一条计数用的临时消息参与触发判断。这个计数仍不等于完整模型请求大小。`fraction` 依赖摘要模型可用的窗口信息；没有可用窗口时，代码会丢弃该比例条件，保留绝对 Token／消息条件；若比例条件全被丢弃，自动压缩不会触发，但手动压缩仍可用。单独配置摘要模型时，比例阈值依据它的窗口，可能比实际执行模型的窗口更大；因此配置时要看执行模型的可承受输入量。

达到阈值也不表示一定压缩。`keep` 决定保留的近期尾部，切分点还要照顾助手的工具调用与对应工具结果，避免留下无法配对的半段；DeerFlow 额外保留**最新的真实用户请求**和带标记的动态上下文提醒。切分后若没有可删除的旧消息，就返回 `None`。所以 `keep: 10` 表示保留目标，并非“压缩后恰好只有 10 条消息”的硬上限。`backend/tests/test_summarization_summary_text.py::test_rescued_user_does_not_drop_earlier_tool_exchanges` 验证了当前请求仍在保留区，较早的工具交换进入摘要区。

摘要模型接收“待摘要旧消息”，若已有 `summary_text`，还会把旧摘要送入新的摘要提示。这使多次压缩能延续历史，也意味着上一次遗漏或写错的事实可能进入下一版。`trim_tokens_to_summarize` 限制的是生成摘要时送入提示的**原始文本片段**，外层提示和转义还会增加用量；它不是下一次主模型请求的硬上限。摘要生成失败或只返回空白文本时，自动路径不提交替换，旧状态保留，稍后仍可再次尝试。`backend/tests/test_summarization_summary_text.py::test_summary_model_failure_does_not_destroy_history` 和 `::test_existing_summary_is_included_when_creating_next_summary` 分别固定了这两个行为。

生成有效摘要后，`_maybe_summarize` 返回的关键状态更新是：

```python
{
    "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *result.preserved_messages],
    "summary_text": result.summary_text,
}
```

这是从 `summarization_middleware.py::_maybe_summarize` 摘出的两个关键字段；源码还可能附带启用任务连续性时的 `task_history`。消息通道删除旧原文并放回保留区，摘要写入 `ThreadState.summary_text`，由 Checkpoint 持久化。摘要**不是**追加在 `messages` 里的普通聊天消息。下一次模型请求由 `DurableContextMiddleware._inject` 将摘要装进隐藏的“持久上下文数据”用户消息；投影时摘要文本还受 6,000 字符的渲染上限约束，所以状态里保存的内容也不一定会原样全部送达模型。静态信任规则放在单独的系统消息，摘要等用户、模型或工具来源的内容仍作为数据处理。`backend/tests/test_durable_context_middleware.py::test_summary_in_channel_not_messages_then_injected` 用真实 Agent 图验证了“状态字段 → 模型请求”的衔接。

LangChain 提供摘要中间件的触发、切分基础；DeerFlow 增加了当前请求保护、状态字段、持久上下文投影、模型失败回退和手动 Checkpoint 写入等接线。真正生成摘要文本的是配置的模型；这段文本的正确性没有由框架自动保证。

## Skill 和 Sub-agent 结果怎样越过压缩边界

只保存对话摘要不足以表达所有后续动作。`DurableContextMiddleware` 在压缩前从结构化工具结果中提取两类**有界引用**，分别写入 `ThreadState.skill_context` 和 `ThreadState.delegations`，后续模型调用再把它们作为隐藏数据投影进去：

| 状态字段 | 保留的内容 | 明确的边界 |
| --- | --- | --- |
| `skill_context` | 已读取 Skill 的名称、`SKILL.md` 路径和简短描述 | 不保存 Skill 正文；应用指令前应按路径重读。只识别配置允许、路径有效且带结构化元数据的 Skill 文件读取。 |
| `delegations` | `task` 委派的描述、状态及有界结果摘要等 | 不是子 Agent 的完整对话或完整产出；简述和结论仍需核验。 |
| `summary_text` | 旧对话的生成式摘要 | 不保证每个事实、数字和来源都被保留。 |

`backend/packages/harness/deerflow/agents/middlewares/durable_context_middleware.py::_capture` / `_inject` 是写入与投影的配对入口。`backend/tests/test_durable_context_middleware.py::test_delegations_survives_summarization_and_stays_injected` 与 `::test_skill_reference_survives_summarization_and_stays_injected` 验证旧工具消息被压缩后，简要委派记录及 Skill 引用仍可进入模型请求。保留“已经做过什么”和“曾读过哪个 Skill”有助于继续工作，但它们都不能替代原始证据。摘要若把“预算已改为 1200”误写成 800，下一轮继续摘要可能强化这个错误；界面可见的旧聊天也不会自动重新进入模型上下文。需要关键数字、引用或完整 Skill 指令时，应回到可核验来源，而非把摘要当作权威事实。

## 手动 `/compact` 与自动路径的关系

Web UI 的输入框将 `/compact` 识别为内置命令，调用 `POST /api/threads/{thread_id}/compact`，并非把这段文字发给 Agent。`backend/packages/harness/deerflow/runtime/context_compaction.py::compact_thread_context` 从最新 Checkpoint 读取状态，复用同一个摘要中间件，默认 `force=True` 以跳过**自动触发阈值**；成功后用 `Overwrite` 替换 `messages`，并单独写入新的 `summary_text`。它仍需有可压缩的旧消息；没有可删内容会返回 `compacted=false`，不会为“强制”而丢掉当前请求。

手动命令不能与该 Thread 正在执行的 Run 并发写状态，路由会返回 409；摘要功能关闭时也会返回 409。可压缩但摘要生成失败则是错误，不会伪装成“无需压缩”。`backend/tests/test_context_compaction.py::test_compact_thread_context_reads_materialized_state_and_overwrites_messages` 与 `::test_compact_thread_context_raises_on_summary_generation_failure` 验证写入和失败边界。压缩改变后续模型使用的 Checkpoint 状态，**不会抹掉 Web UI 的完整可见聊天历史**；因此观察是否成功，应看 `GET /api/threads/{id}/state` 中的 `messages`、`summary_text` 和返回的 `checkpoint_id`，不能只看聊天窗口是否还显示旧消息。

## 可选实践：上下文压缩实验

这项实验用于验证理解，不要求开发新功能。使用测试环境和不含密钥、个人信息的固定长对话；记录同一套事实与更正，例如先写“预算 800”，后来明确更正为“预算 1200”，再给一个近期限制条件。准备两个独立 Thread，使用相同模型与相同输入顺序：

1. **自动组**：临时在本地 `config.yaml` 设置较低的绝对消息阈值（例如 `trigger: {type: messages, value: 8}`）和较小的 `keep`，运行足够多轮，使下一次模型调用前触发摘要。不要把实验配置或密钥提交到 Git。
2. **手动组**：将自动阈值设得高于这段历史，但保持 `summarization.enabled: true`；输入同样的对话，等待 Run 结束后执行 `/compact`。先记录压缩前状态，再记录命令响应与压缩后状态。不要在运行中调用，预期会被拒绝。
3. 两组均检查 `GET /api/threads/{id}/state`：旧消息数是否下降、`summary_text` 是否出现、最新用户请求是否仍在 `messages`、Skill 引用或委派简述（若本实验产生了它们）是否还在各自字段。再发送相同的验证问题：“最终预算是多少？最新限制是什么？依据是什么？”用原始对话作为答案基准，逐项记录正确、遗漏或编造。
4. 分开记录**下一次普通模型调用的输入 Token**与**摘要模型调用的 Token／延迟**，再记录累计 Token。可以用运行追踪或模型调用记录取得调用级数据；`/token-usage` 的累计值与近似 `context_usage` 只作辅助，不用它们代替完整输入量。继续增加几轮并再次压缩，检查更正事实是否在第二次摘要后失真。

建议产出一张表：`组别｜触发方式｜压缩前后 messages 数｜摘要是否保留更正｜下一次普通模型输入 Token｜摘要开销｜验证问题结果`。若两组答案质量不同，先比较摘要内容、保留尾部和模型实际输入，再解释原因；不能只凭“Token 降了”判定压缩有效。实验结论只适用于所用模型、配置和对话样本。

## 面试验收与代码深挖

能说明：自动摘要在模型调用前判断，触发后把旧消息压成 `summary_text` 并保留近期消息；`/compact` 跳过阈值但仍要求有旧消息可删；Skill 和委派只保留有界引用或简述；多次摘要可能传播遗漏和错误。若追问“为什么 UI 还能看到旧消息而模型不一定看到”，应答出可见历史与当前 Checkpoint 上下文分开。若追问“如何证明更省 Token 且不损质量”，应同时提供下一次普通模型输入量、摘要额外开销和固定问题的答案对照。

**可选代码深挖**：阅读 `summarization_middleware.py::_prepare_compaction`、`_build_summary_prompt`、`_amaybe_summarize`，逐条标注触发、分区、旧摘要输入和状态更新。正文给出了正常链路；打开这些函数的额外收益是检查“当前请求超出 `keep` 时如何救回”“摘要输入被截断后怎样选择尾部”等边界。再用 `backend/tests/test_summarization_summary_text.py` 中相应测试验证推断。掌握本课不以打开这些文件或运行实验为前提。
