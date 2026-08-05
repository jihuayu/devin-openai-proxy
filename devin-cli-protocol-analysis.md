# Devin CLI 协议分析文档

> 版本：v3000.3.27 (0becb483)  
> 分析来源：静态二进制字符串提取（`~/.local/share/devin/cli/_versions/3000.3.27/bin/devin`）+ 官方文档  
> 性质：学习性逆向分析，所有字段名、结构、方法均来自二进制中嵌入的调试字符串和错误消息。部分字段含义为合理推断，非 Cognition 官方文档。

---

## 1. 概述

Devin CLI 的后端通信不是单一协议，而是**多协议分层架构**：

1. **Devin API / Connect-RPC**：业务、状态、配额、模型配置以及**真正的 LLM 推理**（`api.devin.ai`）
2. **Raindrop**：可观测性/telemetry 平台，上报事件、span、trace、signal（`api.raindrop.ai`）
3. **ACP**：Agent Client Protocol，本地 REPL/编辑器/云端 session 的统一 JSON-RPC 2.0 协议
4. **MCP**：Model Context Protocol，连接外部工具服务器
5. **Telemetry/Config**：Sentry、Unleash、产品分析

本份文档重点分析 **Devin API 的 LLM 推理协议（Connect-RPC）** 和 **ACP 协议**，因为它们是 agent loop 的核心。

---

## 2. 协议分层总览

```
┌─────────────────────────────────────┐
│           Devin CLI 进程            │
│  chisel (REPL/UI)                   │
│  affogato (agent loop)              │
│  agent-ext (effects/looper)         │
│  toolbox (tools)                    │
│  raindrop-rust (telemetry)          │
└──────────────┬──────────────────────┘
               │
    ┌──────────┼──────────┬──────────────┐
    ▼          ▼          ▼              ▼
 Devin API   Raindrop   ACP (JSON-RPC)  Telemetry
(Connect/   (telemetry) (stdIO/WS/SSE)  (Sentry/Unleash)
  REST)         │
    │           │
    ▼           ▼
 api.devin.ai  api.raindrop.ai/v1/
```

---

## 3. 认证与凭证

### 3.1 登录方式

- **PKCE OAuth 2.0**：浏览器打开 `https://app.devin.ai`，`code_challenge_method=S256`
- **手动 token flow**：`/login <code>`
- **Windsurf 集成**：环境变量 `WINDSURF_API_KEY`

### 3.2 凭证文件

路径：`~/.config/devin/credentials.toml`

```toml
api_server_url = "https://api.devin.ai"
devin_webapp_host = "https://app.devin.ai"
devin_api_url = "https://api.devin.ai"
windsurf_api_key = "..."
api_key = "..."
```

### 3.3 请求头

通用头：

```
Authorization: Bearer <access_token>
Content-Type: application/json
X-Raindrop-Sdk: raindrop-rust   # Raindrop 特有
```

---

## 4. Raindrop Telemetry（可观测性）协议

> **重要修正**：通过进一步对比公开 SDK（`github.com/raindrop-ai/go`）与二进制中的源码路径，确认 `api.raindrop.ai/v1/` **不是 LLM 推理网关**，而是 Raindrop 可观测性/telemetry 平台。Devin CLI 的 LLM 推理实际上通过 `api.devin.ai` 的 Connect-RPC 服务完成（见第 6 节）。二进制中看到的 `InferenceRequest`、`ChatMessageInner` 等结构属于 `windsurf-api-client` 的推理调用，Raindrop SDK 只是负责把推理事件、span、trace 上报到 `api.raindrop.ai`。

### 4.1 基础信息

| 项目 | 值 |
|------|-----|
| 生产 Base URL | `https://api.raindrop.ai/v1/` |
| 本地调试 URL | `http://localhost:5899/v1/`（`RAINDROP_LOCAL_DEBUGGER` / `RAINDROP_WORKSHOP`） |
| SDK 标识 | `raindrop-rust` / `raindrop.rust-sdk` |
| 专用头 | `X-Raindrop-Sdk`、`X-Raindrop-Project-Id` |
| 客户端 crate | `raindrop-rust`（本地 git checkout） |
| CLI 封装 | `agent-ext/src/raindrop.rs`、`windsurf-api-client/src/analytics_client.rs` |
| 核心端点 | `POST /v1/events/track_partial`、`POST /v1/signals/track`、`POST /v1/traces` |
| 认证方式 | `Authorization: Bearer <write_key>`（即 Devin CLI 的 API key / session token） |
| 用途 | 追踪 AI 事件、交互（interaction）、span/trace、用户反馈 signal |

### 4.2 InferenceRequest（13 个字段）

> 以下字段属于 `windsurf-api-client` 实际调用 `GetDevstralStream` / `GetChatMessage` 时使用的 protobuf 请求结构，**并非 Raindrop telemetry 的请求体**。Raindrop 只接收这些推理事件的脱敏/聚合后遥测数据。

字段按 JSON 序列化顺序（合理推断）：

```json
{
  "model": "swe-1-6-fast",
  "messages": [...],
  "tools": [...],
  "query_label": "...",
  "is_user_initiated": true,
  "completion_config": {
    "temperature": 0.7,
    "top_p": 0.9,
    "top_k": 40,
    "max_tokens": 8192
  },
  "max_trailing_images": 4,
  "execution_id": "...",
  "agent_context": {...},
  "disable_prompt_cache_writes": false,
  "system_prefix_len": 1234,
  "hosted_tool_search": {...},
  "generation_id": "..."
}
```

字段说明：

| 字段 | 类型 | 说明 |
|------|------|------|
| `model` | string | 模型标识，如 `swe`, `opus`, `sonnet`, `codex`, `gemini` |
| `messages` | array | 对话消息，OpenAI 兼容格式 |
| `tools` | array | 可用工具定义 |
| `query_label` | string | 推理用途标签，用于 telemetry/分类 |
| `is_user_initiated` | bool | 是否用户主动输入触发 |
| `completion_config` | object | `{temperature, top_p, top_k, max_tokens}` |
| `max_trailing_images` | int | 允许尾随的最大图片数 |
| `execution_id` | string | 单次执行的追踪 ID |
| `agent_context` | object | 6 字段，见下文 |
| `disable_prompt_cache_writes` | bool | 是否禁用 prompt cache 写入 |
| `system_prefix_len` | int | system prompt 的 token 长度 |
| `hosted_tool_search` | object | 托管工具搜索配置 |
| `generation_id` | string | 生成追踪 ID |

### 4.3 AgentContext（6 个字段）

```json
{
  "cwd": "/Users/.../project",
  "agent_id": "...",
  "chain_metadata": {...},
  "read_char_limit": 100000,
  "read_only_profile": false,
  "chain_tool_call": "..."
}
```

### 4.4 ChatMessageInner（11 个字段）

```json
{
  "message_id": "...",
  "role": "user",
  "content": "...",
  "images": [...],
  "tool_call_id": "...",
  "tool_calls": [...],
  "thinking": "...",
  "reasoning_details": [...],
  "metadata": {...},
  "tool_search_result": {...},
  "hosted_tool_searches": [...]
}
```

### 4.5 ChatMessageMetadata（13 个字段）

```json
{
  "num_tokens": 1234,
  "is_user_input": true,
  "request_id": "...",
  "metrics": {...},
  "finish_reason": "...",
  "extensions": [...],
  "committed_credit_cost": 0.001,
  "committed_acu_cost": 0.001,
  "started_generation_at": "...",
  "created_at": "...",
  "from_event_id": "...",
  "telemetry": {...},
  "loaded_tool_names": ["read", "exec"]
}
```

### 4.6 ImageData（6 个字段）

```json
{
  "width": 1024,
  "height": 768,
  "base64_data": "...",
  "mime_type": "image/png",
  "source_path": "/path/to/img.png",
  "caption": "..."
}
```

### 4.7 Tool / ToolCall 定义

**ToolDefinition（6 个字段）：**

```json
{
  "name": "read",
  "description": "...",
  "parameters": {...},
  "custom_tool": false,
  "defer_loading": false,
  "grammar": "...",
  "grammar_syntax": "..."
}
```

**ParsedToolCall（7 个字段）：**

```json
{
  "id": "call_xxx",
  "inference_tool_name": "read",
  "key": "read",
  "arguments": "{\"file_path\": \"/etc/passwd\"}",
  "index": 0,
  "kind": "function",
  "namespace": "core"
}
```

字段说明：

