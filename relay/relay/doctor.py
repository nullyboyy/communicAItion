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
    print(name)
    has_key = bool(os.environ.get(env_var))
    _run(f"{env_var} configured", lambda: (has_key, None if has_key else "not set"))
    reachable = _run(f"{host} reachable", lambda: _endpoint_reachable(host))

    if not has_key or not reachable:
        reason = "no credentials" if not has_key else "endpoint unreachable"
        print(f"    FAIL adapter responding -- skipped ({reason})")
        return

    if not verify_live:
        print("    SKIP adapter responding -- not attempted (pass --verify-live to actually spend one real call)")
        return

    def _live_call():
        agent = adapter_cls(max_tokens=8)
        reply = agent.send("Reply with exactly: OK")
        return bool(reply.strip()), reply.strip()[:40]
    _run("adapter responding (live call made)", _live_call)


def run(verify_live: bool = False):
    check_environment()
    print()
    check_relay()
    print()
    check_provider("OpenAI", "OPENAI_API_KEY", "api.openai.com", adapters.OpenAIAgent, verify_live)
    print()
    check_provider("Anthropic", "ANTHROPIC_API_KEY", "api.anthropic.com", adapters.AnthropicAgent, verify_live)
