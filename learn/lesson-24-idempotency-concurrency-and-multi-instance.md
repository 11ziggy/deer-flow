# 第 24 课：幂等、并发与多实例

第 23 课说明了一个 Run 怎样被取消、回滚或重新生成。本课把视角移到“多个请求几乎同时到达”这一刻，回答运行时可靠性的另一个核心问题：**客户端重试、同一 Thread 的并发提交以及多个 Gateway worker 竞争时，DeerFlow 怎样只接纳应当存在的 Run，并阻止失去所有权的 worker 继续写结果？** 它位于请求主线的“Gateway 接纳 Run → RunStore 原子裁决 → owner worker 执行 → lease 心跳与终结”一段。核心阅读约 40～50 分钟。

前置知识是第 21 课的 Run 状态机，以及第 23 课对 `interrupt`、`rollback` 和 Finalization 的区分。本课必须掌握三种不同的保护：幂等键识别“同一个创建意图”，Thread 活跃唯一约束裁决“不同创建意图能否并发”，owner/lease（所有者/租约）决定“哪个 worker 仍有权执行和终结”。还要能解释 `reject`、`interrupt`、`rollback` 的准确边界，以及为什么进程锁不能成为多实例正确性的依据。

本课暂时不展开模型、工具异常、Token Budget、Loop Detection、Tracing 和完整审计，它们属于第 25 课；也不把定时任务自己的队列租约或 MCP 长任务租约混进来。这里的 lease 专指 `runs` 表上的 Run ownership。外部系统写入的严格恰好一次语义也不由本课机制自动提供，后文会说明这个边界。

## 先分开三种“重复”

“不要重复执行”不是一个单独问题。下面三种场景看起来都可能出现两次请求，但系统需要的答案不同。

| 场景 | 系统应当怎样回答 | 当前主要机制 |
| --- | --- | --- |
| 同一创建请求因超时被客户端重试 | 返回第一次已经接纳的同一个 Run，不再附着第二个 worker | `Idempotency-Key`、`uq_runs_idempotency_key` |
| 两个不同请求同时写同一 Thread | 按冲突策略拒绝或替换；不能让两个活跃 Run 同时推进 ThreadState | `multitask_strategy`、`uq_runs_thread_active`、原子事务 |
| 一个 worker 失联，另一个 worker 想恢复；旧 worker 又迟到 | 只能由租约仍有效的 owner 写入；过期后由一个 peer 原子接管，旧 owner 被隔离（fence） | `owner_worker_id`、`lease_expires_at`、heartbeat、条件更新 |

这三层不能互相替代。幂等键只回答“是不是同一个创建意图”，并不阻止使用两个不同 key 的请求竞争同一 Thread；Thread 唯一约束只限制活跃操作，并不会让一次已经完成的 keyed retry 找回旧 Run；lease 证明当前执行权，却不能判断两个 HTTP 请求的业务意图是否相同。

可以先记住一条主链：

```text
POST 同一 Thread 的 Run
  → 可选 Idempotency-Key 被按 owner + thread 做作用域隔离
  → RunManager 做本 worker 的快速检查
  → 共享 RunStore 原子裁决幂等键与 Thread 冲突
  → 唯一获准的 Run 记录 owner_worker_id 与 lease_expires_at
  → owner 周期续租并执行
  → 续租失败到达确认期限：旧 owner 停止并禁止继续持久化
  → peer 只在 lease + grace 已过期后原子收口孤儿 Run
```

## `Idempotency-Key` 复用的是 Run，不是答案文本

幂等（idempotency）在这里指：对同一个创建意图重复调用，接纳结果与调用一次相同。Thread 级三个创建入口都接受可选请求头：

```text
POST /api/threads/{thread_id}/runs
POST /api/threads/{thread_id}/runs/stream
POST /api/threads/{thread_id}/runs/wait
Idempotency-Key: send-message-42
```

`backend/app/gateway/routers/thread_runs.py::_scope_http_run_idempotency_key` 不会把调用者提供的原文直接放进全局唯一索引。它去掉首尾空白，拒绝空值和超过 255 字符的值，再计算：