- `inference_tool_name` / `key`：工具名，用于推理
- `namespace` / `kind`：命名空间和种类，`core` 为内置，`mcp__server__name` 为 MCP
- `arguments`：JSON 字符串，与 OpenAI 一致
- `index`：一次响应中多个 tool call 的索引

**ToolResult（9 个字段）：**

```json
{
  "success": true,
  "content": "...",
  "display_content": "...",
  "expanded_display_content": "...",
  "failure_reason": null,
  "message_extensions": [...],
  "undo_actions": [...],
  "scope": "...",
  "...": "..."
}
```

### 4.8 响应事件流

Devin LLM 推理返回的是**自定义事件流**（由 `GetDevstralStream` 流式响应解析，可能基于 SSE 或 chunked JSON），CLI 内部解析为以下事件类型：

| 事件名 | 含义 |
|--------|------|
| `Loaded` | 开始加载/连接 |
| `ThinkingToken` | 思考 token（类似 reasoning content） |
| `ContentToken` | 文本内容 token |
| `ContentComplete` | 文本内容输出完成 |
| `ToolRequest` | 模型请求调用工具 |
| `ToolCallDelta` | tool call 增量更新 |
| `ToolResult` | 工具执行结果输入 |
| `ToolUpdate` | 工具调用状态更新 |
| `CompactionStarted` | 上下文压缩开始 |
| `Compacted` | 上下文压缩完成 |
| `Stopped` | 停止 |
| `IterationSnapshot` | 迭代快照 |
| `RetryingInference` | 重新尝试推理 |
| `ConnectionStream` | 连接流 |
| `Retry` | 重试事件 |
| `MovedToBackground` | agent 转后台 |
| `MovedToForeground` | agent 转前台 |
| `ProfileChanged` | profile/model 切换 |
| `InjectedUserMessage` | 注入用户消息 |
| `ShowModal` | 显示弹窗 |
| `Extension` | 扩展事件 |

### 4.9 与 OpenAI 格式的映射

Devin LLM 推理请求的消息部分与 OpenAI 兼容，但工具有自己的 namespace/key 体系。

**请求映射到 OpenAI：**

| Devin 字段 | OpenAI 字段 |
|---------------|-------------|
| `model` | `model` |
| `messages` | `messages` |
| `tools` | `tools`（但 schema 需转换） |
| `completion_config.temperature` | `temperature` |
| `completion_config.top_p` | `top_p` |
| `completion_config.max_tokens` | `max_tokens` |
| `agent_context` / `query_label` 等 | 无对应，需丢弃或模拟 |

**响应映射到 Devin（OpenAI → Devin 内部事件）：**

| OpenAI SSE | Devin 内部事件 |
|------------|----------------|
| `choices[0].delta.content` | `ContentToken` |
| 结束 | `ContentComplete` |
| `choices[0].delta.tool_calls[].function.name/arguments` | `ToolCallDelta` → 累积为 `ToolRequest` |
| `finish_reason` | `stop_reason` |

### 4.10 错误与重试

错误消息：

- `raindrop: http error`
- `raindrop: json error`
- `raindrop: config error`
- `raindrop: dropping oversized payload (> 1 MiB)`
- `HTTP 413 Payload Too Large with image cap`
- `Backend stream creation failed (attempt /), retrying in s`
- `Transient inference error; retrying on next iteration`
- `Exhausted inference retries; stopping turn`
- `Model returned no tool calls; discarding turn and re-sampling from a clean state`

重试逻辑：
- HTTP 413 自动减少 `max_trailing_images` 重试
- 流式连接失败按指数退避重试
- 非流式错误最多 N 次重试

### 4.11 Telemetry / PerformanceMetrics

Devin CLI 通过 `raindrop-rust` SDK 把推理事件和 span 上报到 Raindrop，集成 OpenTelemetry 语义，用于追踪：

```
gen_ai.prompt.0.role
gen_ai.prompt.0.content
gen_ai.completion.0.role          # = assistant
gen_ai.completion.0.content
gen_ai.response.model
gen_ai.request.model
gen_ai.usage.input_tokens
gen_ai.usage.output_tokens
gen_ai.system
ai.prompt
ai.prompt.messages
ai.model.id
ai.model.provider
ai.response.text
traceloop.entity.input
traceloop.entity.output
traceloop.entity.duration_ms
traceloop.association.properties.event_id
```

**PerformanceMetrics（8 个字段）：**

```json
{
  "ttft_ms": 123,
  "total_time_ms": 4567,
  "input_tokens": 1000,
  "output_tokens": 500,
  "cache_read_tokens": 0,
  "cache_creation_tokens": 0,
  "tpot_ms": 50,
  "tokens_per_sec": 120.5
}
```

---

## 5. ACP（Agent Client Protocol）

### 5.1 协议基础

- **标准**：JSON-RPC 2.0
- **Crate**：`agent-client-protocol-1.0.0`
- **传输**：stdio（编辑器）、WebSocket（handoff）、SSE（远程）
- **源码路径**：`chisel-agent/src/acp_server/`、`agent-client-protocol-1.0.0/src/jsonrpc/`

### 5.2 ACP 方法

完整方法列表：

```
initialize
authenticate
session/new
session/load
session/set_mode
session/set_config_option
session/prompt
session/cancel
session/list
session/delete
session/resume
session/close
session/rename
_client/commandRevise
```

### 5.3 核心请求/响应结构

**InitializeRequest / InitializeResponse：**

```json
{
  "jsonrpc": "2.0",
  "method": "initialize",
  "params": {
    "protocolVersion": "2024-11-05",
    "capabilities": {...},
    "clientInfo": {...}
  },
  "id": 1
}
```

`InitializeResponse` 包含：

```json
{
  "agentCapabilities": {...},
  "authMethods": [...],
  "agentInfo": {...},
  "sessionId": "...",
  "...": "..."
}
```

**NewSessionRequest：**

```json
{
  "jsonrpc": "2.0",
  "method": "session/new",
  "params": {
    "working_directory": "...",
    "backend_type": "...",
    "model": "swe-1-6-fast",
    "agent_mode": "normal"
  }
}
```

**PromptRequest：**

```json
{
  "jsonrpc": "2.0",
  "method": "session/prompt",
  "params": {
    "sessionId": "...",
    "content": "...",
    "is_shell": false
  }
}
```

**SetSessionModeRequest：**

```json
{
  "jsonrpc": "2.0",
  "method": "session/set_mode",
  "params": {
    "sessionId": "...",
    "modeId": "..."
  }
}
```

**SetSessionConfigOptionRequest：**

```json
{
  "jsonrpc": "2.0",
  "method": "session/set_config_option",
  "params": {
    "sessionId": "...",
    "category": "...",
    "configId": "...",
    "value": "..."
  }
}
```

### 5.4 能力（Capabilities）

**AgentCapabilities（6 个字段）：**

```json
{
  "loadSession": true,
  "promptCapabilities": {...},
  "mcpCapabilities": {...},
  "sessionCapabilities": {...},
  "authSessionCapabilities": {...},
  "...": "..."
}
```

**PromptCapabilities（4 个字段）：**

```json
{
  "image": true,
  "audio": false,
  "embeddedContext": true,
  "url": true
}
```

**SessionCapabilities（6 个字段）：**

```json
{
  "list": true,
  "delete": true,
  "resume": true,
  "close": true,
  "...": "..."
}
```

**McpCapabilities（3 个字段）：**

```json
{
  "version": "...",
  "method": "...",
  "...": "..."
}
```

### 5.5 Session 配置

**SessionMode（4 个字段）：**

```json
{
  "id": "normal",
  "modeId": "normal",
  "name": "Normal",
  "category": "..."
}
```

**SessionConfigOption / SessionConfigSelect：**

```json
{
  "type": "select",
  "category": "...",
  "currentValue": "...",
  "options": [
    {
      "value": "...",
      "name": "...",
      "description": "..."
    }
  ]
}
```

### 5.6 通知（Notifications）

```
notifications/initialized
notifications/cancelled
notifications/resources/list_changed
notifications/prompts/list_changed
notifications/resources/updated
```

---

## 6. Devin API（REST / gRPC / Connect）

### 6.1 基础 URL

- `https://api.devin.ai`
- `https://app.devin.ai`（webapp/登录）
- `https://app.beta.devin.ai`（beta）

### 6.2 REST 端点

| 端点 | 说明 |
|------|------|
| `/v3/organizations/` | 组织信息 |
| `/v3beta1/organizations/` | Beta 组织 API |
| `/sessions?session_ids=` | 批量查询会话 |
| `/sessions/` | 会话 CRUD |
| `/api/cli/update` | CLI 更新检查 |
| `/usage` | 用量统计 |

