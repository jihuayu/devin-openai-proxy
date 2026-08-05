# Devin API ↔ OpenAI 反代

一个 **OpenAI 客户端 → Devin LLM 推理后端** 的反向代理。

根据对 Devin CLI 实际流量（mitmproxy）的抓包分析：
- Devin CLI 真正的 LLM 推理走 `https://server.codeium.com` 上的 **Connect-RPC + protobuf**。
- 推理入口当前为 `/exa.api_server_pb.ApiServerService/GetChatMessage`（会被流式返回）。
- 认证头格式为 `Authorization: Basic <windsurf_api_key>-<windsurf_api_key>`。
- `api.raindrop.ai` 仅用于 telemetry，不直接处理推理。

本代理把 OpenAI 的 `/v1/chat/completions` 请求转换为 Devin 的 protobuf 请求体，通过 Connect 帧发送，并把返回的流式 protobuf 帧还原成 OpenAI SSE / JSON。

## 主要特性

- OpenAI `chat.completions` ↔ Devin protobuf `GetChatMessage`
- 支持流式 (`stream=true`) 与非流式 (`stream=false`)
- 自动解析 Connect-RPC `[flags:1][length:4 BE][payload]` 帧
- 文本消息映射，支持 system / user / assistant / tool 角色
- `tools` / `tool_calls` / `tool` 角色映射到 Devin protobuf，支持多轮 function calling
- `image_url`（含 base64 data URL）会编码进 `ImageData` 并嵌入到消息内容；选择支持视觉的模型（如 `claude-*`、`kimi-*`、`glm-5-2`）可识别图片
- 可选自动 `web_search`：启用 `AUTO_WEB_SEARCH=true` 后，非流式请求中模型若调用 `web_search`，代理会调用 Devin 后端的 `GetWebSearchResults` 并把结果自动带回继续对话
- 可选 `DEVIN_CONTEXT=true`：注入 CLI 抓到的完整系统提示、`system_info` / `rules` / `available_skills` 上下文消息和 25 个内置工具定义，让请求尽量与真实 Devin CLI 一致
- `tools` 字段映射到 Devin `ToolDefinition`（名称 / 描述 / 参数 schema）
- 自动读取本地 Devin CLI 凭证（`~/.local/share/devin/credentials.toml`）
- 将 Devin 流中的 `reasoning`（field 9）和 `output`（field 3）分别映射为 `reasoning_content` 和 `content`
- 最终 `usage` 映射到 OpenAI `usage`

## 快速开始

```bash
# 1. 创建并激活虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 复制 .env.example 为 .env 并编辑（可选，通常默认即可）

# 4. 启动
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

## 配置

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `DEVIN_BASE_URL` | 从 `credentials.toml` 读取，默认 `https://server.codeium.com` | Devin / Codeium 推理 Base URL |
| `DEVIN_STREAM_PATH` | `/exa.api_server_pb.ApiServerService/GetChatMessage` | 流式 / 非流式都走该 RPC |
| `DEVIN_UNARY_PATH` | `/exa.api_server_pb.ApiServerService/GetChatMessage` | 非流式路径（同上） |
| `DEVIN_TOKEN` | 从凭证文件读取 | `windsurf_api_key` |
| `DEVIN_CONTENT_TYPE` | `application/connect+proto` | Connect 内容类型 |
| `DEVIN_SDK` | `raindrop-rust` | `X-Raindrop-Sdk` 头 |
| `DEFAULT_DEVIN_MODEL` | `swe-1-7` | 无法识别模型名时的默认值 |
| `DEVIN_MODEL_MAP` | `{}` | JSON 模型映射 |
| `DEVIN_TOP_K` | `40` | 默认 `top_k` |
| `FETCH_IMAGE_URLS` | `true` | 是否把远程 `image_url` 下载并 base64 编码后发给模型 |
| `AUTO_WEB_SEARCH` | `false` | 是否自动执行模型发起的 `web_search` 工具调用（非流式） |
| `WEB_SEARCH_NUM_RESULTS` | `5` | 每次 `web_search` 返回的条数 |
| `WEB_SEARCH_MAX_ROUNDS` | `5` | 自动 web_search 最大调用轮数 |
| `DEVIN_CONTEXT` | `false` | 是否注入 Devin CLI 风格的系统提示、上下文消息和内置工具 |
| `DEVIN_CONTEXT_DIR` | `./devin_context` | 系统提示 / 工具定义存放目录 |
| `DEVIN_TEMPERATURE` | `1.0` | 默认 `temperature` |
| `DEVIN_TOP_P` | `0.95` | 默认 `top_p` |
| `DEVIN_MAX_TOKENS` | `128000` | 默认 `max_tokens` |
| `PROXY_TIMEOUT` | `300` | 后端请求超时 |
| `PORT` | `8000` | 监听端口 |