```python
digest = hashlib.sha256(
    f"{owner_id}\0{thread_id}\0{key}".encode()
).hexdigest()
return f"http-run:{digest}"
```

因此，同一认证用户、同一 Thread、同一原始 key 才映射到同一个 HTTP Run key；不同用户或不同 Thread 可以安全复用相同的客户端字符串。三个 Thread 级创建入口共享这个作用域，所以第一次调用 `/runs/stream`、重试时改用 `/runs`，仍应找回同一个 Run。没有显式 Thread、会临时创建 Thread 的无状态 `/api/runs/*` 不属于这份 HTTP 幂等契约。

### 两个请求同时插入时，谁决定只有一行

同一 worker 内，`RunManager._admit_thread_operation()` 会先查找本地已有的相同 key；多 worker 或本地记录已经清理时，最终依据是 `runs` 表的唯一索引。`backend/packages/harness/deerflow/persistence/run/model.py::RunRow` 定义：

```python
Index("uq_runs_idempotency_key", "idempotency_key", unique=True)
```

两个 worker 即使同时生成了不同 `run_id`，也只有一个能提交相同 `idempotency_key`。失败的一方在 `RunRepository.create_thread_operation_atomic()` 中查询已存在的行并抛出 `RunIdempotencyConflict`；`RunManager` 再把那一行水合为 `store_only=True` 的复用句柄。复用方不会注册第二个本地活跃记录，也不会附着第二个 `asyncio.Task`。这条边界很重要：peer 能观察同一个 Run，不等于 peer 获得了它的执行权。

`backend/app/gateway/services.py::start_run` 还会检查复用行中的 `input`、`assistant_id` 和 `conversation_references`。这些字段与当前请求不同就返回 409：

```text
Idempotency-Key already used with a different request
```

不要把这扩大成“服务端比较了请求体的每一个字节”。当前代码核对的是上述字段，不是任意配置字段的完整规范化副本。客户端应在一次逻辑提交的重试周期内保持 key 和请求语义不变；新的业务意图使用新 key。

### 复用 Run 后，各入口返回什么

幂等键绑定的是 Run 身份，而不是“重新读取当前 Thread 的最新状态”。如果旧 Run 完成后，同一 Thread 又有了后续 Run，那么当前 head Checkpoint 可能已属于后者。因此：

| 重试入口 | 当前行为 | 原因 |
| --- | --- | --- |
| `/runs` | 返回相同 `run_id` 和该 Run 的持久状态 | 不创建第二个 worker |
| `/runs/stream` | 活跃且有跨进程 bridge 时观察原流；终态流已清理时发 `gap`，提示加载持久状态 | 不能把空流伪装成一次新的执行；进程内 bridge 也无法观察 peer 的活跃流 |
| `/runs/wait` | 复用时返回该 Run 的持久 `status`/`error`，而不是把 Thread 最新 Checkpoint 当作旧 Run 结果 | Thread head 可能已经被后续 Run 推进 |

幂等键也不是永久拒绝相同 Thread 的所有新工作。旧 keyed Run 到达终态后，不带该 key 或使用新 key 的后续 Run 可以正常接纳；再次使用旧 key 仍然返回旧 Run。`backend/tests/test_thread_run_idempotency.py` 与 `test_run_repository.py::test_run_admission_reuses_process_wide_idempotency_key` 固定了这些行为。

## 不同请求竞争同一 Thread：数据库约束才是最终裁判

同一 Thread 的两个不同 Run 如果同时读取相同 Checkpoint，再分别写消息、Goal、Artifact 或标题，最终状态可能丢更新、重复工具调用或形成不可解释的分支。`RunManager` 有进程内 `asyncio.Lock` 和 Thread 索引，可以快速拒绝同一 worker 的重入；但它们只保护一个 Python 进程：

```text
Gateway worker A                    Gateway worker B
RunManager._lock A                  RunManager._lock B
本地看见 Thread T 空闲              本地也看见 Thread T 空闲
          \                         /
           \---- 共享 runs 表 -----/
```