### 6.3 gRPC / Connect 服务

Connect-RPC 协议，`exa.*_pb.*Service` 路径：

| 服务 | 方法 |
|------|---------|
| `exa.api_server_pb.ApiServerService` | `AssignModel`, `GetDevstralStream`, `GetChatMessage`, `GetImageCaption`, `GetCliModelConfigs`, `GetWebSearchResults`, `RecordTrajectorySegmentEvents` |
| `exa.seat_management_pb.SeatManagementService` | `GetUserStatus`, `GetCliTeamSettings`, `ExchangePKCEAuthorizationCode`, `ExchangeDevinCLIPKCECode` |
| `exa.browser_preview_pb.BrowserPreviewService` | — |
| `exa.attribution_pb.AttributionService` | `Attribution` |
| `exa.product_analytics_pb.ProductAnalyticsService` | `BatchRecordAnalyticsEvents` |

### 6.4 LLM 推理协议（Connect-RPC）

> 这是 Devin CLI 真正进行 LLM 推理/模型补全的入口，与 Raindrop telemetry 是两套系统。

**Base URL**：由 `devin_api_url` / `WINDSURF_API_SERVER_URL` 决定；二进制中同时存在 `api.devin.ai` 与 `server.codeium.com`

- 默认/典型：`https://api.devin.ai`
- 实际抓包中观察到：`https://server.codeium.com`（由用户侧的 `devin_api_url` 配置指定）
- 覆盖方式：`devin_api_url` 凭证项或 `WINDSURF_API_SERVER_URL` 环境变量

**传输协议**：Connect-RPC
- HTTP 方法：`POST`
- Content-Type：`application/connect+proto`（实际抓包中 `GetChatMessage` 也使用流式 envelope）
- 头：`Connect-Protocol-Version: 1`
- Auth：`Authorization: Basic devin-session-token$<jwt>`（实际格式，JWT 内含 `session_id`）

**推理调用链**：

```
1. AssignModel
   POST /exa.api_server_pb.ApiServerService/AssignModel
   输入：模型别名/需求（完整 protobuf 字段未在静态二进制中暴露，推测至少含 `model_router` 字符串）
   输出：ModelAssignment { assignment_jwt, harness_uids, model_uid }

2. （可选）GetImageCaption
   POST /exa.api_server_pb.ApiServerService/GetImageCaption
   输入：图片（请求类型未暴露，推测含 `image` / `image_url` / `image_data`）
   输出：{ caption }

3. 流式补全
   POST /exa.api_server_pb.ApiServerService/GetDevstralStream
   输入：InferenceRequest（protobuf）
   输出：Connect 流式 GetDevstralStreamResponse

4. 非流式/旧路径
   POST /exa.api_server_pb.ApiServerService/GetChatMessage
   输出：GetChatMessageResponse
```

**`GetDevstralStream` 请求**（对应二进制中的 `InferenceRequest`）包含以下字段：

```text
model
messages
tools
query_label
is_user_initiated
completion_config { temperature, top_p, top_k, max_tokens }
max_trailing_images
execution_id
agent_context
    parent_agent_id
    child_agent_id
    extension_id
    ...
disable_prompt_cache_writes
system_prefix_len
hosted_tool_search
generation_id
```

**`GetDevstralStream` 响应流字段**：

```text
output
tool_calls
message_id
delta_text
delta_tokens
stop_reason
usage
redact
delta_thinking
delta_signature
thinking_redacted
latency
timestamp
completion_profile
credit_cost
output_id
thinking_id
request_id
committed_credit_cost
prompt
gemini_thought_signature
delta_signature_type
committed_acu_cost
arena_invocation_cap_reached
phase
committed_quota_cost_basis_points
committed_overage_cost_cents
response_dimension_groups
```

**`GetChatMessage` 响应字段**：

```text
output_id, request_id, message_id, delta_text, delta_tokens,
stop_reason, usage, redact, delta_thinking, delta_signature,
thinking_redacted, latency, timestamp, completion_profile,
credit_cost, thinking_id, committed_credit_cost, prompt,
gemini_thought_signature, delta_signature_type, committed_acu_cost,
arena_invocation_cap_reached, phase,
committed_quota_cost_basis_points, committed_overage_cost_cents,
response_dimension_groups
```

**`GetImageCaption` 响应**：

```text
caption
```

### 6.5 protobuf 字段编号（从二进制字段顺序字符串推断）

> ⚠️ **重大修正**：后续实际抓包显示，二进制字符串反映的是 Rust 内部结构体/JSON schema 字段顺序，**不是 `.proto` 的 wire 编号**。真正的 protobuf 字段编号与下表不同。本节保留为“静态推断过程”，实际 wire 编号见 [6.6 实际抓包结果]。

#### 请求字段

**`InferenceRequest`**（`GetDevstralStream` / `GetChatMessage` 共享输入）

| 编号 | 字段 | 备注 |
|------|------|------|
| 1 | `model` | 字符串，模型别名 |
| 2 | `messages` | `repeated ChatMessageInner` |
| 3 | `tools` | `repeated ToolDefinition` |
| 4 | `query_label` | 字符串，Cognition 内部标签 |
| 5 | `is_user_initiated` | bool |
| 6 | `completion_config` | `CompletionConfig` |
| 7 | `max_trailing_images` | uint32 / int32 |
| 8 | `execution_id` | 字符串，追踪 ID |
| 9 | `agent_context` | `AgentContext` |
| 10 | `disable_prompt_cache_writes` | bool |
| 11 | `system_prefix_len` | uint32 / int32 |
| 12 | `hosted_tool_search` | `HostedToolSearchConfig` |
| 13 | `generation_id` | 字符串 |

**`CompletionConfig`**（嵌套在 `InferenceRequest` 中）

| 编号 | 字段 |
|------|------|
| 1 | `temperature` |
| 2 | `top_p` |
| 3 | `top_k` |
| 4 | `max_tokens` |

**`AgentContext`**（嵌套在 `InferenceRequest` 中）

| 编号 | 字段 |
|------|------|
| 1 | `cwd` |
| 2 | `agent_id` |
| 3 | `chain_metadata` |
| 4 | `read_char_limit` |
| 5 | `read_only_profile` |
| 6 | `chain_tool_call` |

**`ChatMessageInner`**（`messages` 数组元素）

| 编号 | 字段 | 备注 |
|------|------|------|
| 1 | `message_id` | 字符串 |
| 2 | `role` | 字符串 `user` / `assistant` / `tool` |
| 3 | `content` | 字符串或 image 数组 |
| 4 | `images` | `repeated ImageData` |
| 5 | `tool_call_id` | 字符串 |
| 6 | `tool_calls` | `repeated ParsedToolCall` |
| 7 | `thinking` | 字符串 |
| 8 | `reasoning_details` | `repeated Any` |
| 9 | `metadata` | map / struct |
| 10 | `tool_search_result` | `ToolSearchResult` |
| 11 | `hosted_tool_searches` | `repeated HostedToolSearchEvent` |

**`ImageData`**（`images` 数组元素）

| 编号 | 字段 |
|------|------|
| 1 | `width` |
| 2 | `height` |
| 3 | `base64_data` |
| 4 | `mime_type` |
| 5 | `source_path` |
| 6 | `caption` |

**`ParsedToolCall`**（`tool_calls` 数组元素）

| 编号 | 字段 |
|------|------|
| 1 | `id` |
| 2 | `inference_tool_name` |
| 3 | `key` |
| 4 | `arguments` |
| 5 | `index` |
| 6 | `kind` |
| 7 | `namespace` |

#### 响应字段

**`GetChatMessageResponse`**（非流式响应，也作为流式 `GetDevstralStream` 每个 chunk 的完整字段）

| 编号 | 字段 | 备注 |
|------|------|------|
| 1 | `message_id` | 字符串 |
| 2 | `delta_text` | 字符串；新增文本片段 |
| 3 | `delta_tokens` | uint32；新增 token 数 |
| 4 | `stop_reason` | `StopReason` 枚举 |
| 5 | `usage` | `ModelUsageStats` |
| 6 | `redact` | bool |
| 7 | `delta_thinking` | 字符串 |
| 8 | `delta_signature` | 字符串 |
| 9 | `thinking_redacted` | bool |
| 10 | `latency` | uint64 / double |
| 11 | `timestamp` | uint64 / Timestamp |
| 12 | `completion_profile` | `CompletionProfile` |
| 13 | `credit_cost` | uint64 / 分 |
| 14 | `output_id` | 字符串 |
| 15 | `thinking_id` | 字符串 |
| 16 | `request_id` | 字符串 |
| 17 | `committed_credit_cost` | uint64 |
| 18 | `prompt` | 字符串；可能是完整 prompt |
| 19 | `gemini_thought_signature` | 字符串 |
| 20 | `delta_signature_type` | 字符串 |
| 21 | `committed_acu_cost` | uint64 |
| 22 | `arena_invocation_cap_reached` | bool |
| 23 | `phase` | 字符串 |
| 24 | `committed_quota_cost_basis_points` | uint32 |
| 25 | `committed_overage_cost_cents` | uint64 |
| 26 | `response_dimension_groups` | `repeated ResponseDimensionGroup` |

