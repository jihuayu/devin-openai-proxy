# Cursor CLI Agent 内部机制分析（Toolcall / AgentLoop / Memory / 代码搜索）

> 免责声明：本文档基于本地 `cursor-agent` CLI bundle 的静态分析，用于学习理解其设计，不是官方接口文档。文中不会提供认证提取或可直接用于绕过客户端的实现代码。用于商业或生产目的前请遵循 Cursor 服务条款。

---

## 1. 总体定位：Cursor Agent 不是“本地大脑”

Cursor 的 `agent` CLI 是一个**事件驱动的 gRPC/Connect 客户端**。

- 真正的模型推理、工具策略、记忆管理都在云端后端（`api2.cursor.sh` / `api5.cursor.sh`）。
- CLI 负责：建立双向流、转发用户动作、执行工具、收集本地上下文、维护会话状态、重试恢复。

协议层我们已经分析过：`agent.v1.AgentService/Run` 是核心双向流，`AgentClientMessage`（客户端→服务端）和 `AgentServerMessage`（服务端→客户端）是两个 oneof 信封，内部再分若干子流。下面重点看在这层之上：

1. Agent 主循环怎么跑
2. Toolcall 怎么分发与执行
3. Memory / State 怎么保存与恢复
4. 代码搜索与上下文怎么收集

---

## 2. Agent Loop：一次 `Run` 的内部流程

### 2.1 入口与请求构造

`headless.ts`（以及 `ui.tsx`、`agent-session.ts`）最终都会调用 `AgentClient.run()`。调用前会构造：

- `ConversationStateStructure`（当前会话完整状态）
- `ConversationAction`（触发的动作，最常见是 `user_message_action`）
- `ModelDetails` / `RequestedModel`
- `AgentRunRequest` 的其它选项（`headers`、`customSystemPrompt`、`mcp_tools`、`skill_options` 等）

核心入口见 `/tmp/agent-client.js:1579`：

```ts
run(e, t, n, r, s, i, a, o, c, l, u)
// ctx, state, action, modelDetails, listener,
// resources, blobStore, controlledExecManager,
// checkpointHandler, hooks, runOptions
```

### 2.2 两种重试模式

`AgentClient.run` 根据 `useSharedTurnRunner` 选项进入两条路径：

- `runWithSharedTurnRunner`：较新的“共享 turn runner”模式，所有重试共享一个底层 stream runner。
- `runWithLegacyRetryLoop`：旧的独立重试循环。

两者最终都调用 `runInternal`，区别在重试/恢复状态机的位置。

### 2.3 `runInternal`：打开一条 bidi 流

`runInternal`（`/tmp/agent-client.js:2045`）主要做：

1. 构造 `AgentClientMessage.run_request`。
2. 通过 `this.client.run(ctx, ...)` 打开 `AgentService/Run` 双向流。
3. 把单一 bidi 流拆成 4 条逻辑子流：
   - `Interaction` 流：服务端 `AgentServerMessage.interaction_update` / `interaction_query`。
   - `Exec` 流：`exec_server_message` / `exec_client_message`。
   - `Checkpoint` 流：`conversation_checkpoint_update`。
   - `KV` 流：`kv_server_message` / `kv_client_message`。

拆分实现位于 `streamSplitter`，它把每个 `AgentServerMessage` 按 oneof 派发到对应的 writable iterable。

### 2.4 6 个并发 Handler

`runInternal` 底部（`/tmp/agent-client.js:2417-2424`）用 `Promise.allSettled` 同时启动：

```ts
Promise.allSettled([
  streamSplitter,         // 分发消息到子流
  execHandler,            // 执行工具
  interactionController,  // 处理 interaction_update / 响应 interaction_query
  checkpointController,   // 处理 checkpoint
  kvHandler,              // 处理 blob get/set
  conversationActionManager, // 把客户端的 conversation_action 写入流
])
```

这 6 个模块并行运行，直到流结束或出错。

### 2.5 Client Heartbeat

