"""Configuration for the Devin API ↔ OpenAI reverse proxy."""

import json
import os
import re
import tomllib
from typing import Any, Dict, List, Optional


def _load_credentials() -> Dict[str, str]:
    """Load local Devin CLI credentials if present."""
    for path in (
        os.path.expanduser("~/.local/share/devin/credentials.toml"),
        os.path.expanduser("~/.config/devin/credentials.toml"),
    ):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
            return {
                "api_server_url": data.get("api_server_url", ""),
                "token": data.get("windsurf_api_key") or data.get("api_key") or "",
            }
        except Exception:
            try:
                with open(path) as f:
                    text = f.read()
                creds: Dict[str, str] = {}
                for key in ("api_server_url", "windsurf_api_key", "api_key"):
                    m = re.search(rf'{key}\s*=\s*"([^"]+)"', text)
                    if m:
                        creds[key] = m.group(1)
                return {
                    "api_server_url": creds.get("api_server_url", ""),
                    "token": creds.get("windsurf_api_key") or creds.get("api_key") or "",
                }
            except Exception:
                pass
    return {}


def _json_env(name: str) -> Any:
    raw = os.getenv(name)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return None


_CREDS = _load_credentials()

# Base URL for Devin LLM inference (Connect-RPC).
# The CLI credentials call this `api_server_url`; captured traffic uses
# `https://server.codeium.com`.
DEVIN_BASE_URL = (
    os.getenv("DEVIN_BASE_URL") or _CREDS.get("api_server_url") or "https://server.codeium.com"
).rstrip("/")

# Connect-RPC method paths.  Captured `devin -p` traffic uses GetChatMessage,
# which is also streamed.  GetDevstralStream is the canonical streaming method.
DEVIN_STREAM_PATH = os.getenv(
    "DEVIN_STREAM_PATH", "/exa.api_server_pb.ApiServerService/GetChatMessage"
)
DEVIN_UNARY_PATH = os.getenv(
    "DEVIN_UNARY_PATH", "/exa.api_server_pb.ApiServerService/GetChatMessage"
)

# The token from the credentials file.  The real header is
# `Authorization: Basic <token>-<token>`.
DEVIN_TOKEN = os.getenv("DEVIN_TOKEN") or _CREDS.get("token")

# Connect content type.  The real endpoint requires protobuf.
DEVIN_CONTENT_TYPE = os.getenv("DEVIN_CONTENT_TYPE", "application/connect+proto")

# SDK / identity header
DEVIN_SDK = os.getenv("DEVIN_SDK", "raindrop-rust")

DEFAULT_DEVIN_MODEL = os.getenv("DEFAULT_DEVIN_MODEL", "swe-1-7")
DEVIN_QUERY_LABEL = os.getenv("DEVIN_QUERY_LABEL", "agent_turn")
DEVIN_TOP_K = int(os.getenv("DEVIN_TOP_K", "40"))

# Defaults derived from the real Devin CLI capture.
DEVIN_TEMPERATURE = float(os.getenv("DEVIN_TEMPERATURE", "1.0"))
DEVIN_TOP_P = float(os.getenv("DEVIN_TOP_P", "0.95"))
DEVIN_MAX_TOKENS = int(os.getenv("DEVIN_MAX_TOKENS", "128000"))

# When true, inject the captured Devin system prompt, system_info, rules,
# available_skills, and built-in tool list to make the request look like CLI.
DEVIN_CONTEXT = os.getenv("DEVIN_CONTEXT", "false").lower() in ("1", "true", "yes")
DEVIN_CONTEXT_DIR = os.getenv("DEVIN_CONTEXT_DIR", os.path.join(os.path.dirname(__file__), "devin_context"))