**`GetDevstralStreamResponse`**（二进制字符串显示为 `output`, `tool_calls` 两个字段，可能是把流式事件抽象为 `output` 内容或 `tool_calls` 更新）

| 编号 | 字段 |
|------|------|
| 1 | `output` |
| 2 | `tool_calls` |

**`ModelAssignment`**（`AssignModel` 的 `assignment` 字段类型）

| 编号 | 字段 |
|------|------|
| 1 | `assignment_jwt` |
| 2 | `harness_uids` |
| 3 | `model_uid` |

**`AssignModelResponse`**

| 编号 | 字段 |
|------|------|
| 1 | `assignment`（`ModelAssignment`） |

#### 待确认

- `GetDevstralStream` 的流式响应具体使用的是 `GetChatMessageResponse` 逐条下发，还是 `GetDevstralStreamResponse` 作为 `oneof` 包装后下发，**需要真实流量验证**。
- `AssignModel` 的**请求体**未在二进制字符串中暴露完整字段；目前只能根据日志推断至少包含 `model_router`（字符串），其余字段未知。
- `GetImageCaption` 的**请求体**也未暴露，可能包含 `image` / `image_url` / `image_data` 等字段。

### 6.6 实际抓包结果

> 以下数据来自用户本地用 `mitmproxy` 截获的真实流量，服务端为 `https://server.codeium.com`，方法是 `POST /exa.api_server_pb.ApiServerService/GetChatMessage`。这是目前最可靠的协议结构，会随更多抓包继续修正。

#### `GetChatMessage` 请求

- **Content-Type**：`application/connect+proto`
- **Envelope**：单个消息，5 字节头 `0x00` + 长度 + 请求 protobuf
- **请求头**：
  - `connect-protocol-version: 1`
  - `authorization: Basic devin-session-token$<jwt>`
  - `sentry-trace: ...`
- **Token 来源**：
  - 存储在 `~/.local/share/devin/credentials.toml` 的 `windsurf_api_key = "devin-session-token$<jwt>"`
  - 该文件还包含 `api_server_url = "https://server.codeium.com"`、`devin_webapp_host = "app.devin.ai"`、`devin_api_url = "https://api.devin.ai"`
  - JWT payload 仅含 `session_id`，无 `exp`，由服务器端 HS256 签名
- **顶层消息字段**（按 `protoc --decode_raw`）：

| 字段 | 类型 | 含义 |
|------|------|------|
| 1 | 子消息 | 客户端元数据：`client`、版本、`session_token`、locale、平台等 |
| 2 | 字符串 | **System prompt**（完整的 Devin 系统提示） |
| 3 | repeated 子消息 | **消息列表**；每条消息包含 `message_id`（1）、`role`（2，varint）、`content`（3） |
| 7 | varint | 未知（抓取中值为 5） |
| 8 | 子消息 | **Completion 配置**：包含 `max_tokens`（2: 128000）、`temperature`（5: 1.0 double）、`top_p`（8: 0.95 double）等 |
| 10 | repeated 子消息 | **工具列表**；每条工具有 `name`（1）、`description`（2）、`parameters_json`（3） |
| 15 | 子消息 | 未知；可能是 `query_label` / `hosted_tool_search` / 路由标签；含 `execution_id` 类字符串和数值 |
| 16 | 字符串 | 可能是 `execution_id` / `generation_id` |
| 20 | varint | 未知（值为 1） |
| 21 | 字符串 | **模型名**，如 `swe-1-7` |

#### `GetChatMessage` 响应（流式）

- **Content-Type**：`application/connect+proto`
- **Envelope**：39 个 chunk（`flags=0x00`） + 1 个结束帧（`flags=0x02`，2 字节，可能是 trailers）
- **每个 chunk 顶层字段**：

| 字段 | 类型 | 含义 |
|------|------|------|
| 1 | 字符串 | `message_id`（本次回复 ID，如 `bot-...`） |
| 2 | 子消息 | `timestamp`：`seconds`（1）、`nanos`（2） |
| 3 | 字符串 | **内容片段**（后半段回复文本，与 4/5 一起出现） |
| 4 | varint | 与 field 3 同时出现，可能是 `part_index` / `content_type` |
| 5 | varint | 与 field 3 同时出现，出现一次 `2` |
| 7 | 子消息 | `usage` / `completion_profile`：含 token 计数、耗时、`x-request-id`、实际模型名 `swe-1-7` |
| 9 | 字符串 | **内容片段**（前半段回复文本） |
| 12 | double | `latency`（秒），逐 chunk 递增 |
| 17 | 字符串 | `request_id` |
| 28 | repeated 子消息 | `response_dimension_groups`（仅在最后几个 chunk 出现） |

#### 关键发现

1. **实际 API 主机可能是 `server.codeium.com`**，不是 `api.devin.ai`。`api.devin.ai` 是二进制中另一个候选，由 `devin_api_url` 控制。
2. **认证头是 `Basic devin-session-token$<jwt>`**，不是标准 `Bearer`。JWT payload 里只有一个 `session_id`。
3. **`GetChatMessage` 也返回流式响应**，每个 chunk 是一段文本或 usage 指标，不是一次性 JSON。
4. **二进制中推断的 `InferenceRequest` 字段编号不适用于真正的 API protobuf**。API 请求使用另一个（可能是 `GetChatMessageRequest`）protobuf 结构，其字段编号来自 `.proto` 而非 Rust 结构体顺序。
5. **系统提示（field 2）与消息（field 3）是分开的**：请求中把完整 Devin system prompt 作为独立字段，而不是一条 `role: system` 消息。
6. **工具 schema 是 JSON 字符串**，不是 protobuf 嵌套结构。

---

## 7. MCP（Model Context Protocol）

Devin CLI 作为 MCP client，支持多种传输：

- **stdio**：本地子进程 MCP server
- **SSE**：`text/event-stream`
- **Streamable HTTP**：`connect-protocol-version`
- **WebSocket**：`tokio-tungstenite`

关键头：

```
Content-Type: text/event-stream
Mcp-Session-Id: ...
Last-Event-Id: ...
```

---

## 8. Handoff 协议流程

`/handoff` 把本地 session 迁移到云端，协议流程：

```
1. [handoff] connect_acp: connecting
2. [handoff] connect_acp: initialize sent
3. [handoff] connect_acp: initialize response received
4. [handoff] connect_acp: handshake complete (notifications/initialized)
5. [handoff] resolve_org_id: checking project configs / user config
6. [handoff] acp_session_new: sent session/new
7. [handoff] acp_session_new: session created
8. [handoff] acp_set_repo: sent set_config_option
9. [handoff] acp_send_prompt: prompt sent
10. [handoff] acp_send_prompt: prompt response received
11. [handoff] stream_cloud_session: starting (WebSocket)
12. [handoff] poll_session_finished: polling session status via REST
```

---

## 9. 附录：关键字段清单

### 9.1 推理相关

> 完整的字段编号参见 [6.5 protobuf 字段编号]。

```
InferenceRequest (13)
  1 model, 2 messages, 3 tools, 4 query_label, 5 is_user_initiated,
  6 completion_config, 7 max_trailing_images, 8 execution_id, 9 agent_context,
  10 disable_prompt_cache_writes, 11 system_prefix_len, 12 hosted_tool_search,
  13 generation_id

CompletionConfig (4)
  1 temperature, 2 top_p, 3 top_k, 4 max_tokens

AgentContext (6)
  1 cwd, 2 agent_id, 3 chain_metadata, 4 read_char_limit,
  5 read_only_profile, 6 chain_tool_call

ChatMessageInner (11)
  1 message_id, 2 role, 3 content, 4 images, 5 tool_call_id, 6 tool_calls,
  7 thinking, 8 reasoning_details, 9 metadata, 10 tool_search_result,
  11 hosted_tool_searches

ChatMessageMetadata (13)
  num_tokens, is_user_input, request_id, metrics, finish_reason, extensions,
  committed_credit_cost, committed_acu_cost, started_generation_at, created_at,
  from_event_id, telemetry, loaded_tool_names

ImageData (6)
  1 width, 2 height, 3 base64_data, 4 mime_type, 5 source_path, 6 caption

ThinkingBlock (3)
  1 signature, 2 signature_type, 3 (未知)

ParsedToolCall (7)
  1 id, 2 inference_tool_name, 3 key, 4 arguments, 5 index, 6 kind, 7 namespace

ToolResult (9)
  success, content, display_content, expanded_display_content, failure_reason,
  message_extensions, undo_actions, scope, ...

PerformanceMetrics (8)
  ttft_ms, total_time_ms, input_tokens, output_tokens, cache_read_tokens,
  cache_creation_tokens, tpot_ms, tokens_per_sec
```