`runInternal` 还会每 5 秒写一个 `AgentClientMessage.client_heartbeat`（`y.kA`），服务端也回心跳，用于探测连接是否还活着。见 `/tmp/agent-client.js:2328-2343`。

### 2.6 重试/恢复机制

错误分类器在 `/tmp/agent-client.js:1484`，核心逻辑是：

- **stall（流卡住）**：默认重试最多 10 次；`endlessRetries` 模式下只重试 2 次就抛错（防无限卡死）。
- **Abort / Canceled**：视为传输错误，可重试。
- `RetriableError` 且服务端错误未超过 3 次：重试。
- 达到上限或不可恢复错误：抛出。

每次重试前，若已收到 checkpoint，会把 action 改成 `resumeAction`：

```ts
Q = k;  // 最新 checkpoint 状态
R = new ConversationAction({
  action: { case: "resumeAction", value: new ResumeAction() }
})
```

服务端收到 `resumeAction` 后，基于 `conversation_state` 继续之前挂起的 turn。这就是断线恢复的核心。

---

## 3. Toolcall：从模型意图到本地执行

### 3.1 两条通道：Interaction 与 Exec

模型要调工具时，会先在 `InteractionUpdate` 里发 “信号”：

- `tool_call_started`：告诉客户端开始了一个 tool call。
- `partial_tool_call` / `tool_call_delta`：流式传 tool call 参数。
- `tool_call_completed`：tool call 完整结束。

但**真正要执行的工具请求**是通过 `ExecServerMessage` 单独通道发送的。这个设计把 “展示/渲染” 与 “执行” 解耦：

- `InteractionUpdate` 用于 UI 展示模型思维、文本、工具计划。
- `ExecServerMessage` 是服务端给客户端的 “执行命令”，客户端必须完成并返回 `ExecClientMessage`。

### 3.2 `SimpleControlledExecManager` 工具分发

`agent-exec/dist/index.js` 实现了一个 `SimpleControlledExecManager`：

- 维护一个 `handlers` 数组。
- 收到 `ExecServerMessage` 后，按 `message.case` 匹配 handler。
- 每个 handler 把 `ExecServerMessage` 反序列成具体 args，调用本地执行器，再把结果序列成 `ExecClientMessage` 发回。

handler 注册方式示例（`/tmp/agent-exec.js:1225` 附近）：

```ts
// conversationSearch
register(new C(
  conversationSearchExec,
  F("conversationSearchArgs"),   // 反序列 args
  O("conversationSearchResult")  // 序列 result
))
```

`C` 是简单 handler，`y` 是流式 handler。每个 handler 会计算 `localExecutionTimeMs` 并返回，可以带 `hookAdditionalContexts`。

### 3.3 工具执行流程示例（shell）

`ExecServerMessage.shell_args` 的类型是 `ShellArgs`（`/tmp/shell_exec_pb.js:477`）：

```ts
message ShellArgs {
  string command = 1;
  string working_directory = 2;
  int32 timeout = 3;
  string tool_call_id = 4;
  repeated string simple_commands = 5;
  bool has_input_redirect = 6;
  bool has_output_redirect = 7;
  bool is_background = 8;
  bool skip_approval = 9;
  TimeoutBehavior timeout_behavior = 10;
  bool close_stdin = 11;
}
```

客户端拿到后：

1. 解析命令（`ShellParser`），识别出 `simple_commands` 和普通命令。
2. 根据 `UnifiedApprovalPolicy` 判断是否需要用户批准。
3. 调用 `ShellExecutor.execute()` 在本地启动子进程。
4. 把 `ShellResult`（success/failure/timeout/rejected/spawnError/permissionDenied）包进 `ExecClientMessage.shell_result` 返回。

`ShellResult` 的 `success` 包含 `stdout`、`stderr`、`exitCode`；`failure` 包含退出码和错误输出。见 `/tmp/shell_exec_pb.js:612`。

### 3.4 常见本地工具

`ExecServerMessage` 的 oneof 里有 50+ 种工具，可归为几类：