FETCH_IMAGE_URLS = os.getenv("FETCH_IMAGE_URLS", "true").lower() in ("1", "true", "yes")
AUTO_WEB_SEARCH = os.getenv("AUTO_WEB_SEARCH", "false").lower() in ("1", "true", "yes")
WEB_SEARCH_NUM_RESULTS = int(os.getenv("WEB_SEARCH_NUM_RESULTS", "5"))
WEB_SEARCH_MAX_ROUNDS = int(os.getenv("WEB_SEARCH_MAX_ROUNDS", "5"))
PROXY_TIMEOUT = float(os.getenv("PROXY_TIMEOUT", "300"))

DEVIN_MODEL_MAP: Dict[str, str] = _json_env("DEVIN_MODEL_MAP") or {}
DEFAULT_MODELS: List[str] = _json_env("DEVIN_MODELS") or [
    "claude-opus-5-medium",
    "claude-5-fable-medium",
    "claude-sonnet-5-medium",
    "gpt-5-6-sol-medium",
    "gpt-5-6-luna-medium",
    "glm-5-2",
    "kimi-k3-high",
    "swe-1-7",
    "swe-1-7-lightning",
    "adaptive",
    "claude-opus-4-7-medium",
    "claude-opus-4-7-low",
    "claude-opus-4-7-high",
    "claude-opus-4-7-xhigh",
    "claude-opus-4-7-max",
    "claude-opus-4-8-medium",
    "claude-opus-4-8-low",
    "claude-opus-4-8-high",
    "claude-opus-4-8-xhigh",
    "claude-opus-4-8-max",
    "claude-opus-4-8-low-fast",
    "claude-opus-4-8-medium-fast",
    "claude-opus-4-8-high-fast",
    "claude-opus-4-8-xhigh-fast",
    "claude-opus-4-8-max-fast",
    "claude-opus-5-low",
    "claude-opus-5-high",
    "claude-opus-5-xhigh",
    "claude-opus-5-max",
    "claude-opus-5-low-fast",
    "claude-opus-5-medium-fast",
    "claude-opus-5-high-fast",
    "claude-opus-5-xhigh-fast",
    "claude-opus-5-max-fast",
    "claude-5-fable-low",
    "claude-5-fable-high",
    "claude-5-fable-xhigh",
    "claude-5-fable-max",
    "claude-sonnet-5-low",
    "claude-sonnet-5-high",
    "claude-sonnet-5-xhigh",
    "claude-sonnet-5-max",
    "gemini-3-5-flash-minimal",
    "gemini-3-5-flash-low",
    "gemini-3-5-flash-medium",
    "gemini-3-5-flash-high",
    "gemini-3-6-flash-minimal",
    "gemini-3-6-flash-low",
    "gemini-3-6-flash-medium",
    "gemini-3-6-flash-high",
    "gpt-5-6-sol-none",
    "gpt-5-6-sol-low",
    "gpt-5-6-sol-high",
    "gpt-5-6-sol-xhigh",
    "gpt-5-6-sol-max",
    "gpt-5-6-sol-none-priority",
    "gpt-5-6-sol-low-priority",
    "gpt-5-6-sol-medium-priority",
    "gpt-5-6-sol-high-priority",
    "gpt-5-6-sol-xhigh-priority",
    "gpt-5-6-terra-none",
    "gpt-5-6-terra-low",
    "gpt-5-6-terra-medium",
    "gpt-5-6-terra-high",
    "gpt-5-6-terra-xhigh",
    "gpt-5-6-terra-max",
    "gpt-5-6-terra-none-priority",
    "gpt-5-6-terra-low-priority",
    "gpt-5-6-terra-medium-priority",
    "gpt-5-6-terra-high-priority",
    "gpt-5-6-terra-xhigh-priority",
    "gpt-5-6-luna-none",
    "gpt-5-6-luna-low",
    "gpt-5-6-luna-high",
    "gpt-5-6-luna-xhigh",
    "gpt-5-6-luna-max",
    "gpt-5-6-luna-none-priority",
    "gpt-5-6-luna-low-priority",
    "gpt-5-6-luna-medium-priority",
    "gpt-5-6-luna-high-priority",
    "gpt-5-6-luna-xhigh-priority",
    "glm-5-2-max",
    "glm-5-2-1m",
    "glm-5-2-max-1m",
    "glm-5-2-none",
    "glm-5-2-none-1m",
    "grok-4-5-low",
    "grok-4-5-medium",
    "grok-4-5-high",
    "inkling-none",
    "inkling-low",
    "inkling-medium",
    "inkling-high",
    "inkling-xhigh",
    "inkling-max",
    "kimi-k3-low",
    "kimi-k3-max",
    "swe-1-7-medium",
    "claude-opus-4-6",
    "claude-opus-4-6-thinking",
    "claude-opus-4-6-1m",
    "claude-opus-4-6-thinking-1m",
    "gpt-5-4-none",
    "gpt-5-4-low",
    "gpt-5-4-medium",
    "gpt-5-4-high",
    "gpt-5-4-xhigh",
    "gpt-5-5-none",
    "gpt-5-5-low",
    "gpt-5-5-medium",
    "gpt-5-5-high",
    "gpt-5-5-xhigh",
    "gpt-5-5-none-priority",
    "gpt-5-5-low-priority",
    "gpt-5-5-medium-priority",
    "gpt-5-5-high-priority",
    "gpt-5-5-xhigh-priority",
    "gpt-5-4-none-priority",
    "gpt-5-4-low-priority",
    "gpt-5-4-medium-priority",
    "gpt-5-4-high-priority",
    "gpt-5-4-xhigh-priority",
    "gpt-5-4-mini-low",
    "gpt-5-4-mini-medium",
    "gpt-5-4-mini-high",
    "gpt-5-4-mini-xhigh",
    "claude-sonnet-4-6",
    "claude-sonnet-4-6-thinking",
    "claude-sonnet-4-6-1m",
    "claude-sonnet-4-6-thinking-1m",
    "MODEL_GPT_5_2_LOW",
    "MODEL_GPT_5_2_MEDIUM",
    "MODEL_CLAUDE_4_5_OPUS",
    "MODEL_CLAUDE_4_5_OPUS_THINKING",
    "MODEL_PRIVATE_11",
    "MODEL_PRIVATE_2",
    "MODEL_PRIVATE_3",
    "MODEL_CHAT_GPT_4_1_2025_04_14",
    "MODEL_PRIVATE_12",
    "MODEL_PRIVATE_13",
    "MODEL_PRIVATE_14",
    "MODEL_PRIVATE_15",
    "MODEL_GPT_5_2_NONE",
    "MODEL_GPT_5_2_HIGH",
    "MODEL_GPT_5_2_XHIGH",
    "gpt-5-3-codex-low",
    "gpt-5-3-codex-medium",
    "gpt-5-3-codex-high",
    "gpt-5-3-codex-xhigh",
    "gpt-5-3-codex-low-priority",
    "gpt-5-3-codex-medium-priority",
    "gpt-5-3-codex-high-priority",
    "gpt-5-3-codex-xhigh-priority",
    "kimi-k2-6",
    "kimi-k2-7",
    "nemotron-3-ultra-nvfp4",
    "swe-1-6",
    "swe-1-6-fast",
    "swe-check",
    "opus-4-7-review",
    "gpt-5-5-review",
    "gemini-3-1-pro-low",
    "gemini-3-1-pro-high",
    "MODEL_GOOGLE_GEMINI_3_0_FLASH_MINIMAL",
    "MODEL_GOOGLE_GEMINI_3_0_FLASH_LOW",
    "MODEL_GOOGLE_GEMINI_3_0_FLASH_MEDIUM",
    "MODEL_GOOGLE_GEMINI_3_0_FLASH_HIGH",
    "deepseek-v4",
    "subagent-default",
    "memory-migration-default",
]