### 9.2 Cog / 上下文

```
Cog (12)
  set_system_prefix, append_system_messages, context, footer_messages,
  user_display, tool_availability, permission_mask, soft_deny,
  enable_command_execution, enable_argument_interpolation,
  enable_file_reference_expansion, persistent

CogSource enum
  ManagedSession, UserOverride, ProjectBase, Subagent

CogAction
  InjectContext, SetConfig, key, AddSessionConfig, persist, RemoveConfig,
  DisplayUserMessage
```

### 9.3 生命周期事件 / Hook

```
core/pre_tool
core/post_tool
core/pre_inference
core/post_inference
core/pre_stop
core/post_stop
core/session_start
core/post_agent_iteration
core/compaction
core/permission_request
```

### 9.4 子代理

```
SubagentInput (5)
  title, profile, is_background, resume, task

ReadSubagentInput (3)
  agent_id, ...

SubagentStarted (6)
  agent_id, task, profile, depth, is_background, ...

SubagentCompleted (4)
  success, summary, ...
```

### 9.5 权限

```
PermissionDecision
  Allow, Force, Ask, Deny

PermissionMask (3)
  allow, ask, deny

ToolAvailability
  AllowList, ExtendAllowList, BlockList, SetBase, Available, ExtendAvailable,
  ExtendRestricted

PermissionScope
  Write, Read, Command, Fetch, WriteToProcess
```

---

## 10. 逆向分析说明

### 10.1 已知

- **LLM 推理**由 `server.codeium.com`（或 `api.devin.ai`，取决于 `devin_api_url`）上的 Connect-RPC 服务完成；`GetChatMessage` 实际抓包中也是流式
- 推理请求顶层字段来自真实 `.proto`（如 `GetChatMessageRequest`），与二进制中静态推断的内部 `InferenceRequest` 字段编号不一致
- 工具调用有自定义 `inference_tool_name` / `key` / `namespace` 字段
- **Raindrop（`api.raindrop.ai/v1/`）是可观测性/telemetry SDK**，端点包括 `events/track_partial`、`signals/track`、`traces`，使用 `Authorization: Bearer <write_key>`
- Devin LLM API 认证头是 `Authorization: Basic devin-session-token$<jwt>`；Raindrop 额外带 `X-Raindrop-Sdk` 头

### 10.2 未知 / 已部分确认

| 项目 | 状态 | 说明 |
|------|------|------|
| `GetChatMessage` 请求 / 响应 wire 编号 | 已验证 | 见 6.6，仍需更多场景确认 |
| `GetDevstralStream` 请求 / 响应结构 | 仍未知 | 未触发，可能用于 agent action / 工具调用流 |
| `AssignModel` 请求 / 响应结构 | 仍未知 | 未触发，可能是登录 / 模型切换时调用 |
| `GetDevstralStream` 响应帧格式 | 已确认 | Connect 流式 envelope：`[flags:1][length:4 BE][payload]` |
| `AssignModel` 请求体 | 仍未知 | 只有日志中 `model router` 提示，请求类型名未在二进制字符串中出现 |
| `GetImageCaption` 请求体 | 仍未知 | 响应 `caption` 已知，请求可能含 `image` / `image_url` |
| `hosted_tool_search` 的具体格式 | 仍未知 | `anthropic_variant`, `regex`, `bm25` 子字段已见 |
| `agent_context` 中未完全暴露的字段 | 部分推断 | `chain_tool_call` 等已出现，但类型结构仍不明 |

### 10.3 分析方法

- 工具：`strings`、`grep`、`read`、官方 `.mdx` 文档
- 对象：`~/.local/share/devin/cli/_versions/3000.3.27/bin/devin`（Mach-O ARM64，strip 符号但保留 panic/调试字符串）
- 局限：无法看到实际运行时 HTTP body，只能静态推断 schema

---

## 12. Devin LLM 推理协议 vs OpenAI 接口对应关系

> 本章节基于二进制中暴露的结构体字段和事件名，给出 **Devin CLI 实际 LLM 推理调用**（`GetDevstralStream` / `GetChatMessage`）与 OpenAI `chat.completions` 的映射关系。这些调用的请求体结构是 `InferenceRequest`，与 OpenAI 兼容但不完全相同，有自己的 namespace、响应流和 agent 上下文。Raindrop 仅负责 telemetry 上报，不直接处理推理。

### 12.1 URL 与请求头

| 项目 | Devin CLI 推理 | OpenAI |
|------|----------------|--------|
| Base URL | `https://server.codeium.com` 或 `https://api.devin.ai`（由 `devin_api_url` 配置决定） | `https://api.openai.com/v1/` |
| 推理方法 | `POST /exa.api_server_pb.ApiServerService/GetChatMessage` / `GetDevstralStream`（Connect-RPC） | `POST /v1/chat/completions` |
| 本地调试 | `WINDSURF_API_SERVER_URL` 覆盖 | `http://localhost:11434/v1/` 等 |
| SDK Header | `X-Raindrop-Sdk: raindrop-rust`（telemetry 头，可能附带） | `OpenAI-Beta` / 无 |
| Auth | `Authorization: Basic devin-session-token$<jwt>`（JWT 内含 `session_id`） | `Authorization: Bearer <openai_api_key>` |
| Content-Type | `application/proto` / `application/connect+proto` | `application/json` |

### 12.2 请求体字段映射

> 实际抓包显示 `GetChatMessage` 的顶层请求消息不是内部 `InferenceRequest`（见 6.6），而是一个包含 `system_prompt`（2）、`messages`（3）、`tools`（10）、`completion_config`（8）、`model`（21）等字段的 API protobuf。本节字段名仍作为语义参考，但 wire 编号应以实际抓包为准。

**Devin LLM 推理**的请求体由 `windsurf-api-client` 序列化为 protobuf 后，通过 Connect-RPC 发送到 `GetDevstralStream` / `GetChatMessage`。核心 LLM 参数集中在 `messages`、`tools`、`completion_config`：

```json
{
  "model": "swe-1-6-fast",
  "messages": [ ... ],
  "tools": [ ... ],
  "query_label": "agent_turn",
  "is_user_initiated": true,
  "completion_config": {
    "temperature": 0.7,
    "top_p": 0.9,
    "top_k": 40,
    "max_tokens": 8192
  },
  "max_trailing_images": 4,
  "execution_id": "...",
  "agent_context": { ... },
  "disable_prompt_cache_writes": false,
  "system_prefix_len": 1234,
  "hosted_tool_search": { ... },
  "generation_id": "..."
}
```

映射到 OpenAI `chat.completions`：

| Devin 字段 | OpenAI 字段 | 说明 |
|-----------|------------|------|
| `model` | `model` | 模型名。Devin CLI 使用 `swe`, `opus`, `sonnet`, `codex`, `gemini` 等内部别名，需映射为 OpenAI/兼容端点支持的模型名 |
| `messages` | `messages` | 数组，格式兼容，见 12.3 |
| `tools` | `tools` | 数组，schema 需转换，见 12.4 |
| `completion_config.temperature` | `temperature` | 默认 0.7 |
| `completion_config.top_p` | `top_p` | 默认 0.9 |
| `completion_config.max_tokens` | `max_tokens` | 默认 8192 |
| `completion_config.top_k` | 无 | OpenAI 不直接支持 `top_k`，可丢弃 |
| `stream` | 隐式 `stream: true` | `GetDevstralStream` 为流式，`GetChatMessage` 为非流式；OpenAI 需显式设置 `stream: true` |

**无法直接映射的 Devin 专有字段**：

