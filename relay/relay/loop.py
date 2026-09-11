"""
Continuous relay loop (message 016, milestone LIVE-01).

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
"""
import time
from typing import List, Optional

import core


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
             max_cycles: Optional[int] = None) -> List[dict]:
    """Bounded polling loop. max_cycles=None runs until interrupted
    (Ctrl+C) -- that's the real always-on mode. A finite max_cycles is
    what every test here uses, precisely so a test can never hang."""
    core.append_event({
        "event": "relay_started", "ts": core.now_iso(),
        "agent": agent_name, "poll_interval": poll_interval,
        "provider": getattr(agent, "provider_name", type(agent).__name__),
    })
    all_events: List[dict] = []
    cycle = 0
    try:
        while max_cycles is None or cycle < max_cycles:
            all_events.extend(run_cycle(agent_name, agent))
            cycle += 1
            if max_cycles is None or cycle < max_cycles:
                time.sleep(poll_interval)
    except KeyboardInterrupt:
        pass
    return all_events