| 类别 | 代表 case | 作用 |
|------|----------|------|
| 文件读写 | `read_args`, `write_args`, `delete_args` | 读、写、删文件 |
| 目录浏览 | `ls_args` | 返回目录树，不只是平铺列表 |
| 代码搜索 | `grep_args` | 用 ripgrep 风格搜索项目 |
| 诊断 | `diagnostics_args`, `canvas_diagnostics_args` | LSP / 编译器诊断 |
| 上下文收集 | `request_context_args` | 让客户端重建 `RequestContext` |
| 终端/shell | `shell_args`, `shell_stream_args`, `background_shell_spawn_args` | 执行命令、长任务 |
| MCP | `mcp_args`, `list_mcp_resources_exec_args`, `read_mcp_resource_exec_args` | 调用 MCP server |
| 网络 | `fetch_args`, `web_fetch_allowlist_precheck_args` | 抓网页 / API |
| 子 agent | `subagent_args`, `force_background_subagent_args`, `subagent_await_args` | 启动/等待子 agent |
| 代码 diff | `git_diff_request` | 获取 git diff |
| 历史会话 | `conversation_search_args` | 跨会话自然语言搜索 |
| 安全检查 | `shell_allowlist_precheck_args`, `mcp_allowlist_precheck_args` | 工具预授权检查 |

### 3.5 部分执行细节

- **ls**：不是简单列出文件名，而是返回 `LsDirectoryTreeNode` 树，带 `children_dirs`、`children_files`、`num_files`、`full_subtree_extension_counts`、`TerminalMetadata`（cwd、最近命令）。见 `/tmp/ls_exec_pb.js:35`。

- **grep**：`GrepArgs` 含 `pattern`、`path`、`glob`、`output_mode`（count/files/content/union）、`workspace` 等。`GrepSuccess` 返回 `GrepFileMatch` 列表，每个 match 含 `line_number`、`content`、`is_context_line`，并带 `client_truncated`、`ripgrep_truncated` 标志。见 `/tmp/grep_exec_pb.js:37`。

- **read**：`ReadArgs` 含 `path`、`offset`、`limit`，`ReadSuccess` 返回 `content` 或 `data`（bytes）、`total_lines`、`file_size`、`truncated`、`hash`、`range_applied`，支持 redaction。见 `/tmp/read_exec_pb.js:34`。

- **write/delete**：通过 `write_args` / `delete_args` 调用本地 file executor，返回 `WriteResult` / `DeleteResult`，通常会更新 `file_states` 并触发 checkpoint。

### 3.6 审批与沙箱

执行是否需用户确认，由 `UnifiedApprovalPolicy` 决定。CLI 参数映射：

- `--yolo` / `--force`：跳过大部分审批，直接执行 shell/写文件。
- `--trust`：对当前 workspace 授信。
- `--mode ask`：只读模式，不会发起写/执行请求。

Shell 命令会先被解析为 `simple_commands`：如果是纯命令（无管道/重定向），且满足 allowlist/sandbox，可以直接执行；否则可能被拒绝或要求确认。

---

## 4. Memory / State：会话怎么记住上下文

### 4.1 `ConversationStateStructure` 是核心状态对象

`AgentRunRequest.conversation_state` 和 `conversation_checkpoint_update` 都携带 `ConversationStateStructure`（`/tmp/agent_pb.js:8522-8724`）。关键字段：

| 字段 | 作用 |
|------|------|
| `root_prompt_messages_json` | 系统/根 prompt 历史（repeated bytes） |
| `turns` | 每一轮 turn 的序列化状态（repeated bytes） |
| `todos` | 待办列表（repeated bytes） |
| `pending_tool_calls` | 等待人类确认/继续的工具调用 |
| `file_states` / `file_states_v2` | 文件状态 map（path → bytes / 结构） |
| `plans` | plan 数据 map |
| `subagent_states` | 子 agent 状态 map |
| `summary` / `summary_archives` | 会话摘要，用于长上下文压缩 |
| `token_details` | token 统计 |
| `read_paths` | 本轮已读的文件路径 |
| `active_branch_name` | 当前 git branch |
| `tracked_git_repo_branches` | 跟踪的 git 分支 |
| `subagent_runs_by_parent_tool_call_id` | 父子 agent 关联 |