| 字段 | 处理建议 |
|------|----------|
| `query_label` | 用于 Cognition 内部 telemetry/分类，反代时丢弃 |
| `is_user_initiated` | 标记用户主动输入，反代时可硬编码为 `true` |
| `max_trailing_images` | 限制图片数量，反代时可在代理层做图片 resize/过滤 |
| `execution_id` / `generation_id` | 追踪 ID，丢弃或生成 UUID |
| `agent_context` | 包含 `cwd`, `agent_id`, `read_char_limit` 等，与 LLM 无关，丢弃 |
| `disable_prompt_cache_writes` | 提示缓存开关，反代时丢弃 |
| `system_prefix_len` | 提示前缀 token 数，反代时丢弃 |
| `hosted_tool_search` | 托管工具搜索配置，见 12.5 |

### 12.3 messages 格式映射

Devin LLM 推理使用的 `messages` 与 OpenAI 兼容，但单条消息有更多字段。

**OpenAI 标准：**

```json
{
  "role": "user",
  "content": "hello"
}
```

**Devin `ChatMessageInner`（11 字段）：**

```json
{
  "message_id": "...",
  "role": "user",
  "content": "hello",
  "images": [...],
  "tool_call_id": "...",
  "tool_calls": [...],
  "thinking": "...",
  "reasoning_details": [...],
  "metadata": {...},
  "tool_search_result": {...},
  "hosted_tool_searches": [...]
}
```

**映射规则：**

| Devin 字段 | OpenAI 字段 | 说明 |
|-----------|------------|------|
| `role` | `role` | `user` / `assistant` / `tool` |
| `content` | `content` | 字符串或多模态数组 |
| `images` | `content` 中的 `image_url` | OpenAI 格式：`{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}` |
| `tool_calls` | `tool_calls` | assistant 消息中的 tool call 列表 |
| `tool_call_id` | `tool_call_id` | tool 角色消息中的 id |
| `thinking` | `reasoning_content` 或丢弃 | 部分 OpenAI 模型支持 reasoning |
| `reasoning_details` | 丢弃 | Devin 私有 |
| `metadata` | 丢弃 | 私有 |
| `tool_search_result` / `hosted_tool_searches` | 丢弃 | Devin 私有 |

### 12.4 tools 格式映射

**Devin `ToolDefinition`（6 字段）：**

```json
{
  "name": "read",
  "description": "Read a file",
  "parameters": {
    "type": "object",
    "properties": {
      "file_path": { "type": "string" }
    },
    "required": ["file_path"]
  },
  "custom_tool": false,
  "defer_loading": false,
  "grammar": "...",
  "grammar_syntax": "..."
}
```

**OpenAI Tool：**

```json
{
  "type": "function",
  "function": {
    "name": "read",
    "description": "Read a file",
    "parameters": { ... }
  }
}
```

**映射规则：**

| Devin 字段 | OpenAI 字段 | 说明 |
|-----------|------------|------|
| `name` | `function.name` | 工具名 |
| `description` | `function.description` | 工具描述 |
| `parameters` | `function.parameters` | JSON Schema |
| `custom_tool` | 无 | 标记是否为自定义工具，丢弃 |
| `defer_loading` | 无 | 延迟加载标记，丢弃 |
| `grammar` / `grammar_syntax` | 无 | 输出限制语法，丢弃 |

**Tool namespace 处理：**

Devin 工具有 `namespace` 和 `kind`，例如 `core` 或 `mcp__server__name`。OpenAI 不支持命名空间，通常需要把 `namespace` 和 `name` 拼接：`{namespace}__{name}`，或者只保留 `name`。

### 12.5 响应事件流映射

Devin LLM 推理返回的是**自定义事件流**（由 `GetDevstralStream` 流式响应解析而来），需要在代理层把 OpenAI SSE 转成 Devin 内部事件。

**OpenAI SSE 格式：**

```
data: {"id":"...","object":"chat.completion.chunk","created":1234567890,"model":"gpt-4o","choices":[{"index":0,"delta":{"role":"assistant","content":"He"},"finish_reason":null}]}

data: {"choices":[{"delta":{"content":"llo"}}]}

data: {"choices":[{"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

**Devin 内部事件映射：**

| OpenAI 输出 | Devin 内部事件 | 说明 |
|------------|----------------|------|
| `choices[0].delta.content` 开始产生 | `ContentToken` | 文本 token 流 |
| `choices[0].delta.content` 累积完成 | `ContentComplete` | 内容输出结束 |
| `choices[0].delta.tool_calls[].function.name` | `ToolCallDelta` + 累积为 `ToolRequest` | 工具调用请求 |
| `choices[0].finish_reason` | `stop_reason` | `stop` / `tool_calls` / `max_tokens` |
| 流结束 | `Stopped` | 事件流终止 |

**具体转换示例：**

```
OpenAI:  data: {"choices":[{"delta":{"content":"He"}}]}
         ↓
Devin: ContentToken("He")

OpenAI:  data: {"choices":[{"delta":{"content":"llo"}}]}
         ↓
Devin: ContentToken("llo")

OpenAI:  data: {"choices":[{"delta":{"content":""},"finish_reason":"stop"}]}
         ↓
Devin: ContentComplete + stop_reason="stop"
```

**工具调用转换：**

```
OpenAI:  data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"read"}}]}}]}
         ↓
Devin: ToolCallDelta({"index":0,"inference_tool_name":"read","key":"read","kind":"function","namespace":"core","arguments":""})

OpenAI:  data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"file_path\":\"/etc"}}]}}]}
         ↓
Devin: ToolCallDelta({"index":0,"arguments":"{\"file_path\":\"/etc"})

OpenAI:  data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\"/passwd\"}"}}]}}]}
         ↓
Devin: ToolCallDelta(...)

OpenAI:  data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}
         ↓
Devin: ToolRequest([ParsedToolCall])
```

### 12.6 工具结果回传映射

Devin CLI 执行工具后，会把 `ToolResult` 重新塞回 `messages` 中用于下一次推理。

**OpenAI 格式：**

```json
{
  "role": "tool",
  "tool_call_id": "call_xxx",
  "content": "..."
}
```

**Devin 格式：**

```json
{
  "role": "tool",
  "tool_call_id": "call_xxx",
  "content": "...",
  "metadata": {...},
  "images": [...]
}
```

注意：Devin 的 `ToolResult` 有 `success`, `content`, `display_content`, `expanded_display_content`, `failure_reason` 等 9 个字段，实际回传时可能把 `content` 或 `display_content` 作为 `content` 字段。

### 12.7 图片/多模态映射

Devin `ImageData`：

```json
{
  "width": 1024,
  "height": 768,
  "base64_data": "iVBORw0KGgo...",
  "mime_type": "image/png",
  "source_path": "/path/to/img.png",
  "caption": "..."
}
```

OpenAI 多模态消息：

```json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "describe this image"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo..."}}
  ]
}
```

映射：
- `base64_data` + `mime_type` → `image_url.url = data:{mime_type};base64,{base64_data}`
- `max_trailing_images` → 代理层限制消息中图片数量
- `source_path` / `caption` → 丢弃或作为消息上下文

### 12.8 特有字段 `hosted_tool_search`

```json
{
  "anthropic_variant": "...",
  "regex": true,
  "bm25": true
}
```

这是 Cognition 的托管工具搜索配置，用于让模型在调用外部工具前先检索。OpenAI 没有等价机制，反代时通常：
- 丢弃该字段
- 或预先把工具搜索结果作为 system/user message 注入

### 12.9 stop_reason / finish_reason 映射

| Devin `stop_reason` | OpenAI `finish_reason` | 说明 |
|------------------------|------------------------|------|
| `Complete` | `stop` | 自然完成 |
| `message` | `stop` | 生成消息结束 |
| `Restart` | 无 | 模型要求重启/重试 |
| `Cancelled` | 无 | 用户取消 |
| `Interrupted` | 无 | 被中断 |
| `Error` | 无 | 推理错误 |
| `ToolRejected` | 无 | 工具被拒绝 |
| `AuthRequired` | 无 | 需要认证 |
| `Shutdown` | 无 | 关闭 |
| `MaxTurnRequests` | `length` | 达到最大请求数 |
| `OutputTruncated` | `content_filter` / `length` | 输出被截断 |

### 12.10 完整 OpenAI 转 Devin 推理代理逻辑（概念）

```python
async def devin_to_openai(devin_request: dict) -> dict:
    completion_config = devin_request.get("completion_config", {})
    tools = [convert_tool(t) for t in devin_request.get("tools", [])]
    messages = [convert_message(m) for m in devin_request.get("messages", [])]
    # 过滤图片数量
    messages = limit_images(messages, devin_request.get("max_trailing_images"))

    return {
        "model": map_model(devin_request["model"]),
        "messages": messages,
        "tools": tools,
        "temperature": completion_config.get("temperature", 0.7),
        "top_p": completion_config.get("top_p", 0.9),
        "max_tokens": completion_config.get("max_tokens", 8192),
        "stream": True,
    }