A 持有 `_lock A` 时，B 的 `_lock B` 毫不知情。即使给每个进程都加一把“全局”字典锁，也只是各自全局。多实例正确性必须落在所有竞争者都能原子访问的共享系统中。

当前 SQL 模型用一个部分唯一索引表达核心不变量：

```python
Index(
    "uq_runs_thread_active",
    "thread_id",
    unique=True,
    postgresql_where=text("status IN ('pending', 'running')"),
    sqlite_where=text("status IN ('pending', 'running')"),
)
```

它表示：**一个 Thread 最多有一行处于 `pending` 或 `running`。** 终态历史可以有很多行，因为它们不参与这个部分索引。当前约束实际还覆盖 `checkpoint_write`、`artifact_write`、`branch` 等以 Run 行表示的 Thread 操作；所以“Agent Run 不能重叠”只是更广义不变量的一部分——Agent 执行也不能越过正在修改同一 Thread 状态的内部操作。

`RunManager._admit_thread_operation()` 的顺序是：持有本地锁进行本地检查，调用 `RunStore.create_thread_operation_atomic()`，持久插入成功后才注册本地 `RunRecord`。若两个 worker 并发执行 `reject`，两边本地检查都可能通过，但共享数据库只允许一个 INSERT 提交；另一边把唯一冲突转换成 `ConflictError`，Gateway 返回 409，而不是泄漏为 500。

这也解释了为什么“先 SELECT 是否存在，再 INSERT”不够：两个事务都可能在对方 INSERT 前读到空集合。唯一约束是最后防线；需要替换旧 Run 时，还要把锁行、修改旧行和插入新行放在同一事务内。

## `reject`、`interrupt`、`rollback` 不是三个并发级别

`RunCreateRequest.multitask_strategy` 当前只支持这三个值。它们决定发现同 Thread 活跃操作时怎样接纳新 Run，不是数据库隔离级别。

| 策略 | 本 worker 有活跃 Run | 另一 worker 有有效 lease 的活跃 Run | 另一 worker 的 lease 已过期 | 旧 Run 的状态语义 |
| --- | --- | --- | --- | --- |
| `reject` | 新请求 409 | 新请求 409 | 在孤儿被收口前仍冲突 | 不改变旧 Run |
| `interrupt` | 接纳替代 Run，通知旧 task 停止；替代 Run 等待旧 Finalization | 409，不能从本请求直接杀死 peer task | 同一事务把旧持久行标为 `interrupted` 并接纳新 Run | 本地旧 Run 最终走普通中断并保留已提交状态；失联旧 Run 没有协程可继续收尾 |
| `rollback` | 接纳替代 Run，旧 task 停止并尝试恢复 pre-run ThreadState；替代 Run 等待收尾 | 409 | 同样把旧持久行标为 `interrupted` 并接纳新 Run；死 worker 无法回来执行 Checkpoint 回滚 | 本地旧 Run 按第 23 课的 rollback 路径收口；外部副作用仍不撤销 |

对 `interrupt`/`rollback`，`RunRepository.create_thread_operation_atomic()` 会先用 `SELECT ... FOR UPDATE` 锁定同 Thread 活跃行，再在一个事务中完成“检查 lease → 更新旧行 → INSERT 新行”。若任一候选是其他 worker 拥有且 lease 仍有效的 Run，整个事务失败，不能留下“一部分旧行已被中断，但新行没创建”的半完成状态。若并发事务仍在无旧行时一起竞争，`uq_runs_thread_active` 继续承担最后裁决。

这里有一个容易说错的边界：**`multitask_strategy="interrupt"` 不等于向任意远端 worker 发送分布式取消。** 创建请求无法直接取消 peer 进程里的 Python task，所以有效远端 lease 会使替代接纳返回 409。若用户确实要停止那个 Run，应调用第 23 课的 cancel API；非 owner worker 会把 `cancel_action` 持久化，owner 在续租时观察到请求并执行正常取消。等旧 Run 完成 Finalization 后，再提交新 Run。