大量字段是 `bytes` 或 `map<string, bytes>`，说明状态里很多部分是序列化 JSON / 二进制 blob，客户端只负责透传和存储，不解析语义。

### 4.2 Checkpoint 流

`CheckpointController`（`/tmp/agent-client.js:42`）监听 `conversation_checkpoint_update`，每收到一个就调用 `checkpointHandler.handleCheckpoint(ctx, checkpoint, metadata)`。

`headless.ts` 的 checkpoint 处理：

```ts
_ = {
  handleCheckpoint: async (ctx, checkpoint) => {
    await z.handleCheckpoint(ctx, checkpoint); // 更新会话对象
    O.writeFromState(ctx, checkpoint);          // 写本地持久化
  },
  getLatestCheckpoint: () => z.getLatestCheckpoint()
}
```

`handleCheckpoint` 在 `runInternal` 里还会把 `resumeEligibility` 提取出来，用于判断能否重试恢复。

### 4.3 本地持久化

`headless.ts` 创建 `TranscriptWriter`：

```ts
O = new w.Ko(L.workspacePath, z.getId(), z.getBlobStore());
```

它把 checkpoint 和 turn 结束状态写到工作区附近，用于 `--continue` 和 `--resume`。`writeFromState` 保存 state，`writeTurnEndedFromState` 保存 turn 结果。

### 4.4 KV Blob 存储

`KvHandler` 处理 `kv_server_message`：

- `get_blob_args`：服务端让客户端按 `blob_id` 取 blob。
- `set_blob_args`：服务端让客户端存 `blob_id` → `blob_data`。

客户端的 `blobStore`（SQLite / 本地文件）用来缓存/持久化大对象，避免在 gRPC 流里直接传巨大消息。见 `/tmp/kv_pb.js`。

### 4.5 Agent Store

`MountedAgentStore` 与 `agent-store-ids.js`（`/tmp/agent-store-ids.js`）一起用于子 agent 和后台任务：

- store id 格式：`store-<uuid>` 或 `bc-<...>-<uuid>`。
- 环境变量 `CURSOR_AGENT_STORE_SHARED_PATHS`、`CURSOR_AGENT_STORE_FILES_DIR` 控制共享路径。
- `agent_store_conflict_args` 工具用于检测和解决 store 冲突。

### 4.6 后台任务与 `BackgroundWorkRegistry`

`headless.ts`（`/tmp/headless.js:567-645`）在 turn 结束后，会检查 `backgroundWorkRegistry`：

- 如果后台 shell / 子 agent 还在跑，CLI 会等待。
- 当它们完成，把 `completions` 打包成 `backgroundTaskCompletionAction`，再发一次 `AgentRunRequest`。
- 这次请求的 action 是 `BackgroundTaskCompletionAction`，模型会根据结果继续回复。

---

## 5. 代码搜索：Cursor 怎么找到代码

### 5.1 两条路径

Cursor 的代码搜索是**服务端主导 + 客户端执行**的混合：

1. **预置上下文**：`UserMessageAction` 会带上一个 `RequestContext`，里面已经包含 rules、env、git、部分文件、project layout 等。
2. **按需工具**：模型在执行过程中会主动发 `ExecServerMessage` 调 `grep` / `read` / `ls` / `diagnostics` / `request_context_args` / `conversation_search_args`。

### 5.2 `RequestContext` 里有什么

`RequestContext` 是本地代码搜索的核心上下文对象（`/tmp/request_context_exec_pb.js:994-1212`）。主要字段：

| 字段 | 作用 |
|------|------|
| `rules` / `non_file_rules` | `.cursorrules`、项目规则、云规则 |
| `env` | 环境变量、OS、shell 信息 |
| `repository_info` | 仓库索引元数据（`relative_workspace_path` 等） |
| `git_repos` | git 状态、branch、tracked branches |
| `project_layouts` | 项目目录结构 |
| `file_contents` | map：显式 pin / 选中 的文件内容 |
| `tools` | 可用工具列表（MCP / skills） |
| `mcp_instructions` | MCP 使用说明 |
| `skill_options` / `agent_skills` | 启用的 skill |
| `precomputed_human_changes` | 预计算的人类修改 diff |
| `user_intent_summary` | 用户意图摘要 |
| `web_search_enabled` / `web_fetch_enabled` | 是否允许联网 |
| `*_info_complete` 标志 | 各模块是否已准备就绪 |

