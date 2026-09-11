"""
relay doctor (message 018, section 4).

Diagnostic command for an unfamiliar or newly-cloned environment. Never
prints a secret's value -- only whether it's configured. Never spends a
real provider call unless --verify-live is explicitly passed, because a
diagnostic tool that silently costs money on every run is a bad diagnostic
tool; the default check for "does the endpoint exist" is a bare TCP
connect to port 443, not an API call.
"""
import os
import platform
import socket
import sys

import adapters
import core


def _mark(ok: bool) -> str:
    # Not the unicode check/cross Pixl's spec used -- this Windows
    # console's default cp1252 stdout crashes on print() with those
    # characters (UnicodeEncodeError, not just a display glitch, unlike
    # the earlier em-dash case). ASCII is the boring, portable choice.
    return "OK " if ok else "FAIL"


def _run(label: str, fn) -> bool:
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, str(e)
    line = f"    {_mark(ok)} {label}"
    if detail:
        line += f" -- {detail}"
    print(line)
    return ok


def _writable_probe():
    try:
        core.ROOT.mkdir(parents=True, exist_ok=True)
        probe = core.ROOT / ".doctor_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, None
    except Exception as e:
        return False, str(e)


def _endpoint_reachable(host: str):
    try:
        sock = socket.create_connection((host, 443), timeout=5)
        sock.close()
        return True, None
    except OSError as e:
        return False, f"cannot reach {host}:443 -- {e}"


def check_environment():
    print("Environment")
    _run("Python >= 3.6", lambda: (sys.version_info >= (3, 6), f"found {platform.python_version()}"))
    _run("RELAY_ROOT resolves to an existing directory", lambda: (core.ROOT.exists(), str(core.ROOT)))
    _run("filesystem is writable", _writable_probe)


def check_relay():
    print("Relay")
    _run("state readable", lambda: (isinstance(core.load_state(), dict), None))

    def _log_valid():
        events, truncated = core.read_log_safe()
        detail = f"{len(events)} event(s)"
        if truncated:
            detail += ", torn final line auto-repairable"
        return True, detail
    _run("event log readable (tolerating a torn final line)", _log_valid)

    def _recovery():
        report = core.recover()
        return True, f"{len(report['effects'])} effect(s) reapplied this run"
    _run("recovery runs cleanly", _recovery)


def check_provider(name: str, env_var: str, host: str, adapter_cls, verify_live: bool):
    """Four states (message 020 section 6), not the two this used to
    conflate. The category error being guarded against: "endpoint
    accepts invalid credentials" (REACHABLE) is not the same claim as
    "our credential actually works" (AUTHENTICATED), which is not the
    same claim as "the adapter correctly parsed a real response"
    (ADAPTER_OK). One real call, when --verify-live is passed, is
    enough to determine both of the last two together: if adapter.send()
    returns text without raising, the request was both authenticated
    and correctly parsed. If it raises with an HTTP 401/403 in the
    message, that's specifically an authentication failure, not a
    parsing one, and ADAPTER_OK is reported as SKIP rather than FAIL --
    it was never attempted, because there's nothing to parse without a
    successful response."""
    print(name)
    has_key = bool(os.environ.get(env_var))
    _run("configured", lambda: (has_key, None if has_key else f"{env_var} not set"))
    reachable = _run("reachable", lambda: _endpoint_reachable(host))

    if not has_key or not reachable:
        reason = "no credentials" if not has_key else "endpoint unreachable"
        print(f"    FAIL authenticated -- skipped ({reason})")
        print(f"    FAIL adapter response parsed -- skipped ({reason})")
        return

    if not verify_live:
        print("    SKIP authenticated -- not attempted (pass --verify-live to actually spend one real call)")
        print("    SKIP adapter response parsed -- not attempted")
        return

    try:
        agent = adapter_cls(max_tokens=8)
        reply = agent.send("Reply with exactly: OK")
        print(f"    {_mark(True)} authenticated")
        print(f"    {_mark(True)} adapter response parsed -- {reply.strip()[:40]!r}")
    except adapters.AdapterError as e:
        msg = str(e)
        auth_failure = "HTTP 401" in msg or "HTTP 403" in msg
        print(f"    FAIL authenticated -- {msg[:120]}")
        if auth_failure:
            print("    SKIP adapter response parsed -- not attempted (authentication failed first)")
        else:
            # Some other failure (5xx, malformed response, timeout) --
            # authentication status is genuinely unknown here, not
            # provably fine, so it's still reported FAIL above rather
            # than guessed as OK.
            print(f"    FAIL adapter response parsed -- {msg[:120]}")


def run(verify_live: bool = False):
    check_environment()
    print()
    check_relay()
    print()
    check_provider("OpenAI", "OPENAI_API_KEY", "api.openai.com", adapters.OpenAIAgent, verify_live)
    print()
    check_provider("Anthropic", "ANTHROPIC_API_KEY", "api.anthropic.com", adapters.AnthropicAgent, verify_live)
