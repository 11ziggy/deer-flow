# 第 12 课：沙箱（Sandbox）、文件与交付物（Artifact）

智能体（Agent）会调用工具，但工具要处理用户上传的文件时，还需要回答一个工程问题：**同一条会话（Thread）的文件在哪里、工具怎样找到它、处理结果怎样真正交给用户？** 本课位于“模型产生工具调用（Tool Call）→ 运行时（Runtime）执行文件工具 → 会话状态（ThreadState）更新 → 用户读取结果”这一段请求主线。核心阅读约 35～45 分钟。

前置知识是第 5 课的智能体循环（Agent Loop）、第 8 课的状态字段，以及第 11 课的工具参数和运行上下文。本课聚焦文件流转，必须掌握沙箱提供者（Sandbox Provider）与沙箱实例、虚拟路径、获取与释放的生命周期，以及 `uploads`、`workspace`、`outputs`、Artifact 的区别。容器逃逸、跨实例回收和完整权限模型分别属于后续可靠性、安全性专题，这里只讲本链路需要的边界。

## 沿一个文件任务走完主线

假设用户上传 `notes.txt`，要求“整理成一份摘要文件”。以下是**流程示意，不是一次真实运行日志**：

```text
POST /api/threads/{thread_id}/uploads
  → 当前用户、Thread 的 uploads/notes.txt
  → 前端提交本轮用户消息 HumanMessage.additional_kwargs.files 文件元数据
  → UploadsMiddleware 给模型标出 /mnt/user-data/uploads/notes.txt
  → read_file 通过 Sandbox 读取内容
  → 工具在 workspace 处理，结果写到 outputs/summary.md
  → present_files 登记输出路径到 ThreadState.artifacts
  → 客户端通过 GET /api/threads/{thread_id}/artifacts/... 读取文件
```

上传接口返回的文件信息包含 `virtual_path` 和 `artifact_url`；它先把文件保存到当前用户的 Thread 目录。`frontend/src/core/threads/hooks.ts` 随后把返回的文件信息放进所提交用户消息的 `additional_kwargs.files`。对于不共享 Thread 数据目录的 Sandbox Provider，网关（Gateway）还会把文件同步进 Sandbox。`backend/app/gateway/routers/uploads.py::upload_files` 的关键分支可摘为（中间省略保存文件的代码）：

```python
sync_to_sandbox = not _uses_thread_data_mounts(sandbox_provider)
...
if sync_to_sandbox and sandbox is not None:
    for file_path, virtual_path in sandbox_sync_targets:
        await run_file_io(_sync_upload_to_sandbox, sandbox, file_path, virtual_path)
```

这说明“文件已经上传”和“某种远端沙箱已经拿到文件”是两个步骤；共享挂载的实现不需要重复同步。本轮上传由 `backend/packages/harness/deerflow/agents/middlewares/uploads_middleware.py::UploadsMiddleware.before_agent` 从最近一条 `HumanMessage.additional_kwargs.files` 取出，核对文件存在后，给消息加入 `<current_uploads>` 路径、文件大小及有界提纲或预览，并更新 `uploaded_files`。**提示不等于把完整文件内容送进模型。** 读取正文仍需 `read_file` 等工具。较早轮次的上传不会自动列在本轮提示中，可用 `list_uploaded_files` 查找。

## 三个目录、两种路径、一个交付动作

文件工具对 Agent 展示的是**虚拟路径**，即不同 Sandbox 实现共同接受的路径约定；宿主机上的实际存储路径由当前用户和 Thread 决定。`backend/packages/harness/deerflow/config/paths.py::Paths.sandbox_work_dir`、`sandbox_uploads_dir`、`sandbox_outputs_dir` 分别计算三个目录：

| Agent 看到的路径 | 本课用途 | 是否直接成为交付物 |
| --- | --- | --- |
| `/mnt/user-data/uploads/` | 用户上传的输入 | 否；上传接口另有供访问原件的 `artifact_url`。 |
| `/mnt/user-data/workspace/` | 处理中的脚本、中间文件 | 否。 |
| `/mnt/user-data/outputs/` | 要交付的最终文件 | 还需调用 `present_files` 登记。 |