`repository_info_should_query_prod` 标志为 true 时，说明需要向后端索引服务查询仓库索引。

### 5.3 `request_context_args`：让客户端重建上下文

服务端可以发 `ExecServerMessage.request_context_args`（类型 `RequestContextArgs`，`/tmp/request_context_exec_pb.js:239-269`）让客户端重新收集 workspace 上下文。

```ts
message RequestContextArgs {
  optional string notes_session_id = 2;
  optional string workspace_id = 3;
  optional string read_only_pinned_tree_sha = 4;
  optional string read_only_plugin_cache_root = 5;
  optional bool use_cached = 7;
}
```

客户端调用本地 `LocalResourceProvider` 收集 rules、files、git、environment 等，构造 `RequestContext`，包进 `ExecClientMessage.request_context_result` 返回。`served_from_disk_cache` 表示命中了本地缓存。

### 5.4 `grep`：本地代码搜索的主力

`GrepArgs`（`/tmp/grep_exec_pb.js:37`）字段：

- `pattern`：搜索正则/字符串。
- `path`：相对或绝对路径（可选）。
- `glob`：文件过滤 glob（可选）。
- `output_mode`：count / files / content / union。
- `workspace`：workspace 标识。

`GrepSuccess`（`/tmp/grep_exec_pb.js:204`）返回：

- `workspaceResults`：按 workspace 分组的结果。
- 不同 output_mode 对应不同结构：`GrepCountResult`、`GrepFilesResult`、`GrepContentResult`、`GrepUnionResult`。
- `GrepContentResult` 返回 `GrepFileMatch` 列表，每个文件含 `GrepContentMatch`（`line_number`、`content`、`is_context_line`）。
- 结果带 `client_truncated` 和 `ripgrep_truncated`：表示被客户端或被底层 ripgrep 截断了。

这说明底层实际执行很可能是 `ripgrep`，客户端只是把参数传进去并格式化结果。

### 5.5 `ls`：不是简单列文件

`LsArgs`（`/tmp/ls_exec_pb.js:35`）字段：

- `path`
- `ignore`：忽略模式数组
- `tool_call_id`

`LsSuccess` 返回 `LsDirectoryTreeNode` 树：

- `abs_path`
- `children_dirs`（子目录节点，递归）
- `children_files`（文件节点）
- `num_files`
- `full_subtree_extension_counts`
- `children_were_processed`

它会返回一棵**目录树**，帮助模型理解项目整体结构，而不仅仅是当前目录的文件列表。

### 5.6 `read`：按需读文件

`ReadArgs` 支持 `offset`、`limit`，`ReadSuccess` 支持内容、字节数据、行号、hash、truncated 标志。这意味着模型可以只读文件的一部分，不需要把整个大文件塞进 prompt。

### 5.7 `diagnostics`：LSP / 编译器诊断

`diagnostics_args` 会收集当前 workspace 的 LSP 错误或警告，返回给模型，用于修复 bug、找类型错误。

### 5.8 `conversation_search`：跨会话记忆搜索

`ConversationSearchArgs`（`/tmp/conversation_search_exec_pb.js:40`）：

- `query`：自然语言查询
- `tool_call_id`
- `limit`

`ConversationSearchSuccess` 返回 `hits`：

- `conversation_id`
- `title`
- `source`（枚举：当前会话 / 其它会话）
- `updated_at_ms`

这允许模型“回忆”你之前跟 Cursor 聊过的内容。

### 5.9 后端索引服务

CLI 网络配置里提到 `repo42.cursor.sh` 用于 codebase indexing（HTTP/2 only）。`RequestContext` 里的 `repository_info` 可能就是由该服务提供的索引元数据。客户端不一定自己建完整索引，而是把索引元数据/搜索结果传回后端，由后端做语义检索。