同一 worker 的替代接纳可以直接设置旧 `RunRecord.abort_action`、触发 `abort_event` 并取消 task。新 Run 虽已得到 `pending` 行，worker 启动仍会等待旧 Run 的 `finalizing` 屏障，避免 rollback 或标题/Checkpoint 尾部写入覆盖新 Run。这是第 23 课“取消后仍需 Finalization”在并发接纳端的落点。

## owner 与 lease：数据库行存在，不代表 worker 仍活着

唯一约束能防止同时接纳两个活跃行，却不能判断持有那一行的进程是否已崩溃。如果一个 worker 在 `running` 时宕机，永远保留活跃行会让 Thread 永久 409；立即由其他 worker 接管，又可能在原 worker 只是短暂卡顿时形成双执行。

DeerFlow 为每个 Run 记录：

- `owner_worker_id`：当前执行所有者；
- `lease_expires_at`：该所有权最近一次被数据库确认的有效期限；
- `cancel_action` / `cancel_requested_at`：非 owner 请求 owner 停止时的持久信号。

启用 `run_ownership.heartbeat_enabled` 后，新 Run 的 lease 默认持续 `lease_seconds=30` 秒。`RunManager._heartbeat_loop()` 约每 `lease_seconds / 3` 续租一次。SQL 的 `RunRepository.renew_lease()` 不是无条件更新时间，而是比较并交换（compare-and-set，CAS）式条件更新：

```python
update(RunRow).where(
    RunRow.run_id == run_id,
    RunRow.owner_worker_id == owner_worker_id,
    RunRow.status.in_(("pending", "running")),
).values(lease_expires_at=lease_dt)
```

只有 owner 未变化且 Run 仍活跃，续租才成功；同一个更新还返回持久 `cancel_action`。因此，旧 worker 不能在行已被终结或所有权条件不再匹配后继续把 lease 刷新回来。

### owner 必须在自己的确认期限到达时停止

本地 `RunRecord.lease_expires_at` 只在数据库成功确认续租后前移，它代表这个 worker **最后能够证明** 自己拥有 Run 的期限。`RunManager._renew_leases()` 对数据库调用设置不超过该期限的等待时间：

```text
续租暂时失败，但最后确认期限尚未到
  → 保持执行，下一轮重试

期限到达前仍无法确认，或数据库拒绝 owner/status 条件
  → ownership_lost = True
  → 设置 abort_event 并取消本地 task
  → 禁止后续 RunJournal、Checkpoint、Thread metadata、完成状态等持久写入
```

这叫隔离（fencing）：不是只把日志写成“租约丢失”，而是让失去资格的执行者停止产生权威结果。即使一次续租调用在超时取消后才迟到并提交成功，本地 worker 也不会把已经越过的旧确认期限重新解释成连续所有权；`test_late_successful_renewal_still_fences_local_run` 固定了这条失败关闭原则。

### peer 为什么还要等待 `grace_seconds`

peer 的接管条件比 owner 的自停条件更保守。`RunRepository.claim_for_takeover()` 只在状态仍为 `pending/running` 且 `lease_expires_at < now - grace_seconds`（或旧行没有 lease）时，用一条条件 UPDATE 把孤儿 Run 收口为 `error`。默认 `grace_seconds=10` 是跨 worker 时钟偏差预算，也是额外恢复延迟；它不是 owner 可以继续执行的宽限期。

```text
owner 的最后确认期限                 peer 可接管时间
         |---------- grace ---------->|
owner 到此必须停止                    一个 peer 的 CAS 能成功
                                      其他 peer 更新 0 行，不能重复接管
```

多个 peer 同时扫描到同一过期 Run 并不危险，关键不在扫描结果，而在最后的条件 UPDATE。只有一个调用能把仍活跃的行改成 `error`；其他调用发现状态已经变化，返回未接管。接管也不会恢复旧 Python 协程，它只以 `stop_reason=orphan_recovered` 等证据终结旧 Run，并由 Gateway 发布终止流、安排清理。Checkpoint 可供之后创建新 Run 使用，但不是协程迁移。

