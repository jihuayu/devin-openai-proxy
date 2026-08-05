"""OpenAI Responses API -> Devin chat completion adapter.

Supports non-streaming /v1/responses.  This is a best-effort mapping; the
Responses API has many features (computer use, file search, prompt caching,
stateful previous_response_id) that do not map 1:1 to the Devin backend.
"""

import json
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx

import config
import engine
import transform


def _new_id() -> str:
    return f"resp_{uuid.uuid4().hex[:16]}"


def _new_item_id() -> str:
    return f"msg_{uuid.uuid4().hex[:16]}"


def _new_call_id() -> str:
    return f"fc_{uuid.uuid4().hex[:16]}"


def _input_content_to_openai(content: Any) -> Any:
    """Convert a single Response input content item to an OpenAI content part."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    converted: List[Dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "input_text":
            converted.append({"type": "text", "text": item.get("text", "")})
        elif itype == "input_image":
            image_url = item.get("image_url") or item.get("file_id")
            if image_url:
                converted.append({"type": "image_url", "image_url": {"url": image_url}})
        elif itype == "input_file":
            # Files are not supported in the Devin chat path; skip.
            continue
    return converted if converted else ""


def _input_items_to_messages(input_value: Any) -> List[Dict[str, Any]]:
    """Convert a Responses ``input`` value to OpenAI ``messages``."""
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]

    if not isinstance(input_value, list):
        return [{"role": "user", "content": str(input_value)}]

    messages: List[Dict[str, Any]] = []
    for item in input_value:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue

        itype = item.get("type")
        if itype == "message":
            role = item.get("role", "user")
            if role == "developer":
                role = "system"
            messages.append({"role": role, "content": _input_content_to_openai(item.get("content", ""))})

        elif itype == "function_call_output":
            output = item.get("output", "")
            if isinstance(output, list):
                texts = [p.get("text", "") for p in output if p.get("type") == "output_text"]
                output = "\n".join(texts)
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": output,
            })

        elif itype == "web_search_call":
            # Web search results can be passed back as a tool result.
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("id", ""),
                "content": json.dumps(item.get("action", {}), ensure_ascii=False),
            })

        # other input item types are ignored

    return messages


def _tools_to_openai(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Translate Responses tool definitions to Chat Completions tool definitions."""
    out: List[Dict[str, Any]] = []
    for tool in tools:
        ttype = tool.get("type")
        if ttype == "function":
            out.append(tool)
        elif ttype == "web_search":
            # Map the built-in web_search tool to the local web_search function tool.
            out.append(transform.WEB_SEARCH_TOOL)
        # other built-in tools (file_search, code_interpreter, etc.) not supported
    return out


def _build_usage(usage: Optional[Dict[str, int]]) -> Dict[str, Any]:
    u = usage or {}
    return {
        "input_tokens": u.get("prompt_tokens", 0),
        "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
        "output_tokens": u.get("completion_tokens", 0),
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": u.get("total_tokens", 0),
    }


def _message_output(text: str) -> Dict[str, Any]:
    return {
        "type": "message",
        "id": _new_item_id(),
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": text,
                "annotations": [],
            }
        ],
        "status": "completed",
    }


def _function_call_output(call: Dict[str, Any]) -> Dict[str, Any]:
    fn = call.get("function", {})
    return {
        "type": "function_call",
        "id": call.get("id", _new_item_id()),
        "call_id": call.get("id", _new_call_id()),
        "name": fn.get("name", ""),
        "arguments": fn.get("arguments", ""),
        "status": "completed",
    }


def _web_search_call_output(call: Dict[str, Any]) -> Dict[str, Any]:
    fn = call.get("function", {})
    try:
        args = json.loads(fn.get("arguments", "{}"))
    except Exception:
        args = {}
    return {
        "type": "web_search_call",
        "id": call.get("id", _new_item_id()),
        "call_id": call.get("id", _new_call_id()),
        "status": "completed",
        "action": {
            "type": "search",
            "query": args.get("query", ""),
            "num_results": args.get("num_results", config.WEB_SEARCH_NUM_RESULTS),
        },
    }


def _completion_to_response(completion: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a Chat Completions response to a Responses API response."""
    if completion.get("error"):
        return completion

    choice = completion["choices"][0]
    message = choice.get("message", {})
    tool_calls = message.get("tool_calls") or []

    output: List[Dict[str, Any]] = []
    if tool_calls:
        for call in tool_calls:
            name = call.get("function", {}).get("name", "")
            if name == "web_search":
                output.append(_web_search_call_output(call))
            else:
                output.append(_function_call_output(call))

    # If the final message has text, append it as the last output item.
    if message.get("content"):
        output.append(_message_output(message["content"]))

    return {
        "id": completion.get("id", _new_id()),
        "object": "response",
        "created_at": completion.get("created", time.time()),
        "model": completion.get("model", body.get("model", "")),
        "output": output,
        "parallel_tool_calls": body.get("parallel_tool_calls", True),
        "temperature": completion.get("temperature", body.get("temperature", config.DEVIN_TEMPERATURE)),
        "top_p": completion.get("top_p", body.get("top_p", config.DEVIN_TOP_P)),
        "tool_choice": body.get("tool_choice", "auto"),
        "tools": [t for t in body.get("tools", [])],
        "instructions": body.get("instructions"),
        "usage": _build_usage(completion.get("usage")),
    }


def responses_to_chat_body(body: Dict[str, Any]) -> Dict[str, Any]:
    """Build an OpenAI chat.completions body from a Responses API body."""
    messages = _input_items_to_messages(body.get("input"))

    if body.get("instructions"):
        messages.insert(0, {"role": "system", "content": body["instructions"]})

    chat_body: Dict[str, Any] = {
        "model": body.get("model", config.DEFAULT_DEVIN_MODEL),
        "messages": messages,
        "stream": False,
    }

    if "max_output_tokens" in body:
        chat_body["max_tokens"] = body["max_output_tokens"]
    elif "max_tokens" in body:
        chat_body["max_tokens"] = body["max_tokens"]

    if "temperature" in body:
        chat_body["temperature"] = body["temperature"]
    if "top_p" in body:
        chat_body["top_p"] = body["top_p"]
    if "top_k" in body:
        chat_body["top_k"] = body["top_k"]
    if "reasoning" in body:
        chat_body["reasoning"] = body["reasoning"]

    if body.get("tools"):
        chat_body["tools"] = _tools_to_openai(body["tools"])
    if "tool_choice" in body:
        chat_body["tool_choice"] = body["tool_choice"]
    if "parallel_tool_calls" in body:
        chat_body["parallel_tool_calls"] = body["parallel_tool_calls"]

    return chat_body


async def create_response(
    client: httpx.AsyncClient,
    body: Dict[str, Any],
    token: str,
) -> Dict[str, Any]:
    """Create a non-streaming Responses API response."""
    chat_body = responses_to_chat_body(body)
    completion = await engine.complete_chat(client, chat_body, token)
    return _completion_to_response(completion, body)