## 使用示例

```python
from openai import OpenAI

client = OpenAI(
    api_key="your_windsurf_api_key",
    base_url="http://localhost:8000/v1",
)

for chunk in client.chat.completions.create(
    model="swe-1-7",
    messages=[{"role": "user", "content": "Hello"}],
    stream=True,
):
    print(chunk.choices[0].delta.content or "", end="")
```

curl 测试：

```bash
DEVIN_TOKEN=$(sed -n 's/^windsurf_api_key = "\(.*\)"/\1/p' ~/.local/share/devin/credentials.toml)
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $DEVIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"swe-1-7","messages":[{"role":"user","content":"hello"}],"stream":true}'
```

启用自动 `web_search`（非流式）后可直接问实时问题：

```bash
AUTO_WEB_SEARCH=true python -m uvicorn app:app --host 127.0.0.1 --port 8000

DEVIN_TOKEN=$(sed -n 's/^windsurf_api_key = "\(.*\)"/\1/p' ~/.local/share/devin/credentials.toml)
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $DEVIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"swe-1-7","messages":[{"role":"user","content":"current weather in Beijing"}],"stream":false}'
```

开启 `DEVIN_CONTEXT` 模拟完整 Devin CLI 上下文：

```bash
DEVIN_CONTEXT=true AUTO_WEB_SEARCH=true python -m uvicorn app:app --host 127.0.0.1 --port 8000

DEVIN_TOKEN=$(sed -n 's/^windsurf_api_key = "\(.*\)"/\1/p' ~/.local/share/devin/credentials.toml)
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $DEVIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"swe-1-7","messages":[{"role":"user","content":"read /etc/hosts"}],"stream":false}'
```

## 协议映射速查

| OpenAI | Devin protobuf 请求 |
|--------|----------------------|
| `model` | field 21 | 通过 `DEVIN_MODEL_MAP` 映射 |
| `system` 消息 | field 2 `system_prompt` | 多段 system 消息会拼接 |
| `user/assistant/tool` 消息 | field 3 `messages` | role: user=1, assistant=2, tool=4 |
| `temperature` | `completion_config` field 5 | - |
| `top_p` | `completion_config` field 8 | - |
| `max_tokens` | `completion_config` field 2 | - |
| `top_k` | `completion_config` field 7 | 默认 40 |
| `tools` | field 10 | `ToolDefinition` 转换 |

响应映射：

| Devin 响应字段 | OpenAI 输出 |
|---------------|-------------|
| field 9 文本片段 | `choices[0].delta.reasoning_content` |
| field 3 文本片段 | `choices[0].delta.content` |
| field 6 子消息 | `choices[0].delta.tool_calls`（function calling） |
| field 5 varint | `finish_reason`：`2`=stop，`10`=tool_calls |
| field 7 usage 子消息 | 最终 `usage` chunk / JSON |
| end-of-stream 帧 | `[DONE]` |

## 调试

- `GET /health`
- `GET /v1/models`

## 本地测试 / 抓包

`connect_capture.py` 是一个 `mitmdump` 脚本，可用于捕获真实 CLI 流量：

```bash
mitmdump -s connect_capture.py -p 8082
HTTPS_PROXY=http://127.0.0.1:8082 HTTP_PROXY=http://127.0.0.1:8082 devin -p "hello"
```

捕获的请求体保存在 `mitm_dump/`。

## 已知局限

- 当前实现基于 protobuf **wire 编号** 手动编解码，没有完整 `.proto` schema；若后端升级字段编号可能需要调整。
- 图片识别依赖后端模型是否支持视觉输入；`swe-1-7` 等代码模型会忽略图片，建议选择 `claude-*`、`kimi-*`、`glm-5-2` 等模型。
- 自动 `web_search` 仅在非流式请求中生效，且受 `WEB_SEARCH_MAX_ROUNDS` 限制；模型可能在多轮搜索后仍不收敛。
- `DEVIN_CONTEXT=true` 会注入较大的系统提示、上下文消息和 25 个内置工具，显著增加 token 消耗；非 Devin 场景建议关闭。
- 注入的 `available_skills` / `tools` 是静态抓取文件，不会随用户本地的技能目录动态变化；如需更新，重新抓取或手动编辑 `devin_context/`。
- `read` / `edit` / `grep` / `exec` 等内置工具目前只返回 `tool_calls`，由客户端执行；代理仅自动执行 `web_search`。
- 依赖本地 `windsurf_api_key` 的推理配额；无配额时会收到 `resource_exhausted` 等错误。
