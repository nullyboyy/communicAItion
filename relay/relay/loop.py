"""
Continuous relay loop (message 016, milestone LIVE-01; safety limits and
heartbeat added message 018).

Two phases per cycle, kept genuinely separate in code, not just in
prose, because that boundary is one Pixl explicitly asked to protect:

  TRANSPORT: core.recover() + core.process_once() -- discover, validate,
             commit, archive, record. Never touches a provider, never
             decides what anything means.
  AGENT TURN: core.deliver_pending() -- context.build_context() ->
              adapter.send() -> reply staged. Never discovers files,
              never decides what's pending, never touches project truth
              beyond the one message it was handed.

run_loop() is a bounded polling loop, not a daemon framework.
max_cycles defaults to None (run until interrupted) for a real
always-on process, but every test in this project passes a finite
number so nothing can hang a test run.

Safety limits (message 018 sections 10-11): max_cycles (already existed,
this is "max turns"), max_runtime_s, and a circuit breaker on consecutive
cycle-level failures. None of these require a persistent deployment to
be real -- they're plain Python control flow, testable right here.
"""
import datetime
import json
import time
from pathlib import Path
from typing import List, Optional

import core

HEARTBEAT_DIR = core.ROOT / "state" / "heartbeats"

# Message 020's real finding: two independent processes (one per agent,
# as the systemd deployment proposal has them) writing a single shared
# heartbeat.json meant whichever wrote last silently erased the other's
# record. Per-agent files fix that structurally -- there's no shared
# file for two writers to race on.
DEFAULT_STALE_AFTER_S = 30.0


class CircuitOpen(Exception):
    """Raised (and caught by run_loop) when consecutive cycle failures
    exceed the breaker threshold. Not a crash -- a deliberate stop."""


def heartbeat_path(agent_name: str) -> Path:
    return HEARTBEAT_DIR / f"{agent_name}.json"


def write_heartbeat(agent_name: str, cycle: int, last_seq_seen: int, status: str) -> None:
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    core.atomic_write_json(heartbeat_path(agent_name), {
        "agent": agent_name,
        "status": status,
        "last_cycle": cycle,
        "last_message": last_seq_seen,
        "timestamp": core.now_iso(),
    })


def _parse_iso_utc(ts: str) -> "datetime.datetime":
    """datetime.fromisoformat() doesn't exist until Python 3.7 -- this
    environment is 3.6.8. core.now_iso() always produces UTC with a
    '+00:00' suffix (datetime.now(timezone.utc).isoformat(...)), so
    stripping that fixed suffix and parsing the naive remainder is
    exact for every timestamp this project ever writes, without pulling
    in a dependency for general ISO 8601 parsing this doesn't need."""
    assert ts.endswith("+00:00"), f"unexpected timestamp format: {ts!r}"
    naive = datetime.datetime.strptime(ts[:-6], "%Y-%m-%dT%H:%M:%S")
    return naive.replace(tzinfo=datetime.timezone.utc)


