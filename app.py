"""OpenAI-compatible reverse proxy for Devin LLM inference (Connect-RPC).

Exposes ``/v1/chat/completions`` (and a few other OpenAI-style endpoints).
The proxy converts OpenAI requests into Devin's protobuf Connect stream,
posts it to ``server.codeium.com``, and converts the stream back to OpenAI.
"""

import json
import logging
import os
from typing import Any, AsyncIterator, Dict, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import config
import connect
import engine
import responses
import transform

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("devin-proxy")

if os.path.exists(".env"):
    load_dotenv(".env")

app = FastAPI(title="Devin API ↔ OpenAI Reverse Proxy", version="0.4.0")


@app.on_event("startup")
async def startup():
    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(config.PROXY_TIMEOUT),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )


@app.on_event("shutdown")
async def shutdown():
    await app.state.client.aclose()


def _extract_token(creds: str) -> str:
    """Return the raw token from a credential string.

    The real backend accepts either a single token or ``<token>-<token>``.
    The token itself may contain hyphens, so we detect the doubled form by
    looking for a separator exactly in the middle of the string.
    """
    n = len(creds)
    if n % 2 == 1:
        mid = n // 2
        if creds[mid] == "-" and creds[:mid] == creds[mid + 1 :]:
            return creds[:mid]
    return creds


def _get_token(request: Request) -> Optional[str]:
    """Resolve the raw Devin/CLI token.

    The real backend expects ``Authorization: Basic <token>-<token>``.
    A client may pass the raw token, ``Bearer <token>``, or the full
    ``Basic <token>-<token>`` header.
    """
    auth = request.headers.get("Authorization", "")
    if not auth:
        return config.DEVIN_TOKEN

    lower = auth.lower()
    if lower.startswith("basic "):
        return _extract_token(auth[6:])
    if lower.startswith("bearer "):
        return auth[7:]
    return _extract_token(auth)


@app.get("/health")
async def health():
    return {"status": "ok", "devin_base_url": config.DEVIN_BASE_URL}


@app.get("/v1/models")
async def models():
    data = [
        {"id": m, "object": "model", "created": transform._now(), "owned_by": "devin"}
        for m in config.DEFAULT_MODELS
    ]
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    token = _get_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Missing Authorization header or DEVIN_TOKEN")

    stream = body.get("stream", False)
    include_usage = (body.get("stream_options") or {}).get("include_usage", False)

    if stream:
        try:
            devin_payload, openai_model, generation_id = await transform.openai_to_devin_request(
                body, token, app.state.client
            )
        except Exception as exc:
            logger.exception("Failed to transform OpenAI request to Devin")
            raise HTTPException(status_code=400, detail=f"Request transformation failed: {exc}")

        async def sse_generator() -> AsyncIterator[str]:
            async with engine.call_devin(app.state.client, devin_payload, token, stream=True) as resp:
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    try:
                        detail = exc.response.json()
                    except Exception:
                        detail = {"message": str(exc)}
                    yield f"data: {json.dumps({'error': detail}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                except httpx.RequestError as exc:
                    yield f"data: {json.dumps({'error': {'message': str(exc)}}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                async for line in transform.devin_frames_to_openai_sse(
                    resp, openai_model, generation_id, include_usage=include_usage
                ):
                    yield line

        return StreamingResponse(
            sse_generator(),
            media_type="text/event-stream; charset=utf-8",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # Non-streaming
    completion = await engine.complete_chat(app.state.client, body, token)
    if completion.get("error"):
        return JSONResponse(completion, status_code=400)
    return JSONResponse(completion)


@app.post("/v1/responses")
async def responses_endpoint(request: Request):
    body = await request.json()
    token = _get_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Missing Authorization header or DEVIN_TOKEN")

    if body.get("stream"):
        return StreamingResponse(
            responses.stream_response(app.state.client, body, token),
            media_type="text/event-stream; charset=utf-8",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    response = await responses.create_response(app.state.client, body, token)
    if response.get("error"):
        return JSONResponse(response, status_code=400)
    return JSONResponse(response)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def passthrough(request: Request, path: str):
    """Passthrough everything else to the Devin API with the right headers."""
    token = _get_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Missing Authorization header or DEVIN_TOKEN")

    client: httpx.AsyncClient = app.state.client
    url = config.DEVIN_BASE_URL + "/" + path
    if request.query_params:
        url += "?" + str(request.query_params)

    method = request.method
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    headers["Authorization"] = engine.devin_auth_header(token)
    headers["X-Raindrop-Sdk"] = config.DEVIN_SDK

    body = None
    if method in ("POST", "PUT", "PATCH"):
        body = await request.body()

    response = await client.request(method, url, content=body, headers=headers)

    async def body_iter():
        async for chunk in response.aiter_bytes():
            yield chunk

    return StreamingResponse(
        body_iter(),
        status_code=response.status_code,
        headers={
            k: v
            for k, v in response.headers.items()
            if k.lower() not in ("content-length", "transfer-encoding")
        },
        media_type=response.headers.get("content-type"),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