---

## 6. Toolcall / Memory / Code Search 如何协同

一个典型多轮 agent turn 的完整数据流：

```
1. 用户输入
   → UserMessageAction（带 RequestContext + userMessage.text）
   → AgentRunRequest

2. 服务端思考
   → AgentServerMessage.interaction_update.thinking_delta/text_delta

3. 服务端决定搜索代码
   → AgentServerMessage.exec_server_message.grep_args
   → 客户端执行 grep，返回 ExecClientMessage.grep_result

4. 服务端决定读文件
   → AgentServerMessage.exec_server_message.read_args
   → 客户端返回 ReadSuccess

5. 服务端决定编辑
   → AgentServerMessage.exec_server_message.write_args
   → 客户端写文件，返回 WriteSuccess

6. 服务端输出文本
   → interaction_update.text_delta

7. Turn 结束
   → interaction_update.turn_ended
   → conversation_checkpoint_update（ConversationStateStructure）

8. 本地保存 checkpoint / turn 到 transcript writer

9. 若连接断开，用 resumeAction + 最新 checkpoint 重试
```

---

## 7. 映射到 OpenAI 格式

如果要给 Cursor Agent 包一层 OpenAI-compatible 接口，重点映射关系：

| Cursor 内部 | OpenAI 对应 |
|-------------|-------------|
| `AgentRunRequest` with `user_message_action` | `POST /v1/chat/completions` 的 `messages` |
| `UserMessage.text` | `messages[].content` |
| `InteractionUpdate.text_delta` | `chat.completion.chunk.delta.content` |
| `InteractionUpdate.thinking_delta` | `delta.reasoning_content`（非标准，部分客户端支持） |
| `InteractionUpdate.tool_call_*` | `delta.tool_calls`（需自己拼装） |
| `InteractionUpdate.turn_ended` | 最后一个 chunk 的 `finish_reason: "stop"` |
| `ExecServerMessage.*` | 不直接暴露；在 `agent` 模式下由本地执行 |
| `result.usage` | `usage.prompt_tokens` / `completion_tokens` |

差异：

- Cursor 的 `conversation_state` 比 OpenAI 的 `messages` 大得多，包含文件状态、计划、子 agent 状态等。
- Cursor 的 tool call 是流式 partial，OpenAI 是完整的 JSON object。
- Cursor 有 thinking、shell output、step 等额外事件，OpenAI 标准流没有。

因此最稳妥的包法还是像我们之前给的 `cursor_openai_bridge.py`：把 `agent` CLI 当后端，只把 `text_delta` 和最终 `result` 映射成 SSE；不要自己实现整个 gRPC agent loop。

---

## 8. 总结

| 模块 | 关键文件 | 核心要点 |
|------|----------|----------|
| Agent Loop | `/tmp/agent-client.js` | bidi stream + 4 子流 + 6 并发 handler + 重试/恢复 |
| Tool Call | `/tmp/agent-exec.js`, `/tmp/exec_pb.js`, `/tmp/shell_exec_pb.js`, `/tmp/grep_exec_pb.js` | `ExecServerMessage` 分发到本地 handler，结果通过 `ExecClientMessage` 返回 |
| Memory | `/tmp/agent_pb.js`, `/tmp/kv_pb.js` | `ConversationStateStructure` + checkpoint + blob store + transcript |
| 代码搜索 | `/tmp/request_context_exec_pb.js`, `/tmp/grep_exec_pb.js`, `/tmp/ls_exec_pb.js`, `/tmp/read_exec_pb.js` | `RequestContext` 预置上下文 + `grep`/`read`/`ls`/`diagnostics`/`request_context_args` 工具 |

本文档只覆盖结构与设计，不覆盖认证、token 提取或可直接连接 `api2.cursor.sh` 的代码。如果要继续研究某一块（例如 `RequestContext` 的构建过程、`UnifiedApprovalPolicy` 的 allowlist 逻辑、子 agent 生命周期），可以继续拆。
