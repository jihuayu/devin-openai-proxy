"""OpenAI <-> Devin LLM inference protocol transformers.

Updated for the real protobuf wire format captured from
``server.codeium.com/exa.api_server_pb.ApiServerService/GetChatMessage``.
"""

import asyncio
import base64
import json
import mimetypes
import os
import re
import secrets
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx

import config
import proto


def _now() -> int:
    return int(time.time())


def _uuid() -> str:
    return str(uuid.uuid4())


# -----------------------------------------------------------------------------
# Protobuf wire schemas (derived from real CLI traffic)
# -----------------------------------------------------------------------------

CLIENT_METADATA_SCHEMA = {1: 2, 2: 2, 3: 2, 4: 2, 5: 2, 7: 2, 12: 2, 28: 2, 31: 2}

# Fields observed in ChatMessageInner request submessages:
#   1 message_id, 2 role, 3 content, 4 images (repeated ImageData),
#   5 tool_call_id (assistant tool-call id when needed),
#   6 tool_calls (repeated), 7 tool_call_id (tool-result),
#   11 thinking / reasoning text.
MESSAGE_SCHEMA = {1: 2, 2: 0, 3: 2, 4: (2, True), 5: 2, 6: (2, True), 7: 2, 11: 2}

# ImageData (width, height, base64_data, mime_type, source_path, caption)
IMAGE_SCHEMA = {1: 0, 2: 0, 3: 2, 4: 2, 5: 2, 6: 2}

COMPLETION_CONFIG_SCHEMA = {1: 0, 2: 0, 3: 0, 5: 1, 7: 0, 8: 1}
TOOL_SCHEMA = {1: 2, 2: 2, 3: 2}
REQUEST_SCHEMA = {
    1: 2,
    2: 2,
    3: (2, True),
    7: 0,
    8: 2,
    10: (2, True),
    15: 2,
    16: 2,
    20: 0,
    21: 2,
}


# -----------------------------------------------------------------------------
# Stop reason mapping (field 5 in response frames)
# -----------------------------------------------------------------------------

# Observed values:
#   2 -> natural completion (stop)
#   10 -> tool call
_FINISH_REASON = {2: "stop", 10: "tool_calls"}


# -----------------------------------------------------------------------------
# Request builders
# -----------------------------------------------------------------------------

def _build_client_metadata(token: str) -> bytes:
    return proto.encode_message(
        CLIENT_METADATA_SCHEMA,
        {
            1: "devin-cli",
            2: "3000.3.27",
            3: token,
            4: "en",
            5: "darwin",
            7: "3000.3.27",
            12: "chisel",
            28: "chisel",
            31: secrets.token_hex(366),
        },
    )


def _build_image(
    base64_data: str,
    mime_type: str,
    width: int = 0,
    height: int = 0,
    source_path: str = "",
    caption: str = "",
) -> bytes:
    return proto.encode_message(
        IMAGE_SCHEMA,
        {
            1: width,
            2: height,
            3: base64_data,
            4: mime_type,
            5: source_path,
            6: caption,
        },
    )


def _build_tool_call_submessage(
    call_id: str,
    name: str,
    arguments: str,
) -> bytes:
    return proto.encode_message(
        {1: 2, 2: 2, 3: 2},
        {1: call_id, 2: name, 3: arguments},
    )


def _build_message(
    role: int,
    content: str,
    msg_id: str,
    images: Optional[List[bytes]] = None,
    tool_calls: Optional[List[bytes]] = None,
    tool_call_id: str = "",
    thinking: str = "",
) -> bytes:
    values: Dict[int, Any] = {1: msg_id, 2: role}
    if content:
        values[3] = content
    if images:
        values[4] = images
    if tool_calls:
        values[6] = tool_calls
    if tool_call_id:
        values[7] = tool_call_id
    if thinking:
        values[11] = thinking
    return proto.encode_message(MESSAGE_SCHEMA, values)


