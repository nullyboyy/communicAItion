"""
Agent adapters.

The relay core (core.py) only knows how to move, validate, and log
messages. context.py assembles model input. The contract each
adapter satisfies is exactly one function:

    send(assembled_context: str) -> str

Note this signature takes a single pre-assembled string, NOT the raw
state dict Pixl's message 006 sketched (`send(message, state,
history_context)`). That's a deliberate change, not an oversight --
see LOBSTER_TO_PIXL_007.md for the reasoning: her own message draws a
boundary where the adapter must not own "where does project truth
live," but handing it the raw state dict lets it reach in and decide
that for itself anyway. context.build_context() resolves state/
identity/history into one string *before* the adapter is called, so
the adapter only ever sees the finished prompt.

Retry policy lives in core.deliver_pending(), not here -- an adapter
raises AdapterError on any failure (network, bad response, missing
credentials) and does exactly one attempt. The relay owns retry/backoff/
logging, per the same "relay owns orchestration" boundary: on failure it
retries up to 3 times (1s/2s/4s backoff), logging a `message_retrying`
event per attempt. Note what "the message" means here -- the *incoming*
message was already archived as processed back in process_once(), long
before delivery is attempted; a delivery failure never touches that
archived file. It logs a terminal `message_delivery_failed` event instead,
which deliver_pending() treats the same way archive/failed/ treats a
malformed message: not silently retried on the next run, requires a
human to look.

Credentials: read from environment variables only (see .env.example
at the project root). Never read from, or written to, shared_state.json
or history/log.jsonl.
"""
import json
import os
import urllib.error
import urllib.request


class AdapterError(Exception):
    """Raised on any single-attempt adapter failure. Never raised for
    'no credentials configured' silently swallowed elsewhere -- that's
    also an AdapterError, so deliver_pending()'s retry/failed-archive
    path handles it the same as a network blip."""


class StubAgent:
    """Raises on use. Exists so the relay's shape is complete and
    testable before either model is actually wired in."""

    provider_name = "stub"

    def __init__(self, name: str):
        self.name = name

    def send(self, assembled_context: str) -> str:
        raise AdapterError(
            f"'{self.name}' adapter has no model wired in yet -- "
            "this milestone proves transport and bookkeeping only."
        )


class AnthropicAgent:
    """Calls the Anthropic Messages API directly over urllib (stdlib
    only -- this environment's Python 3.6 has no `anthropic` package
    installed, and pulling one in isn't worth a dependency for one
    HTTP call). One attempt; raises AdapterError on any failure."""

    provider_name = "anthropic"
    API_URL = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"

    def __init__(self, name: str = "lobster", model: str = None, max_tokens: int = 1024):
        self.name = name
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
        self.max_tokens = max_tokens

    def send(self, assembled_context: str) -> str:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise AdapterError("ANTHROPIC_API_KEY is not set in the environment")

        payload = json.dumps({
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": assembled_context}],
        }).encode("utf-8")

        req = urllib.request.Request(
            self.API_URL,
            data=payload,
            method="POST",
            headers={
                "x-api-key": api_key,
                "anthropic-version": self.API_VERSION,
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise AdapterError(f"HTTP {e.code} from Anthropic API: {detail}") from e
        except urllib.error.URLError as e:
            raise AdapterError(f"network error calling Anthropic API: {e}") from e

        try:
            return "".join(block["text"] for block in body["content"] if block.get("type") == "text")
        except (KeyError, TypeError) as e:
            raise AdapterError(f"unexpected response shape from Anthropic API: {body}") from e


class OpenAIAgent:
    """Pixl's adapter, symmetrical to AnthropicAgent by construction --
    same one-attempt contract, same urllib-only implementation, same
    AdapterError-on-any-failure behavior. This is real, correct code
    against OpenAI's Chat Completions API, but it is UNVERIFIED against
    the live network, same as AnthropicAgent: this environment has no
    outbound network access and no OPENAI_API_KEY configured. Message
    016 asked this to be implemented/verified; implemented is honest,
    verified is not, and I'm not blurring that line."""

    provider_name = "openai"
    API_URL = "https://api.openai.com/v1/chat/completions"

    def __init__(self, name: str = "pixl", model: str = None, max_tokens: int = 1024):
        self.name = name
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o")
        self.max_tokens = max_tokens

    def send(self, assembled_context: str) -> str:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise AdapterError("OPENAI_API_KEY is not set in the environment")

        payload = json.dumps({
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": assembled_context}],
        }).encode("utf-8")

        req = urllib.request.Request(
            self.API_URL,
            data=payload,
            method="POST",
            headers={
                "authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise AdapterError(f"HTTP {e.code} from OpenAI API: {detail}") from e
        except urllib.error.URLError as e:
            raise AdapterError(f"network error calling OpenAI API: {e}") from e

        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise AdapterError(f"unexpected response shape from OpenAI API: {body}") from e
