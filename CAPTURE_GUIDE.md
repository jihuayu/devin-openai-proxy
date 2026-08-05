# Devin CLI Connect-RPC 抓包与解码指南

## 准备

```bash
brew install mitmproxy protobuf
```

## 1. 安装 mitmproxy CA 到系统信任库

Devin CLI 用 `rustls-platform-verifier`，会读取 macOS 系统/用户证书库。mitmproxy 第一次启动后会在 `~/.mitmproxy/` 生成 CA：

```bash
# 第一次启动，生成证书后按 Ctrl-C 退出
mitmdump -p 8080 -q

# 确认证书已生成
ls ~/.mitmproxy/mitmproxy-ca-cert.pem
```

如果加入 `System.keychain` 报 `Error reading file`，可能是权限或路径问题，改用**用户登录钥匙串**即可（rustls 平台验证器同样信任）：

```bash
security add-trusted-cert -r trustRoot \
  -k ~/Library/Keychains/login.keychain-db \
  ~/.mitmproxy/mitmproxy-ca-cert.pem
```

如果仍想放系统钥匙串，可尝试复制到 `/tmp` 后再执行（注意 `com.apple.provenance` 扩展属性不影响读取，但若遇到未知错误可作测试）：

```bash
cp ~/.mitmproxy/mitmproxy-ca-cert.pem /tmp/mitm-ca.pem
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain \
  /tmp/mitm-ca.pem
```

## 2. 启动抓包

```bash
mitmdump -s connect_capture.py -p 8080
```

## 3. 运行 Devin CLI

```bash
HTTPS_PROXY=http://127.0.0.1:8080 \
HTTP_PROXY=http://127.0.0.1:8080 \
  devin
```

在 REPL 里随便发一条消息，CLI 会调用：

- `POST https://server.codeium.com/exa.seat_management_pb.SeatManagementService/GetUserStatus`（license/座位检查）
- `POST https://server.codeium.com/exa.api_server_pb.ApiServerService/GetAccountManagedPlugins`（插件列表）
- `POST https://server.codeium.com/exa.api_server_pb.ApiServerService/GetChatMessage`（聊天补全，**流式**）
- `POST https://server.codeium.com/exa.product_analytics_pb.ProductAnalyticsService/BatchRecordAnalyticsEvents`（埋点）

如果触发图片/工具调用，还可能出现 `GetImageCaption`、`GetDevstralStream`。所有请求/响应体会被 `connect_capture.py` 保存到 `mitm_dump/`。

## 4. 解码 protobuf

### 流式 / Connect envelope 格式

实测 `GetChatMessage` 也使用 `Content-Type: application/connect+proto`，所以**所有请求/响应都可能需要先解 envelope**：

```bash
# 解 GetChatMessage 请求（单个 envelope）
python3 decode_connect_stream.py --connect \
  mitm_dump/XXX_exa_api_server_pb_ApiServerService_GetChatMessage_req.bin

# 解 GetChatMessage 响应（多个 chunk）
python3 decode_connect_stream.py --connect \
  mitm_dump/XXX_exa_api_server_pb_ApiServerService_GetChatMessage_resp.bin
```

脚本会把 envelope 拆成 `chunk_0000_flags_00.bin`、`chunk_0001_flags_00.bin`... 然后对每个 chunk 调用 `protoc --decode_raw`。

### 直接看某个 chunk

```bash
protoc --decode_raw < mitm_dump/XXX_GetChatMessage_resp/chunk_0000_flags_00.bin
```

## 5. 验证字段编号

`protoc --decode_raw` 输出类似：

```
1: "devin-cli"
2: "You are Devin, ..."
3 {
  1: "msg-..."
  2: 1
  3: "hello"
}
...
```

左边数字就是 wire field number，应参考 `devin-cli-protocol-analysis.md` 的 [6.6 实际抓包结果](../devin-cli-protocol-analysis.md#66-实际抓包结果)。

## 已知限制

- `AssignModel` / `GetImageCaption` / `GetDevstralStream` 在本次抓包中未出现，需要更多场景触发。
- `GetChatMessage` 也是流式响应，chunk 中 `field 3` / `field 9` 都承载文本，具体语义（`content` / `reasoning` / `thinking`）需进一步验证。
- 真实 protobuf 字段编号与静态二进制中的结构体顺序不同，所有 wire 编号应以抓包结果为准。
- 如果 Devin CLI 有自定义证书 pinning（目前未发现相关字符串），则上述 TLS 拦截会失败，需要改用 `WINDSURF_API_SERVER_URL` 指向本地反向代理。

## 隐私提醒

`mitm_dump/` 里包含你的 `session_token`、系统提示、聊天记录等敏感信息，**不要上传到公共仓库或分享给别人**。