通常的宿主布局是 `{DEER_FLOW_HOME}/users/{user_id}/threads/{thread_id}/user-data/{uploads,workspace,outputs}/`；实际根目录可配置，不应让 Agent 把它写死。`LocalSandboxProvider._build_thread_path_mappings` 为每个用户和 Thread 建立 `PathMapping`，把上述虚拟目录映射到对应宿主目录。对于使用挂载的容器 Sandbox，虚拟路径在容器内对应挂载目录。`Paths.resolve_virtual_path` 也让 Gateway 按同一用户和 Thread 将虚拟路径解析回宿主文件，并拒绝越出 `user-data` 根目录的路径。

这是一条**名称与归属不变量**：同样的 `/mnt/user-data/workspace/report.md`，在不同 Thread 或不同用户下应指向不同文件。`backend/tests/test_local_sandbox_virtual_path_contract.py::test_per_thread_user_data_mapping_isolated` 和 `::test_same_thread_different_users_are_isolated` 分别验证了这两种分离。虚拟路径方便工具与 Provider 共用接口，但不能把“路径被映射”直接推断成“运行进程已被安全隔离”。

输出目录也不自动生成 Artifact。`backend/packages/harness/deerflow/tools/builtins/present_file_tool.py::present_file_tool` 的关键状态更新可以缩写为：

```python
normalized_paths = [_normalize_presented_filepath(runtime, filepath) for filepath in filepaths]
return Command(update={
    "artifacts": normalized_paths,
    "messages": [ToolMessage("Successfully presented files", tool_call_id=tool_call_id)],
})
```

`_normalize_presented_filepath` 将当前 Thread 的输出目录内路径规范为 `/mnt/user-data/outputs/...`；若给它 `workspace` 或别的 Thread 的路径，返回错误 `ToolMessage`，不会更新 `artifacts`。`backend/packages/harness/deerflow/agents/thread_state.py::merge_artifacts` 合并并去重这些路径。因此，**状态里的 Artifact 是已登记给客户端展示的输出路径，文件字节仍留在文件系统中**；它不是 `workspace` 中的每个文件，也不是一份文件内容被复制进 Checkpoint。还要注意：`present_files` 主要校验路径归属，**不负责确认文件必然存在**；若只登记了一个尚未写入的输出路径，后续读取仍会失败。

Gateway 的 `backend/app/gateway/routers/artifacts.py::get_artifact` 按当前用户和 Thread 解析路径，检查文件是否存在，再返回可浏览或下载的响应；不存在返回 404。这个 `GET` 路由也可读取 `uploads` 等合法 Thread 文件，所以“Artifact 路由可访问的文件”与“`present_files` 登记到 `ThreadState.artifacts` 的交付物”不是同一个集合。本课的完成标准是**文件已写入 `outputs`、路径已登记、读取路由确实能取回内容**。

## 谁获取 Sandbox，谁决定何时释放

沙箱提供者负责创建或复用执行环境；沙箱实例提供 `read_file`、`write_file`、`execute_command` 等操作。`backend/packages/harness/deerflow/sandbox/sandbox_provider.py::SandboxProvider` 的接口形状如下（省略方法体）：

```python
def acquire(self, thread_id: str | None = None, *, user_id: str | None = None) -> str: ...
def get(self, sandbox_id: str) -> Sandbox | None: ...
def release(self, sandbox_id: str) -> None: ...
```

普通共享视图运行默认懒获取：`SandboxMiddleware.before_agent` 不主动获取；第一次触碰沙箱的工具再由 `sandbox/tools.py::ensure_sandbox_initialized` 或异步对应函数获取或复用实例，并把 `sandbox_id` 留在运行状态中。若配置为非懒获取，或本轮需要提前准备明确限定的技能文件视图，则可在 `before_agent` 获取。这样只回答文本、没有使用沙箱工具的普通运行通常无需分配执行环境。复用已有 `sandbox_id` 时仍要检查当前调用是否有沙箱执行权限；不能把旧的状态当成永久授权。

同一实例可能同时被主 Agent、子任务或 Gateway 上传同步使用。**执行租约（lease）**是进程内“谁还在使用这个 Sandbox 客户端”的记录，不是文件锁，也不是 Checkpoint。`backend/packages/harness/deerflow/sandbox/lease.py::SandboxLeaseManager.release` 在某个使用者结束时先解除其绑定与命令作用域，只有最后一个使用者离开且存在正常释放请求时才调用 Provider 的 `release`。`backend/tests/test_sandbox_leases.py::test_last_execution_lease_is_the_only_provider_releaser` 固定了“主任务结束时子任务仍在使用，就不能提前释放”的行为。`SandboxMiddleware.after_agent` / `aafter_agent` 是正常结束时的释放点；外层执行清理还会兜住跳过正常退出钩子的情况。