async def openai_to_devin(openai_stream):
    async for chunk in openai_stream:
        delta = chunk.choices[0].delta

        if delta.content:
            yield {"type": "ContentToken", "content": delta.content}

        if delta.tool_calls:
            for tc in delta.tool_calls:
                yield {"type": "ToolCallDelta", "tool_call": tc.model_dump()}

        finish = chunk.choices[0].finish_reason
        if finish:
            if finish == "tool_calls":
                yield {"type": "ToolRequest", "tool_calls": accumulated_tool_calls}
            yield {"type": "ContentComplete", "stop_reason": map_finish_reason(finish)}
            yield {"type": "Stopped"}
```

### 12.11 关键差异总结

| 差异点 | Devin 推理 | OpenAI |
|--------|----------|--------|
| 默认流式 | `GetDevstralStream` 默认流式 | 需显式 `stream: true` |
| Tool 命名空间 | `namespace` + `name` + `kind` | 仅 `name` |
| Tool schema 扩展 | `grammar`, `grammar_syntax` | 不支持 |
| Agent 上下文 | `agent_context` | 无 |
| 图片限制 | `max_trailing_images` | 无，需代理控制 |
| 工具搜索 | `hosted_tool_search` | 无 |
| 思考内容 | `thinking`, `reasoning_details` | `reasoning_content`（仅部分模型） |
| 事件类型 | 自定义事件（ContentToken 等） | 标准 SSE |
| Telemetry | `PerformanceMetrics`, `gen_ai.*` | 无内嵌，需 usage 计算 |
| 认证 header | `Authorization: Bearer <devin_token>` | `Authorization: Bearer <openai_api_key>` |

---

## 13. Devin LLM 推理架构深入分析

### 13.1 协议栈：CLI 内部通过 Devin API 完成推理

从二进制中的源码路径可以发现，Devin CLI 内部存在多层推理抽象，真正的在线 LLM 调用走 `windsurf-api-client` → `api.devin.ai` 的 Connect-RPC，而不是 `api.raindrop.ai`：

```
chisel (REPL/UI)
  │
  ▼
affogato/src/agent/control_loop.rs  <- agent loop
  │
  ▼
affogato/src/agent/effects/inference.rs  <- 推理效果
  │
  ▼
inference/src/request.rs            <- 统一请求构造（InferenceRequest）
inference/src/backend.rs            <- 后端抽象
inference/src/compat.rs             <- 兼容层
inference/src/stream.rs             <- 统一流处理
inference/src/retry.rs              <- 重试逻辑
  │
  ▼
windsurf-api-client/src/inference_client.rs
windsurf-api-client/src/backend.rs
  │
  ▼
POST https://api.devin.ai/exa.api_server_pb.ApiServerService/GetDevstralStream
  (Connect-RPC, application/proto or application/connect+proto)
```

旁路（offline / 本地调试 / 测试）：

```
inference/src/request.rs
  │
  ▼
OpenAI / Anthropic / Google / xAI 等 provider
```

这说明 **Devin API（`api.devin.ai`）是默认路由，但 CLI 的 `inference` crate 也支持直连各 provider**。`inference/src/compat.rs` 专门把各 provider 的响应统一为内部事件流。

### 13.2 `inference` crate 的职责

| 文件 | 职责 |
|------|------|
| `inference/src/request.rs` | 构造 LLM 请求（model, messages, tools, config） |
| `inference/src/backend.rs` | 后端路由：Devin API (Connect-RPC) vs OpenAI vs Anthropic |
| `inference/src/compat.rs` | 把不同 provider 的响应格式统一为内部事件 |
| `inference/src/stream.rs` | 统一流式输出、TTFT 统计、Sentry trace |
| `inference/src/retry.rs` | 重试策略（指数退避、quota 耗尽、payload 过大） |

`inference/src/stream.rs` 中的关键日志字符串：

```
First token received
chat.completion
time_to_first_token
Waiting for first token
Chat completion stream finished
Chat completion stream dropped
```

这说明 **CLI 内部把任何底层推理流（包括 `GetDevstralStream` 的 Connect 流式响应）都抽象为 `chat.completion` 事件**，并统计 `time_to_first_token`（TTFT）。`GetDevstralStream` 返回的 `delta_text`、`tool_calls`、`delta_thinking` 等字段在这里被转换为统一的 `chat.completion` token 流。

### 13.3 模型配置架构

二进制中暴露了完整的模型配置结构：

**Provider 专用配置：**

```rust
struct OpenAiInferenceConfig {
    reasoning_effort,
    extended_prompt_cache_retention,
    reasoning_context,
    service_tier,
}

struct AnthropicInferenceConfig {
    thinking,
    fast_mode,
    context_1m,
    effort,
}

struct GoogleInferenceConfig { ... }
struct XaiInferenceConfig { ... }
struct ZaiInferenceConfig { ... }
struct ThinkingMachinesInferenceConfig { ... }
```

**统一配置：**

```rust
struct InferenceConfig {
    config,              // 上面某个 provider 专用配置
    model_info,          // ModelInfo
    model_features,      // ModelFeatures
    completion_profile,  // CompletionProfile
}
```

**ModelInfo / ModelFeatures 关键字段：**

```rust
struct ModelInfo {
    model_type,
    model_profile,
    supports_images,
    supports_legacy,
    is_premium,
    max_tokens,
    api_provider,
    model_family_metadata,
    is_default_model_in_family,
    disabled_reason,
}

struct ModelFeatures {
    supports_tool_calls,
    supports_thinking,
    supports_image_captions,
    supports_parallel_tool_calls,
    supports_context_tokens,
    supports_cumulative_context,
    requires_fim_context,
    requires_llama3_tokens,
    requires_instruct_tags,
    interleave_thinking,
    preserve_thinking,
    supports_rejection_context,
    // ... 更多
}
```

**CompletionProfile 性能指标：**

```rust
struct CompletionProfile {
    model_profile,
    draft_model_profile,
    time_to_first_prefill_pass,
    time_to_first_token,
    total_completion_time,
    total_model_time,
    model_usage,
    num_prefill_passes,
    total_prefill_pass_time,
    avg_prefill_pass_time,
    num_generation_passes,
    total_generation_pass_time,
    avg_generation_pass_time,
    num_spec_copy_passes,
    total_spec_copy_pass_time,
    avg_spec_copy_pass_time,
}
```

这些字段显示 Devin 后端（`GetDevstralStream` 服务）支持**推测解码（speculative decoding / draft model）**、多 pass 生成、prefill 优化等高级功能。

### 13.4 事件流协议格式推断

二进制中暴露的 Connect-RPC 和 `GetDevstralStream` 相关字符串显示：

| 线索 | 推断 |
|------|------|
| `application/proto` / `application/connect+proto` | 请求/响应使用 protobuf 序列化 |
| `connect-protocol-version` | Connect-RPC 协议版本头 |
| `Connect POST content-type1connect-protocol-version` | 实际 POST 到 `https://api.devin.ai/exa.*Service/*` |
| `GetDevstralStream` / `GetChatMessage` | 流式/非流式推理 RPC 方法 |
| `Collecting streaming response` | 流式响应由 Connect  envelope 封装 |
| `server sent compressed envelope, but compression is not supported` | 响应 envelope 可能被压缩 |
| `HTTP body stream error while reading Connect response` | 使用 HTTP body 流读取 Connect 响应 |
| `unexpected input buffer end while decoding` | protobuf 解码时需要完整 buffer |
| `size overflows MAX_SIZE` | 单个 protobuf message 大小上限 |

**最可能的格式：**

1. **Connect 流式 protobuf**：`GetDevstralStream` 返回 `application/connect+proto` 流式 envelope，每个 envelope 包含一个 `GetDevstralStreamResponse` 消息（`delta_text`, `tool_calls`, `stop_reason` 等字段）。
2. **非流式 protobuf**：`GetChatMessage` 返回单个 `GetChatMessageResponse` protobuf 消息。
3. **内部统一事件流**：`inference/src/stream.rs` 把 Connect 流解析为 `ContentToken`, `ToolCallDelta`, `ToolRequest`, `ContentComplete` 等内部事件。

**Connect 流式 Envelope 帧格式**