def read_heartbeat(agent_name: str, stale_after_s: float = DEFAULT_STALE_AFTER_S) -> Optional[dict]:
    """Returns the heartbeat dict with an added 'display_status' that
    accounts for staleness -- a heartbeat isn't useful if a 3-day-old
    "alive" record still reads as ONLINE. 'stopped' is left alone
    regardless of age: an explicit stop is definite information, not
    something that goes stale, unlike an unexplained silence."""
    p = heartbeat_path(agent_name)
    if not p.exists():
        return None
    hb = json.loads(p.read_text(encoding="utf-8"))
    if hb["status"] == "stopped":
        hb["display_status"] = "STOPPED"
        return hb
    ts = _parse_iso_utc(hb["timestamp"])
    age_s = (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds()
    hb["age_s"] = age_s
    hb["display_status"] = "STALE" if age_s >= stale_after_s else "ONLINE"
    return hb


def _cycle_failure_delta(events: List[dict]) -> "tuple[int, int]":
    """(failures, deliveries) observed in one cycle. Counting terminal
    failures rather than a pass/fail boolean per cycle matters because
    process_once()/deliver_pending() handle every pending message in one
    cycle, not one message per cycle -- a sustained outage with N
    messages queued fails all N in a single cycle, and that should trip
    the breaker immediately rather than needing N separate cycles to
    "notice." Any success in the same cycle resets the counter to zero
    in the caller: a provider that's working again shouldn't stay
    tripped because of an older failure earlier in the same batch.

    Counts message_delivery_failed AND reply_stage_failed -- message 020
    testing surfaced that only counting the former left a real gap: a
    persistent staging failure (disk full, permissions) retries forever
    (correctly, per section 2 -- it must not abandon a durable result)
    but was invisible to the circuit breaker, which could let it loop
    unbounded. A repeatedly-failing stage is exactly the kind of
    "provider working, environment broken" condition the breaker exists
    to catch, even though no provider call is failing."""
    n_failed = sum(
        1 for e in events
        if e.get("event") in ("message_delivery_failed", "reply_stage_failed")
    )
    n_delivered = sum(1 for e in events if e.get("event") == "message_delivered")
    return n_failed, n_delivered


def run_cycle(agent_name: str, agent) -> List[dict]:
    """One full cycle for one agent's runtime. Returns every event the
    cycle produced, in order."""
    events = [{"event": "cycle_started", "ts": core.now_iso(), "agent": agent_name}]

    # Explicit, even though process_once()/deliver_pending() each call
    # recover() themselves -- idempotent, so calling it here too costs
    # nothing, and it keeps "recovery is part of the ordinary loop, not
    # a special ceremony" true in the code's shape, not just in prose.
    recovery_report = core.recover()
    if recovery_report["effects"] or recovery_report["truncated_line_dropped"]:
        events.append({
            "event": "cycle_recovery", "ts": core.now_iso(),
            "agent": agent_name, "report": recovery_report,
        })

    events.extend(core.process_once())                       # TRANSPORT
    events.extend(core.deliver_pending(agent_name, agent))    # AGENT TURN

    completed = {
        "event": "relay_cycle_completed", "ts": core.now_iso(),
        "agent": agent_name, "events_this_cycle": len(events),
    }
    core.append_event(completed)
    events.append(completed)
    return events


def run_loop(agent_name: str, agent, poll_interval: float = 2.0,
             max_cycles: Optional[int] = None, max_runtime_s: Optional[float] = None,
             max_consecutive_failures: int = 5) -> List[dict]:
    """Bounded polling loop with real safety limits, not just documented
    ones. max_cycles=None + max_runtime_s=None runs until interrupted
    (Ctrl+C) or the circuit breaker trips -- that's the real always-on
    mode. Every test in this project passes a finite max_cycles so
    nothing can hang a test run regardless of the other limits.

    STOP semantics (message 018 section 11): Ctrl+C is caught between
    cycles, never mid-cycle -- the current cycle's transport/delivery
    work always finishes (or fails and is logged) before the loop exits,
    so a human stop can't land inside an in-flight provider call the way
    the crash tests deliberately do. That's the actual difference between
    a "stop" and a "kill": a stop waits for a safe boundary, a kill
    (message 017/019's tests) doesn't, on purpose, to measure what
    happens when it doesn't."""
    core.append_event({
        "event": "relay_started", "ts": core.now_iso(),
        "agent": agent_name, "poll_interval": poll_interval,
        "provider": getattr(agent, "provider_name", type(agent).__name__),
        "max_cycles": max_cycles, "max_runtime_s": max_runtime_s,
        "max_consecutive_failures": max_consecutive_failures,
    })
    all_events: List[dict] = []
    cycle = 0
    consecutive_failures = 0
    start = time.monotonic()
    stop_reason = None

    try:
        while True:
            if max_cycles is not None and cycle >= max_cycles:
                stop_reason = "max_cycles_reached"
                break
            if max_runtime_s is not None and (time.monotonic() - start) >= max_runtime_s:
                stop_reason = "max_runtime_reached"
                break

            cycle_events = run_cycle(agent_name, agent)
            all_events.extend(cycle_events)
            cycle += 1

            last_seq = core.max_committed_seq()
            n_failed, n_delivered = _cycle_failure_delta(cycle_events)
            if n_delivered > 0:
                consecutive_failures = 0       # a real success clears the streak
            else:
                consecutive_failures += n_failed  # an idle cycle (n_failed=0) leaves it unchanged
            write_heartbeat(agent_name, cycle, last_seq,
                             "alive" if consecutive_failures == 0 else "degraded")

            if consecutive_failures >= max_consecutive_failures:
                stop_reason = "circuit_breaker_tripped"
                break

            more_to_do = (max_cycles is None or cycle < max_cycles)
            if more_to_do:
                time.sleep(poll_interval)
    except KeyboardInterrupt:
        stop_reason = "human_stop_requested"

    write_heartbeat(agent_name, cycle, core.max_committed_seq(), "stopped")
    stop_event = {
        "event": "relay_stopped", "ts": core.now_iso(), "agent": agent_name,
        "reason": stop_reason, "cycles_run": cycle,
        "consecutive_failures_at_stop": consecutive_failures,
    }
    core.append_event(stop_event)
    all_events.append(stop_event)
    return all_events