租约方案依赖各节点时钟大致同步。时钟偏差超过 `grace_seconds` 时，peer 可能过早判断租约过期；提高 grace 会降低误接管风险，但会延长真实崩溃后的恢复时间。这是 lease 的明确代价，不是实现细节。

## 多实例安全不是只打开 heartbeat

当前生产默认仍是 `GATEWAY_WORKERS=1`。多 worker 请求会被负载均衡到不同进程，完整安全组合至少需要共享持久层与跨进程观察通道各司其职：

| 能力 | 多 worker 需要什么 | 缺失时的问题 |
| --- | --- | --- |
| Run 接纳、幂等、活跃唯一约束和 lease CAS | 共享 PostgreSQL `RunRepository` | 每个进程的 `MemoryRunStore` 看不到对方；SQLite 不支持这里的并发多进程写模型 |
| owner 心跳与孤儿恢复 | `run_ownership.heartbeat_enabled: true` | NULL lease 会让启动/扩容时的 peer 把仍活跃 Run 当孤儿 |
| Run 事件与单例交付收据 | `run_events.backend: db` | memory/JSONL 事件存储无法跨 worker 保证单例写入 |
| SSE 加入、`/wait` 与有限重放 | Redis StreamBridge | 请求落到非 owner 时无法观察 owner 的进程内流 |

`backend/app/gateway/deps.py::_enforce_postgres_for_multi_worker` 在 `GATEWAY_WORKERS > 1` 时会拒绝非 Postgres、非 DB RunEventStore 或未开启 heartbeat 的配置，并对 scheduler、进程内浏览器会话等额外限制失败关闭。项目 README 进一步把 Redis StreamBridge 列为生产多 worker 组合的一部分。`run_ownership` 是启动期配置；修改后需要重启 Gateway，不能假设热加载会重建心跳任务。

共享数据库解决的是裁决与事实；Redis Bridge 解决的是跨进程实时观察。把 Bridge 换成 Redis 并不会自动产生数据库唯一约束，把数据库换成 Postgres 也不会让进程内 SSE 缓冲被另一 worker 看见。面试时应按职责说明，而不是笼统说“上了 Redis/Postgres 就支持分布式”。

## 当前保证到哪里：不是任意副作用的“恰好一次”

完成本课后可以准确说：

- 同一个 HTTP 幂等 key 在其 owner + Thread 作用域内只接纳一个 Run 行，重试不附着第二个 worker；
- 同一 Thread 最多存在一个 `pending/running` 操作，竞争者由数据库约束与事务裁决；
- 一个 Run 在任一时刻只有持有有效 lease 的 owner 有资格继续执行和持久终结，失去确认的旧 owner 会被 fence；
- 多个 peer 对孤儿 Run 的终结由条件更新保证只成功一次。

但不能据此声称“整个 Agent 的每个动作严格恰好执行一次”。设想 owner 调用外部支付、邮件或数据库 API：远端已经提交副作用，DeerFlow 还没写回 ToolMessage 时进程崩溃。新 Run 无法仅从本地 Checkpoint 判断远端是否成功；重试工具可能再次写入，rollback 也不会撤销远端结果。

外部写工具仍需要自己的业务幂等键、远端唯一约束、查询后确认、outbox/收据或可补偿设计。本课机制提供的是 **Run admission 与 owner finalization 的防重复边界**，不是跨 DeerFlow、模型提供商和任意第三方系统的分布式事务。

## 课程产出：并发与幂等测试

产出建议保存为 `learn/outputs/lesson-24-concurrency-idempotency.md`，并提交一份局部测试改动或独立实验代码。目标不是从零重写 RunManager，而是用可重复证据区分“同 key 复用”“不同 key 冲突”和“lease 失效后的 fencing”。

先在 `backend/tests/test_run_repository.py` 的现有 repository fixture 上补充或临时编写一个并发测试。使用两个 `RunManager` 模拟两个 worker，共享同一个 `RunRepository`，用 `asyncio.Event` 或数据库屏障尽量让提交同时进入，不要靠长 `sleep` 猜测时序。至少验证下面三组：

