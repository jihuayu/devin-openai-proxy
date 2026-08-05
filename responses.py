"""OpenAI Responses API -> Devin chat completion adapter.

Supports non-streaming and streaming /v1/responses.  This is a best-effort
mapping; many Responses API features (computer use, file search, prompt
caching, stateful previous_response_id) do not map 1:1 to the Devin backend.
"""

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

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


def _event(event_type: str, data: Dict[str, Any]) -> str:
    return f"event: response.{event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_response(
    client: httpx.AsyncClient,
    body: Dict[str, Any],
    token: str,
) -> AsyncIterator[str]:
    """Stream a Responses API response by translating a Chat Completions SSE stream."""
    import json as _json

    chat_body = responses_to_chat_body(body)
    chat_body["stream"] = True

    response_id = _new_id()
    model = chat_body.get("model", config.DEFAULT_DEVIN_MODEL)
    created_at = time.time()

    yield _event(
        "created",
        {
            "type": "response.created",
            "response": {
                "id": response_id,
                "object": "response",
                "created_at": created_at,
                "model": model,
                "output": [],
            },
        },
    )

    try:
        devin_payload, openai_model, _ = await transform.openai_to_devin_request(
            chat_body, token, client
        )
    except Exception as exc:
        logger = logging.getLogger("devin-proxy")
        logger.exception("Failed to transform Responses request")
        yield _event("error", {"type": "error", "message": str(exc)})
        return

    message_item_id: Optional[str] = None
    message_started = False
    content_text = ""
    tool_states: Dict[int, Dict[str, Any]] = {}
    finish_reason: Optional[str] = None
    final_usage: Optional[Dict[str, int]] = None

    async with engine.call_devin(client, devin_payload, token, stream=True) as resp:
        try:
            resp.raise_for_status()
        except Exception as exc:
            yield _event("error", {"type": "error", "message": str(exc)})
            return

        async for line in transform.devin_frames_to_openai_sse(
            resp, openai_model, response_id, include_usage=True
        ):
            if line.startswith("data: [DONE]"):
                continue
            if not line.startswith("data: "):
                continue

            try:
                chunk = _json.loads(line[6:].strip())
            except Exception:
                continue

            if chunk.get("usage"):
                final_usage = chunk["usage"]

            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta", {})

            # Content streaming
            delta_text = delta.get("content") or ""
            if delta_text:
                if not message_started:
                    message_item_id = _new_item_id()
                    message_started = True
                    yield _event(
                        "output_item.added",
                        {
                            "type": "output_item.added",
                            "output_item": {
                                "type": "message",
                                "id": message_item_id,
                                "role": "assistant",
                                "content": [],
                                "status": "in_progress",
                            },
                        },
                    )
                    yield _event(
                        "content_part.added",
                        {
                            "type": "content_part.added",
                            "item_id": message_item_id,
                            "content_index": 0,
                            "part": {
                                "type": "output_text",
                                "text": "",
                                "annotations": [],
                            },
                        },
                    )
                content_text += delta_text
                yield _event(
                    "output_text.delta",
                    {
                        "type": "output_text.delta",
                        "item_id": message_item_id,
                        "content_index": 0,
                        "delta": delta_text,
                    },
                )

            # Tool call streaming
            for call in delta.get("tool_calls") or []:
                idx = call.get("index", 0)
                state = tool_states.setdefault(
                    idx,
                    {
                        "id": None,
                        "name": None,
                        "arguments": "",
                        "item_added": False,
                        "last_args_len": 0,
                    },
                )
                if call.get("id"):
                    state["id"] = call["id"]
                if call.get("function", {}).get("name"):
                    state["name"] = call["function"]["name"]
                if call.get("function", {}).get("arguments"):
                    state["arguments"] += call["function"]["arguments"]

                if state["id"] and state["name"] and not state["item_added"]:
                    state["item_added"] = True
                    yield _event(
                        "output_item.added",
                        {
                            "type": "output_item.added",
                            "output_item": {
                                "type": "function_call",
                                "id": state["id"],
                                "call_id": state["id"],
                                "name": state["name"],
                                "arguments": state["arguments"],
                                "status": "in_progress",
                            },
                        },
                    )
                    state["last_args_len"] = len(state["arguments"])
                elif state["item_added"]:
                    new_len = len(state["arguments"])
                    if new_len > state["last_args_len"]:
                        arg_delta = state["arguments"][state["last_args_len"] : new_len]
                        state["last_args_len"] = new_len
                        yield _event(
                            "function_call_arguments.delta",
                            {
                                "type": "function_call_arguments.delta",
                                "item_id": state["id"],
                                "delta": arg_delta,
                            },
                        )

            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

    # Close any message content part
    if message_started:
        yield _event(
            "output_text.done",
            {
                "type": "output_text.done",
                "item_id": message_item_id,
                "content_index": 0,
            },
        )
        yield _event(
            "output_item.done",
            {
                "type": "output_item.done",
                "item_id": message_item_id,
            },
        )

    # Close tool calls
    for state in tool_states.values():
        if not state["item_added"]:
            continue
        if state.get("id"):
            yield _event(
                "function_call_arguments.done",
                {
                    "type": "function_call_arguments.done",
                    "item_id": state["id"],
                },
            )
            yield _event(
                "output_item.done",
                {
                    "type": "output_item.done",
                    "item_id": state["id"],
                },
            )

    final_item_id: Optional[str] = None
    final_text = ""

    # If the model returned a web_search tool call and auto-execute is enabled,
    # run the search and emit a final assistant message.
    if (
        finish_reason == "tool_calls"
        and config.AUTO_WEB_SEARCH
        and tool_states
        and all(s.get("name") == "web_search" for s in tool_states.values() if s.get("item_added"))
    ):
        assistant_msg = {
            "role": "assistant",
            "content": content_text,
            "tool_calls": [
                {
                    "id": s["id"],
                    "type": "function",
                    "function": {
                        "name": s["name"],
                        "arguments": s["arguments"],
                    },
                }
                for s in tool_states.values()
                if s.get("item_added")
            ],
        }
        continuation = list(chat_body.get("messages", []))
        continuation.append(assistant_msg)
        for s in tool_states.values():
            if not s.get("item_added") or s.get("name") != "web_search":
                continue
            try:
                args = _json.loads(s["arguments"])
            except Exception:
                continue
            query = args.get("query", "")
            num_results = args.get("num_results", config.WEB_SEARCH_NUM_RESULTS)
            if not query:
                continue
            try:
                result_text = await transform.execute_web_search(
                    query, int(num_results), token, client
                )
            except Exception as exc:
                result_text = f"Error executing web_search: {exc}"
            continuation.append(
                {
                    "role": "tool",
                    "tool_call_id": s["id"],
                    "content": result_text,
                }
            )

        chat_body["messages"] = continuation
        completion = await engine.complete_chat(client, chat_body, token)
        final_text = (completion.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if final_text:
            final_item_id = _new_item_id()
            yield _event(
                "output_item.added",
                {
                    "type": "output_item.added",
                    "output_item": {
                        "type": "message",
                        "id": final_item_id,
                        "role": "assistant",
                        "content": [],
                        "status": "completed",
                    },
                },
            )
            yield _event(
                "content_part.added",
                {
                    "type": "content_part.added",
                    "item_id": final_item_id,
                    "content_index": 0,
                    "part": {
                        "type": "output_text",
                        "text": "",
                        "annotations": [],
                    },
                },
            )
            yield _event(
                "output_text.delta",
                {
                    "type": "output_text.delta",
                    "item_id": final_item_id,
                    "content_index": 0,
                    "delta": final_text,
                },
            )
            yield _event(
                "output_text.done",
                {
                    "type": "output_text.done",
                    "item_id": final_item_id,
                    "content_index": 0,
                },
            )
            yield _event(
                "output_item.done",
                {
                    "type": "output_item.done",
                    "item_id": final_item_id,
                },
            )

    # Build final response and emit completed
    output: List[Dict[str, Any]] = []
    if message_started:
        output.append(_message_output(content_text))
    for s in tool_states.values():
        if s.get("item_added"):
            output.append(
                {
                    "type": "function_call",
                    "id": s.get("id"),
                    "call_id": s.get("id"),
                    "name": s.get("name"),
                    "arguments": s.get("arguments"),
                    "status": "completed",
                }
            )
    if final_item_id and final_text:
        output.append(_message_output(final_text))

    final_response = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "model": model,
        "output": output,
        "parallel_tool_calls": body.get("parallel_tool_calls", True),
        "temperature": body.get("temperature", config.DEVIN_TEMPERATURE),
        "top_p": body.get("top_p", config.DEVIN_TOP_P),
        "tool_choice": body.get("tool_choice", "auto"),
        "usage": _build_usage(final_usage),
    }
    yield _event(
        "completed",
        {
            "type": "response.completed",
            "response": final_response,
        },
    )