**`release` 不一定是销毁。** `LocalSandboxProvider.release` 不删除缓存实例；一些远端 Provider 可把环境停放以待复用。这里的生命周期承诺是“本次使用结束，不能打断尚在使用它的其他调用”，而不是“本轮结束后文件和沙箱立即消失”。租约只协调当前进程内对客户端的使用；跨实例容器所有权还有独立机制，本课不展开。

## 本地沙箱的安全边界

本地沙箱（Local Sandbox）用路径映射让结构化文件工具接受 `/mnt/user-data/...`。`backend/packages/harness/deerflow/sandbox/local/local_sandbox.py::LocalSandbox._resolve_path_with_mapping` 会检查映射后路径是否仍位于对应目录内；这是必要的文件路径校验。但 `LocalSandbox.execute_command` 将命令交给**宿主机的 shell 子进程**执行，命令中的虚拟路径只做文本替换，子进程仍处于宿主机进程和文件系统环境。`LocalSandboxProvider.supports_agent_skill_isolation` 在允许宿主 `bash` 时也返回 `False`，因为 shell 可绕过结构化工具的路径映射。

所以 Local Sandbox 适合在可信开发环境验证文件流程，不能当作执行不可信代码的安全隔离边界。是否允许工具触碰沙箱，还要看运行时授权；路径规范化也不能替代这项授权。这里仅需能说清这两个不同层次的限制，完整威胁模型留到第 26 课。

## 可选验证：留下可核对的文件流转记录

若已有可运行的 DeerFlow，选一个**纯文本** `notes.txt` 上传并发起摘要任务。观察下面四处即可，无需修改生产代码或从零实现工具：

1. 上传响应里的 `virtual_path` 指向 `/mnt/user-data/uploads/notes.txt`；本轮消息中的 `<current_uploads>` 提供路径和有限的文件信息，不代表 Agent 已读完正文。
2. `read_file` 的工具结果出现文件内容；在 `workspace` 做中间处理，最终文件写入 `/mnt/user-data/outputs/summary.md`。
3. `present_files` 之后，读取 `GET /api/threads/{thread_id}/state` 的 `values.artifacts`，应包含 `/mnt/user-data/outputs/summary.md`；只把文件写进 `workspace` 不满足这一步。
4. 用当前用户的 Artifact 读取接口取回 `summary.md`，核对内容；若路径不存在，预期为 404。

本课选纯文本，是因为 `read_file` 读取文本；PDF、表格等二进制文件需要相应解析方式。上传时的文档转 Markdown 由 `uploads.auto_convert_documents` 控制，`config.example.yaml` 和 `backend/app/gateway/routers/uploads.py::_auto_convert_documents_enabled` 的默认值都是 `False`。当前 `backend/packages/harness/deerflow/agents/lead_agent/prompt.py` 的通用提示仍写着转换版 Markdown 可用；这句话不能保证当前上传已转换，应以配置及上传响应中是否真的有 `markdown_virtual_path` 为准。

想进一步练习源码复核，可分别查看 `backend/tests/test_uploads_router.py::test_upload_files_writes_thread_storage_and_skips_local_sandbox_sync`（共享目录为何跳过同步）、`backend/tests/test_present_file_tool_core_logic.py::test_present_files_rejects_paths_outside_outputs`（交付路径边界）和 `backend/tests/test_artifacts_router.py::test_get_artifact_reads_utf8_text_file_on_windows_locale`（读取路由返回实际文本）。正文已给出结论；打开测试的收益是亲自识别输入、断言和失败方式。

**面试验收**：能用 `notes.txt → summary.md` 连贯讲出上传、虚拟路径映射、首次获取或复用 Sandbox、租约释放、写入 `outputs`、`present_files` 登记和 Gateway 读取；能解释为什么“写了文件”“登记了路径”和“用户成功读到文件”需要分别验证；能说清 Local Sandbox 的路径映射为什么不等于安全隔离。下一课再讨论 Skill 如何让 Agent 按需获得领域操作说明。
