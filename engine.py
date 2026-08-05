"""Low-level Devin backend calls shared by chat and responses endpoints."""

import json
import logging
from typing import Any, Dict, Optional

import httpx

import config
import connect
import transform

logger = logging.getLogger("devin-proxy")


def devin_auth_header(token: str) -> str:
    return f"Basic {token}-{token}"


def devin_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": devin_auth_header(token),
        "Content-Type": config.DEVIN_CONTENT_TYPE,
        "Accept": "*/*",
        "Connect-Protocol-Version": "1",
        "X-Raindrop-Sdk": config.DEVIN_SDK,
    }


def devin_url(stream: bool) -> str:
    path = config.DEVIN_STREAM_PATH if stream else config.DEVIN_UNARY_PATH
    return config.DEVIN_BASE_URL + path


def call_devin(client: httpx.AsyncClient, payload: bytes, token: str, stream: bool):
    """Return an httpx stream context manager for the Devin Connect request."""
    body = connect.encode_connect_request(payload)
    return client.stream(
        "POST",
        devin_url(stream),
        content=body,
        headers=devin_headers(token),
    )


async def complete_chat(
    client: httpx.AsyncClient,
    body: Dict[str, Any],
    token: str,
) -> Dict[str, Any]:
    """Run one or more non-streaming GetChatMessage calls, optionally executing tools."""
    original_body = dict(body)
    original_body["messages"] = list(body.get("messages", []))

    if config.AUTO_WEB_SEARCH:
        transform.maybe_inject_web_search_tool(original_body)

    for _ in range(config.WEB_SEARCH_MAX_ROUNDS):
        try:
            devin_payload, openai_model, generation_id = await transform.openai_to_devin_request(
                original_body, token, client
            )
        except Exception as exc:
            logger.exception("Failed to transform OpenAI request to Devin")
            return {"error": {"message": f"Request transformation failed: {exc}"}}

        async with call_devin(client, devin_payload, token, stream=False) as resp:
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                try:
                    detail = exc.response.json()
                except Exception:
                    detail = {"message": str(exc)}
                return {"error": detail}
            except httpx.RequestError as exc:
                return {"error": {"message": str(exc)}}

            completion = await transform.devin_frames_to_openai_completion(
                resp, openai_model, generation_id
            )
            if completion.get("error"):
                return completion

        if not config.AUTO_WEB_SEARCH or completion["choices"][0]["finish_reason"] != "tool_calls":
            return completion

        queries = transform.get_web_search_queries(completion)
        if not queries:
            return completion

        assistant_msg = {
            "role": "assistant",
            "content": completion["choices"][0]["message"].get("content", ""),
            "tool_calls": completion["choices"][0]["message"].get("tool_calls", []),
        }

        tool_results = []
        for i, call in enumerate(assistant_msg["tool_calls"]):
            if call.get("function", {}).get("name") != "web_search":
                continue
            try:
                args = json.loads(call["function"].get("arguments", "{}"))
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
            tool_results.append((i, result_text))

        continuation = list(original_body["messages"])
        continuation.append(assistant_msg)
        for i, result_text in tool_results:
            call = assistant_msg["tool_calls"][i]
            continuation.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": result_text,
                }
            )
        original_body["messages"] = continuation

    return completion