根据 Connect-RPC v1 规范与二进制中 `connectrpc_axum_core::envelope` / `foundation_connectrpc::shared::envelope` 的实现，`GetDevstralStream` 的流式响应格式为：

```
[flags: 1 byte][length: 4 bytes BE][payload: length bytes]
```

- `flags`：
  - `0x00`：普通消息（未压缩）
  - `0x01`：消息经过压缩（对应 `Connect-Content-Encoding` 指定的算法）
  - `0x02`：End-of-Stream（可能带 trailers）
- `length`：`payload` 的字节数，大端无符号 32 位整数
- `payload`：一个 protobuf 消息（例如 `GetChatMessageResponse` 或 `GetDevstralStreamResponse`），需使用 `application/proto` 解码

**读流程：**

1. 读取 5 字节头 → 解析 `flags` 与 `length`
2. 读取 `length` 字节 payload
3. 用对应 response 的 protobuf 解码
4. 当 `flags == 0x02` 且 `length == 0` 时，流结束；若 `payload` 非空，则可能携带错误/trailers

**非流式 `GetChatMessage` / `GetImageCaption` / `AssignModel`：**

- 请求 `Content-Type: application/proto`
- 请求体是单个 protobuf 消息（没有 5 字节 envelope）
- 响应也是单个 protobuf 消息，直接放在 HTTP body 中

**最不可能：**

- 裸 OpenAI SSE（因为 `GetDevstralStream` 的响应是 protobuf，且 CLI 内部解析为自定义事件类型）

### 13.5 如何验证 LLM 推理流量

唯一可靠方法是运行时抓包：

```bash
# 1. 使用 mitmproxy 截获 https://api.devin.ai 的 Connect-RPC 流量
#    注意需要信任 mitmproxy 根证书，且 Devin CLI 使用 rustls
# 2. 启动 Devin CLI 并提问
# 3. 观察请求/响应的 Content-Type 和 body

# 如果要截获 Raindrop telemetry（可选）
RAINDROP_WORKSHOP=1 devin
# 监听 http://127.0.0.1:5899/v1/ 的 events/track_partial 流量
```

### 13.6 ToolCallDelta 累积状态机

Devin LLM 的工具调用和 OpenAI 一样，是**增量流式返回**的。需要按 `index` 累积。

**OpenAI SSE 增量 chunk：**

```
data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_123","type":"function","function":{"name":"read"}}]}}]}

data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"file"}}]}}]}

data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"_path\":\""}}]}}]}

data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"/etc/passwd\"}"}}]}}]}
```

**Devin 内部累积状态：**

```rust
struct ToolCallAccumulator {
    index: usize,
    id: Option<String>,
    inference_tool_name: Option<String>,
    key: Option<String>,
    arguments: String,          // 累积的 JSON 字符串
    kind: Option<String>,       // "function" | "custom"
    namespace: Option<String>,  // "core" | "mcp__..."
}
```

**状态转换：**

```
收到 ToolCallDelta(index=0, id=call_123, inference_tool_name=read, key=read, kind=function, namespace=core, arguments="")
  → 创建 accumulator[0]

收到 ToolCallDelta(index=0, arguments="{\"file")
  → accumulator[0].arguments += "{\"file"

收到 ToolCallDelta(index=0, arguments="_path\":\"/etc/passwd\"}")
  → accumulator[0].arguments += "_path\":\"/etc/passwd\"}"

当 finish_reason == "tool_calls" 或所有 token 接收完毕
  → 把 accumulator[0] 转成 ParsedToolCall
  → 触发 ToolRequest([ParsedToolCall])
```

**namespace 处理：**

- `core`：内置工具，如 `read`, `edit`, `exec`
- `mcp__<server>__<tool>`：MCP 工具
- 拼接规则：`{namespace}__{name}` 作为完整工具名

**`arguments` 字段拼接：**

- OpenAI `function.arguments` 是增量 JSON 字符串片段
- Devin `ParsedToolCall.arguments` 是完整 JSON 字符串
- 转换时需要按 `index` 累积并 JSON parse 验证

### 13.7 推理错误类型

二进制中 `inference/src/retry.rs` 暴露的错误分类：

```rust
enum InferenceError {
    QuotaExhausted,
    ServerError,
    ClientError,
    ContextTooLong,
    PayloadTooLarge,
    Disconnected,
    MalformedResponse,
    AuthFlowError,
    Refusal,
    Timeout,
    PermissionDenied,
    ConnectionFailed,
    RateLimited,
    Unauthenticated,
}
```

重试策略：

| 错误 | 处理 |
|------|------|
| `QuotaExhausted` | 停止并报错 |
| `RateLimited` | 按 `Retry-After` 等待重试 |
| `ContextTooLong` | 触发 compaction（上下文压缩） |
| `PayloadTooLarge` | 减少图片数量 / 截断消息重试 |
| `ServerError` / `ConnectionFailed` / `Disconnected` | 指数退避重试 |
| `MalformedResponse` | 重试或停止 |
| `Refusal` | 返回拒绝原因 |
| `Timeout` | 重试 |

### 13.8 模型名内部枚举（部分）

二进制中暴露了完整的内部模型名到 provider 模型名的映射：

**OpenAI 系列：**

```
MODEL_CHAT_GPT_4
MODEL_CHAT_GPT_4O_2024_05_13
MODEL_CHAT_GPT_4O_2024_08_06
MODEL_CHAT_GPT_4O_MINI_2024_07_18
MODEL_CHAT_GPT_4_1_2025_04_14
MODEL_CHAT_GPT_4_1_MINI_2025_04_14
MODEL_CHAT_GPT_4_1_NANO_2025_04_14
MODEL_CHAT_O1_PREVIEW
MODEL_CHAT_O1_MINI
MODEL_CHAT_O1
MODEL_CHAT_O3_MINI
MODEL_CHAT_O3_MINI_LOW
MODEL_CHAT_O3_MINI_HIGH
MODEL_CHAT_O3
MODEL_CHAT_O3_LOW
MODEL_CHAT_O3_HIGH
MODEL_CHAT_O4_MINI
MODEL_CHAT_O4_MINI_LOW
MODEL_CHAT_O4_MINI_HIGH
MODEL_CHAT_GPT_4_5
MODEL_CODEX_MINI_LATEST
MODEL_CODEX_MINI_LATEST_LOW
MODEL_CODEX_MINI_LATEST_HIGH
MODEL_GPT_5_NANO
MODEL_CHAT_GPT_5
```

**其他 provider：**

```
claude-haiku-4-5
yeti-june12-high
yeti2-june15-low
yeti2-june15-max
yeti3-june29-low
yeti3-june29-max
hestia2, hestia3
chiron
```

**Gemini 系列：**

```
gemini-2.5-flash
gemini-2.5-flash-lite
gemini-3-flash-preview
gemini-3.1-flash-lite-preview
gemini-3.1-pro-preview
gemini-3.5-flash
```

**内部模型 ID：**

```
MODEL_UNSPECIFIED
MODEL_8341, MODEL_8528, MODEL_9024, MODEL_14602, ...
MODEL_QUERY_9905, MODEL_CHAT_11120, ...
MODEL_DEEPSEEK_V3_INTERNAL
MODEL_DEEPSEEK_R1_INTERNAL
MODEL_DRAFT_11408, MODEL_DRAFT_CHAT_11883
MODEL_CASCADE_22893, ...
```

这些 ID 说明 Devin 后端有大量自有/实验模型（如 `yeti`, `hestia`, `chiron`, `cascade`），并不只是 OpenAI/Anthropic/Gemini 的代理。

### 13.9 反代 Devin LLM 推理接口的额外难点

基于以上分析，反代 `GetDevstralStream` / `GetChatMessage` 比之前估计的更复杂：

1. **CLI 内部有统一的 `inference` crate**：它不是简单把 OpenAI 响应喂给 Devin CLI，而是期望统一的 `chat.completion` token 流 + `ToolCallDelta` 事件。
2. **工具 namespace**：需要把 `{namespace}__{name}` 映射回 Devin 的 `inference_tool_name` / `key`。
3. **图片限制**：`max_trailing_images` 超出会触发 HTTP 413，代理需主动 resize。
4. **推测解码和多 pass**：某些模型使用 draft model 和多 pass，响应可能不是简单 token 流。
5. **reasoning / thinking**：部分模型输出 `thinking` 块，需要映射到 `thinking` 字段。
6. **错误处理**：需要把 OpenAI 的 `context_length_exceeded` 映射为 Devin 的 `ContextTooLong`，触发 compaction。

---