def _build_completion_config(
    max_tokens: int,
    temperature: float,
    top_p: float,
    top_k: int,
) -> bytes:
    return proto.encode_message(
        COMPLETION_CONFIG_SCHEMA,
        {
            1: 1,  # captured traffic always has 1 (backend-side streaming)
            2: max_tokens,
            3: 400,
            5: temperature,
            7: top_k,
            8: top_p,
        },
    )


def _load_devin_context(name: str) -> str:
    """Load a static context fragment from ``DEVIN_CONTEXT_DIR`` if it exists."""
    path = os.path.join(config.DEVIN_CONTEXT_DIR, name)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def _load_devin_tools() -> List[bytes]:
    """Load the captured Devin built-in tool definitions as protobuf-encoded bytes."""
    import json

    path = os.path.join(config.DEVIN_CONTEXT_DIR, "tools.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            tools = json.load(f)
    except Exception:
        return []
    encoded: List[bytes] = []
    for tool in tools:
        fn = tool.get("function", {})
        encoded.append(
            _build_tool(
                fn.get("name", ""),
                fn.get("description", ""),
                json.dumps(fn.get("parameters", {"type": "object"})),
            )
        )
    return encoded


def _build_system_info() -> str:
    """Generate a ``<system_info>`` block matching the CLI format."""
    cwd = os.getcwd()
    platform = "macos"  # best effort; could detect sys.platform
    os_version = ""
    try:
        uname = os.uname()
        platform = uname.sysname.lower()
        os_version = f"{uname.sysname} {uname.release}"
    except Exception:
        pass
    if not os_version:
        import platform as _platform
        os_version = f"{_platform.system()} {_platform.release()}"
    today = time.strftime("%A, %Y-%m-%d")
    return (
        f"<system_info>\n"
        f"The following information is automatically generated context about your current environment.\n"
        f"Current workspace directories:\n"
        f"  {cwd} (cwd)\n\n"
        f"Platform: {platform}\n"
        f"OS Version: {os_version}\n"
        f"Today's date: {today}\n"
        f"</system_info>"
    )


def _build_rules() -> str:
    """Generate a ``<rules>`` block from global and project rules files."""
    global_rules_path = os.path.expanduser("~/.codeium/windsurf/memories/global_rules.md")
    global_rules = ""
    if os.path.exists(global_rules_path):
        try:
            with open(global_rules_path, "r", encoding="utf-8") as f:
                global_rules = f.read()
        except Exception:
            pass

    agents_rules_path = os.path.join(os.getcwd(), "AGENTS.md")
    agents_rules = ""
    if os.path.exists(agents_rules_path):
        try:
            with open(agents_rules_path, "r", encoding="utf-8") as f:
                agents_rules = f.read()
        except Exception:
            pass

    parts = [f'<rules type="always-on">']
    parts.append(f'<rule name="global_rules" path="{global_rules_path}">\n\n{global_rules}</rule>')
    if agents_rules:
        parts.append(f'\n<rule name="AGENTS" path="{agents_rules_path}">\n{agents_rules}</rule>')
    parts.append('</rules>')
    return "\n".join(parts)


def _build_available_skills() -> str:
    """Load the static ``<available_skills>`` fragment or return a minimal placeholder."""
    text = _load_devin_context("available_skills.txt")
    if text:
        return text
    return "<available_skills>\n</available_skills>"


def _devin_context_messages() -> List[Tuple[int, str, List[bytes], List[bytes], str, str]]:
    """Return the extra context messages the CLI sends before the user message."""
    return [
        (1, _build_system_info(), [], [], "", ""),
        (1, _build_rules(), [], [], "", ""),
        (1, _build_available_skills(), [], [], "", ""),
    ]


def _build_tool(name: str, description: str, parameters_json: str) -> bytes:
    return proto.encode_message(TOOL_SCHEMA, {1: name, 2: description, 3: parameters_json})


def _build_extra_submessage() -> bytes:
    return proto.encode_message(
        {1: 2, 3: 0, 4: 0},
        {1: _uuid(), 3: 4, 4: 14},
    )


# -----------------------------------------------------------------------------
# OpenAI -> Devin request
# -----------------------------------------------------------------------------

_DEVIN_ROLE = {
    "user": 1,
    "assistant": 2,
    "tool": 4,
}


def _openai_role(role: str) -> int:
    return _DEVIN_ROLE.get(role, 1)


def _parse_data_url(url: str) -> Optional[Tuple[str, str]]:
    """Parse ``data:<mime>;base64,<data>`` and return (mime, base64)."""
    m = re.match(r"^data:([^;]+);base64,(.+)$", url)
    if not m:
        return None
    return m.group(1), m.group(2)


async def _resolve_image(
    part: Dict[str, Any],
    client: httpx.AsyncClient,
) -> Optional[Tuple[str, str]]:
    """Return (mime_type, base64_data) for an image_url part, or None."""
    image_url = part.get("image_url", {})
    url = image_url.get("url", "")
    if not url:
        return None

    parsed = _parse_data_url(url)
    if parsed:
        return parsed

    if not config.FETCH_IMAGE_URLS:
        return None

    try:
        resp = await client.get(url, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        mime = resp.headers.get("content-type") or mimetypes.guess_type(url)[0] or "image/png"
        return mime, base64.b64encode(resp.content).decode("ascii")
    except Exception:
        return None


def _openai_tool_call_to_devin(tc: Dict[str, Any]) -> bytes:
    fn = tc.get("function", {})
    call_id = tc.get("id") or f"functions.{fn.get('name','call')}:{tc.get('index',0)}"
    return _build_tool_call_submessage(
        call_id,
        fn.get("name", ""),
        fn.get("arguments", ""),
    )


async def _openai_message_to_devin(
    msg: Dict[str, Any],
    client: httpx.AsyncClient,
) -> Tuple[int, str, List[bytes], List[bytes], str, str]:
    """Return (role, content, images, tool_calls, tool_call_id, thinking)."""
    role = msg.get("role", "user")
    content = msg.get("content", "")
    images: List[bytes] = []
    image_refs: List[str] = []
    tool_calls: List[bytes] = []
    tool_call_id = ""
    thinking = ""

    if isinstance(content, list):
        texts: List[str] = []
        for part in content:
            ptype = part.get("type")
            if ptype == "text":
                texts.append(part.get("text", ""))
            elif ptype == "image_url":
                resolved = await _resolve_image(part, client)
                if resolved:
                    mime, b64 = resolved
                    images.append(_build_image(b64, mime))
                    image_refs.append(f"data:{mime};base64,{b64}")
                else:
                    image_url = part.get("image_url", {})
                    url = image_url.get("url", "")
                    image_refs.append(url)
        content = "\n".join(texts + image_refs)
    elif not isinstance(content, str):
        content = json.dumps(content)

    if role == "assistant":
        openai_tool_calls = msg.get("tool_calls")
        if openai_tool_calls:
            tool_calls = [_openai_tool_call_to_devin(tc) for tc in openai_tool_calls]

    if role == "tool":
        tool_call_id = msg.get("tool_call_id", "")

    return _openai_role(role), content, images, tool_calls, tool_call_id, thinking


def _openai_tool_to_devin(tool: Dict[str, Any]) -> bytes:
    fn = tool.get("function", {})
    return _build_tool(
        fn.get("name", ""),
        fn.get("description", ""),
        json.dumps(fn.get("parameters", {"type": "object"})),
    )


async def _split_messages(
    body: Dict[str, Any],
    client: httpx.AsyncClient,
) -> Tuple[str, List[Tuple[int, str, List[bytes], List[bytes], str, str]]]:
    """Separate system prompt and other messages; resolve images asynchronously."""
    system_parts: List[str] = []
    tasks = []

    for m in body.get("messages", []):
        if m.get("role") == "system":
            content = m.get("content", "")
            if isinstance(content, list):
                texts = [p.get("text", "") for p in content if p.get("type") == "text"]
                system_parts.append("\n".join(texts))
            elif isinstance(content, str):
                system_parts.append(content)
        else:
            tasks.append(_openai_message_to_devin(m, client))

    other = await asyncio.gather(*tasks) if tasks else []

    if config.DEVIN_CONTEXT:
        devin_prompt = _load_devin_context("system_prompt.txt") or "You are Devin."
        if system_parts:
            system_prompt = f"{devin_prompt}\n\n" + "\n\n".join(system_parts)
        else:
            system_prompt = devin_prompt
        messages: List[Tuple[int, str, List[bytes], List[bytes], str, str]] = (
            _devin_context_messages() + list(other)
        )
    else:
        system_prompt = "\n\n".join(system_parts) if system_parts else "You are a helpful coding assistant."
        messages = list(other)

    return system_prompt, messages


async def openai_to_devin_request(
    body: Dict[str, Any],
    token: str,
    client: httpx.AsyncClient,
) -> Tuple[bytes, str, str]:
    """Convert an OpenAI chat.completions request into a Devin protobuf request body.

    Returns (protobuf body, openai_model, generation_id).
    """
    model = body.get("model", config.DEFAULT_DEVIN_MODEL)
    if model in config.DEVIN_MODEL_MAP:
        model = config.DEVIN_MODEL_MAP[model]

    system_prompt, messages = await _split_messages(body, client)
    devin_messages = [
        _build_message(role, content, _uuid(), images, tool_calls, tool_call_id, thinking)
        for role, content, images, tool_calls, tool_call_id, thinking in messages
    ]

    if config.DEVIN_CONTEXT:
        devin_tools = _load_devin_tools()
        devin_tool_names = {
            proto.get_string(proto.decode_message(t)[1][0])
            for t in devin_tools
        }
        user_tools = [
            _openai_tool_to_devin(t)
            for t in body.get("tools", [])
            if t.get("function", {}).get("name") not in devin_tool_names
        ]
        tools = devin_tools + user_tools
    else:
        tools = [_openai_tool_to_devin(t) for t in body.get("tools", [])]

    completion_config = _build_completion_config(
        max_tokens=body.get("max_tokens", config.DEVIN_MAX_TOKENS),
        temperature=body.get("temperature", config.DEVIN_TEMPERATURE),
        top_p=body.get("top_p", config.DEVIN_TOP_P),
        top_k=int(body.get("top_k", config.DEVIN_TOP_K)),
    )

    generation_id = _uuid()

    request = proto.encode_message(
        REQUEST_SCHEMA,
        {
            1: _build_client_metadata(token),
            2: system_prompt,
            3: devin_messages,
            7: 5,
            8: completion_config,
            10: tools,
            15: _build_extra_submessage(),
            16: generation_id,
            20: 1,
            21: model,
        },
    )
    return request, model, generation_id


# -----------------------------------------------------------------------------
# OpenAI response helpers
# -----------------------------------------------------------------------------

def _openai_chunk(
    model: str,
    stream_id: str,
    content: Optional[str] = None,
    reasoning_content: Optional[str] = None,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    finish_reason: Optional[str] = None,
    usage: Optional[Dict[str, Any]] = None,
) -> str:
    delta: Dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if reasoning_content is not None:
        delta["reasoning_content"] = reasoning_content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls

    chunk: Dict[str, Any] = {
        "id": stream_id,
        "object": "chat.completion.chunk",
        "created": _now(),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage:
        chunk["usage"] = usage
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _openai_completion(
    model: str,
    stream_id: str,
    content: str,
    reasoning_content: str = "",
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    finish_reason: Optional[str] = None,
    usage: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    message: Dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning_content:
        message["reasoning_content"] = reasoning_content
    if tool_calls:
        message["tool_calls"] = tool_calls
    completion: Dict[str, Any] = {
        "id": stream_id,
        "object": "chat.completion",
        "created": _now(),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage:
        completion["usage"] = usage
    return completion


def _map_usage(usage_msg: Optional[Dict[int, List[Tuple[int, Any]]]]) -> Optional[Dict[str, int]]:
    if not usage_msg:
        return None
    input_tokens = 0
    output_tokens = 0
    if 2 in usage_msg:
        input_tokens = proto.get_varint(usage_msg[2][0])
    if 3 in usage_msg:
        output_tokens = proto.get_varint(usage_msg[3][0])
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _process_response_frame(
    payload: bytes,
    state: Dict[str, Any],
    include_usage: bool,
    model: str,
    stream_id: str,
) -> List[str]:
    """Decode one protobuf response frame and return OpenAI SSE chunks."""
    try:
        msg = proto.decode_message(payload)
    except Exception:
        return []

    chunks: List[str] = []

    # Reasoning / thinking comes on field 9; final assistant content on field 3.
    if 9 in msg:
        text = proto.decode_string(msg[9][0][1])
        if text:
            state["reasoning"] += text
            chunks.append(_openai_chunk(model, stream_id, reasoning_content=text))

    if 3 in msg:
        text = proto.decode_string(msg[3][0][1])
        if text:
            state["content"] += text
            chunks.append(_openai_chunk(model, stream_id, content=text))

    # Tool call frame: field 6 carries a tool call submessage, field 5 the stop reason.
    if 6 in msg:
        try:
            tc_bytes = proto.get_submessage(msg[6][0])
            tc = proto.decode_message(tc_bytes)
            call_id = proto.get_string(tc[1][0]) if 1 in tc else ""
            name = proto.get_string(tc[2][0]) if 2 in tc else ""
            arguments = proto.get_string(tc[3][0]) if 3 in tc else ""

            state["tool_calls"].append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            )

            tool_index = 0
            chunks.append(
                _openai_chunk(
                    model,
                    stream_id,
                    tool_calls=[
                        {
                            "index": tool_index,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                )
            )
        except Exception:
            pass

    if 5 in msg:
        try:
            stop = proto.get_varint(msg[5][0])
            if stop in _FINISH_REASON:
                state["finish_reason"] = _FINISH_REASON[stop]
        except Exception:
            pass

    if 7 in msg:
        usage_bytes = proto.get_submessage(msg[7][0])
        try:
            usage_msg = proto.decode_message(usage_bytes)
            usage = _map_usage(usage_msg)
            if usage:
                state["usage"] = usage
        except Exception:
            pass

    return chunks


async def devin_frames_to_openai_sse(
    response: httpx.Response,
    model: str,
    stream_id: str,
    include_usage: bool = False,
):
    """Yield formatted SSE lines from a Devin Connect+proto stream."""
    import connect

    state: Dict[str, Any] = {
        "content": "",
        "reasoning": "",
        "tool_calls": [],
        "usage": None,
        "finish_reason": "stop",
    }
    error: Optional[Dict[str, Any]] = None

    async for flags, payload in connect.iter_connect_frames(response):
        if payload is None:
            continue

        if flags == 2:
            if payload and payload.startswith(b"{"):
                try:
                    obj = json.loads(payload.decode("utf-8"))
                    if isinstance(obj, dict) and obj.get("error"):
                        error = obj["error"]
                except Exception:
                    pass
            break

        chunks = _process_response_frame(payload, state, include_usage, model, stream_id)
        for chunk in chunks:
            yield chunk

    if error:
        yield f"data: {json.dumps({'error': error}, ensure_ascii=False)}\n\n"
    else:
        yield _openai_chunk(model, stream_id, finish_reason=state["finish_reason"])

    if include_usage and state.get("usage"):
        yield _openai_chunk(model, stream_id, usage=state["usage"])

    yield "data: [DONE]\n\n"


async def devin_frames_to_openai_completion(
    response: httpx.Response,
    model: str,
    stream_id: str,
) -> Dict[str, Any]:
    """Collect a Devin Connect+proto stream into a single OpenAI completion."""
    import connect

    state: Dict[str, Any] = {
        "content": "",
        "reasoning": "",
        "tool_calls": [],
        "usage": None,
        "finish_reason": "stop",
    }
    error: Optional[Dict[str, Any]] = None

    async for flags, payload in connect.iter_connect_frames(response):
        if payload is None:
            continue
        if flags == 2:
            if payload and payload.startswith(b"{"):
                try:
                    obj = json.loads(payload.decode("utf-8"))
                    if isinstance(obj, dict) and obj.get("error"):
                        error = obj["error"]
                except Exception:
                    pass
            break
        _process_response_frame(payload, state, False, model, stream_id)

    if error:
        return {"error": error}

    return _openai_completion(
        model=model,
        stream_id=stream_id,
        content=state["content"],
        reasoning_content=state["reasoning"],
        tool_calls=state["tool_calls"] or None,
        finish_reason=state["finish_reason"],
        usage=state.get("usage"),
    )


# -----------------------------------------------------------------------------
# Web search tool execution (mirrors Devin CLI's GetWebSearchResults flow)
# -----------------------------------------------------------------------------

WEB_SEARCH_REQUEST_SCHEMA = {1: 2, 2: 2, 3: 0}
WEB_SEARCH_RESULT_SCHEMA = {1: 2, 3: 2, 4: 2, 7: 2}

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for up-to-date information. Returns a list of search results with URLs, titles, and snippets.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
                "num_results": {
                    "type": "integer",
                    "default": 5,
                    "description": "Maximum number of results to return.",
                },
            },
            "required": ["query"],
        },
    },
}


def _devin_auth_header(token: str) -> str:
    return f"Basic {token}-{token}"


async def execute_web_search(
    query: str,
    num_results: int,
    token: str,
    client: httpx.AsyncClient,
) -> str:
    """Call Devin's ``GetWebSearchResults`` and format results like the CLI does."""
    request = proto.encode_message(
        WEB_SEARCH_REQUEST_SCHEMA,
        {
            1: _build_client_metadata(token),
            2: query,
            3: num_results,
        },
    )

    url = f"{config.DEVIN_BASE_URL}/exa.api_server_pb.ApiServerService/GetWebSearchResults"
    headers = {
        "Authorization": _devin_auth_header(token),
        "Content-Type": "application/proto",
        "Accept": "*/*",
        "Connect-Protocol-Version": "1",
        "X-Raindrop-Sdk": config.DEVIN_SDK,
    }

    response = await client.post(url, content=request, headers=headers, timeout=30.0)
    response.raise_for_status()

    results = proto.decode_message(response.content)
    parts = [f'# Web Search Results for "{query}"']
    for idx, (_, raw) in enumerate(results.get(1, []), start=1):
        try:
            msg = proto.decode_message(raw)
            url = proto.get_string(msg[3][0]) if 3 in msg else ""
            title = proto.get_string(msg[4][0]) if 4 in msg else ""
            content = proto.get_string(msg[7][0]) if 7 in msg else ""
            parts.append(f"\n## {idx}. {title}")
            parts.append(f"URL: {url}")
            if content:
                parts.append(f"\n{content}")
        except Exception:
            continue

    return "\n".join(parts)


def maybe_inject_web_search_tool(body: Dict[str, Any]) -> None:
    """Ensure a ``web_search`` tool definition is in the request tools list."""
    tools = body.get("tools") or []
    if any(t.get("function", {}).get("name") == "web_search" for t in tools):
        return
    tools.append(WEB_SEARCH_TOOL)
    body["tools"] = tools


def build_tool_messages(completion: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Generate OpenAI-style assistant + tool messages from a tool-calling completion."""
    assistant_msg: Dict[str, Any] = {
        "role": "assistant",
        "content": completion["choices"][0]["message"].get("content", ""),
    }
    tool_calls = completion["choices"][0]["message"].get("tool_calls")
    if tool_calls:
        assistant_msg["tool_calls"] = tool_calls

    tool_messages = []
    for call in tool_calls or []:
        call_id = call.get("id", "")
        # The actual content will be filled in by the caller after executing the tool.
        tool_messages.append({"role": "tool", "tool_call_id": call_id, "content": ""})

    return [assistant_msg] + tool_messages


def get_web_search_queries(completion: Dict[str, Any]) -> List[Tuple[str, int]]:
    """Return (query, num_results) for every web_search tool call in the completion."""
    queries = []
    for call in completion["choices"][0]["message"].get("tool_calls") or []:
        if call.get("function", {}).get("name") != "web_search":
            continue
        try:
            args = json.loads(call["function"].get("arguments", "{}"))
        except Exception:
            continue
        query = args.get("query", "")
        num_results = args.get("num_results", config.WEB_SEARCH_NUM_RESULTS)
        if query:
            queries.append((query, int(num_results)))
    return queries