| 组别 | 并发输入 | 预期结果 | 必须记录的证据 |
| --- | --- | --- | --- |
| A：同 key 重试 | 同 owner、同 Thread、同 input、同 idempotency key | 所有成功结果的 `run_id` 相同；`runs` 表只有一行；只有首次接纳者可附着 worker | 返回 Run ID、行数、`idempotency_reused/store_only` |
| B：不同 key 竞争 | 同 Thread、不同 key，均为 `reject` | 只有一个 `pending/running`；其余为 `ConflictError`/HTTP 409 | 成功数、冲突数、`uq_runs_thread_active` 后的活跃行数 |
| C：不同 Thread | 不同 Thread 并发提交 | 都可接纳，证明锁定范围没有退化成全局串行拒绝 | 每个 Thread 的 Run ID 与状态 |

再用现有测试验证 lease 与替换边界：

```powershell
cd backend
uv run pytest tests/test_thread_run_idempotency.py -k "same_idempotency_key or scoped_to_thread or scoped_to_authenticated_user or different_input" -q
uv run pytest tests/test_run_repository.py -k "process_wide_idempotency_key or atomic_rejects_unique_violation" -q
uv run pytest tests/test_multi_worker_run_ownership.py -k "atomic_reject_prevents_duplicate or interrupt_claims_and_creates or interrupt_rejects_other_worker_valid_lease or concurrent_reconcilers_report_one_successful_claim or heartbeat_renews_active_run_leases or renewal_exception_through_confirmed_expiry_fail_stops_run" -q
```

这些测试分别证明 HTTP key 的作用域与载荷冲突、SQL 唯一冲突被转换为业务冲突、`interrupt` 对过期/有效 peer lease 的不同处理、多个恢复者只有一个成功，以及 owner 续租或丢失所有权后的行为。Memory store 测试用于快速固定语义，不等于真实多进程证明；SQL repository 测试固定数据库边界。若有可丢弃的 Postgres 环境，可以把同一并发矩阵作为可选集成实验，但不要在共享生产 Thread 上制造 lease 过期或强杀 worker。

产出最后附一张表，逐项写出你观察到的 Run ID、HTTP/异常结果、活跃行数、owner、lease 与最终状态，并用不超过 300 字回答：“为什么 `_lock` 能防本进程重入，却不能代替 `uq_runs_thread_active`？”以及“为什么 owner 到期自停与 peer 在 grace 后接管必须是两个时间点？”

## 可选代码深挖与面试验收

代码深挖按收益排序：先读 `app/gateway/routers/thread_runs.py::_scope_http_run_idempotency_key` 与 `services.py::start_run` 的 reuse 分支，确认 key 如何作用域化、哪些请求字段会核对、为什么不附着第二个 worker；再读 `runtime/runs/manager.py::_admit_thread_operation` 与 `persistence/run/model.py::RunRow`，把本地检查、数据库唯一索引和本地注册顺序连起来；然后只读 `persistence/run/sql.py::create_thread_operation_atomic/renew_lease/claim_for_takeover`，确认替换事务和三个条件更新；最后读 `RunManager._renew_leases/_mark_ownership_lost/reconcile_orphaned_inflight_runs`，核对 owner 自停、peer grace 与 fencing。无需在本课展开完整 worker Finalization 或第 25 课的事件审计。

面试验收：能在 3 分钟内从客户端超时重试讲到幂等 key 复用同一 Run，并说明 raw key 为什么要按 owner + Thread 做作用域隔离；能画出两个 worker 都通过本地检查、最终由共享部分唯一索引裁决的竞态；能准确比较 `reject`、`interrupt`、`rollback`，尤其指出有效远端 lease 会使替代创建冲突；能沿“owner 心跳续租 → 最后确认期限到达后 fence → peer 等待 grace → CAS 接管”解释多实例恢复；能明确区分 Run 防重复保证与外部工具副作用的恰好一次问题。
