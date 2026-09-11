"""
CLI for the relay's first milestone: transport + bookkeeping, no models.

    python main.py send --from lobster --to pixl --file draft.md [--reply-to 4]
    python main.py process
    python main.py status [--n 20]

`send` drops a validly-named file into the right outbox (no sequence
number assigned yet -- that happens in `process`, under the lock, so
concurrent sends can never collide).

`process` scans both outboxes once, validates each pending file,
assigns sequence numbers, appends events to history/log.jsonl, moves
each file to messages/archive/ under its canonical name (or
messages/archive/failed/ if malformed), and updates state/shared_state.json.
It does not call any model -- that's the next milestone.

`status` prints the current shared_state.json plus the last N events,
so the operator has visibility without a UI.
"""
import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

import adapters
import core
import doctor
import loop as loop_module


def _agent_for(name: str):
    """The one place that decides which provider backs which agent.
    Both are real adapters now (message 016) -- neither has been
    verified against a live network in this environment, which is a
    fact about this sandbox, not about the code."""
    if name == "lobster":
        return adapters.AnthropicAgent(name="lobster")
    return adapters.OpenAIAgent(name="pixl")


def cmd_send(args: argparse.Namespace) -> int:
    body = Path(args.file).read_text(encoding="utf-8")
    path = core.send_message(args.from_, args.to, body, reply_to=args.reply_to)
    print(f"dropped: {path.relative_to(core.ROOT)}")
    return 0


def cmd_process(args: argparse.Namespace) -> int:
    events = core.process_once()
    if not events:
        print("nothing to process")
        return 0
    for e in events:
        if e["status"] == "processed":
            print(f"seq {e['seq']:06d}: {e['from']} -> {e['to']} "
                  f"(reply_to={e['reply_to']}) archived as {e['archived_as']}")
        else:
            print(f"REJECTED {e['raw_file']}: {e['reason']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = core.load_state()
    print("=== shared_state.json ===")
    print(json.dumps(state, indent=2))
    print(f"\n=== last {args.n} events ===")
    for e in core.tail_events(args.n):
        print(json.dumps(e))
    return 0


def cmd_deliver(args: argparse.Namespace) -> int:
    agent = _agent_for(args.agent)
    results = core.deliver_pending(args.agent, agent)
    if not results:
        print("nothing pending")
        return 0
    for r in results:
        if r["status"] == "delivered":
            print(f"seq {r['seq']:06d}: delivered, reply drafted as {r['reply_draft']}")
        else:
            print(f"seq {r['seq']:06d}: FAILED -- {r['reason']}")
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    report = core.recover()
    print(json.dumps(report, indent=2))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    doctor.run(verify_live=args.verify_live)
    return 0


def cmd_loop(args: argparse.Namespace) -> int:
    agent = _agent_for(args.agent)
    events = loop_module.run_loop(
        args.agent, agent, poll_interval=args.interval, max_cycles=args.cycles,
        max_runtime_s=args.max_runtime, max_consecutive_failures=args.max_failures,
    )
    stop = next((e for e in reversed(events) if e.get("event") == "relay_stopped"), None)
    print(f"stopped: {stop['reason'] if stop else 'unknown'} after {stop['cycles_run'] if stop else '?'} cycle(s), "
          f"{len(events)} events this run -- see log.jsonl for detail")
    return 0


def cmd_live_status(args: argparse.Namespace) -> int:
    events, _ = core.read_log_safe()
    counts = {"messages": 0, "successful_turns": 0, "provider_retries": 0,
              "unknown_results": 0, "delivery_failures": 0}
    last_message_ts = {a: None for a in core.AGENTS}
    for e in events:
        if e.get("event") == "message_processed":
            counts["messages"] += 1
            last_message_ts[e["from"]] = e["ts"]
        elif e.get("event") == "message_delivered":
            counts["successful_turns"] += 1
        elif e.get("event") == "message_retrying":
            counts["provider_retries"] += 1
        elif e.get("event") == "message_delivery_failed":
            counts["delivery_failures"] += 1

    pending = [e for e in events if e.get("event") == "message_processed"]
    delivered_or_replied = {e["seq"] for e in events if e.get("event") == "message_delivered"} | \
                            {e["reply_to"] for e in events if e.get("event") == "message_processed" and e.get("reply_to")}
    for e in pending:
        if e["seq"] not in delivered_or_replied and core.result_status(e["to"], e["seq"], events) == "UNKNOWN":
            counts["unknown_results"] += 1

    heartbeats = {}
    if loop_module.HEARTBEAT_PATH.exists():
        hb = json.loads(loop_module.HEARTBEAT_PATH.read_text(encoding="utf-8"))
        heartbeats[hb["agent"]] = hb

    print("communicAItion")
    print("-" * 28)
    print()
    for agent in core.AGENTS:
        hb = heartbeats.get(agent)
        state = hb["status"].upper() if hb else "NO HEARTBEAT"
        print(f"{agent.capitalize():<12}{state}")
    print()
    print(f"Messages              {counts['messages']}")
    print(f"Successful turns      {counts['successful_turns']}")
    print(f"Provider retries      {counts['provider_retries']}")
    print(f"Unknown results       {counts['unknown_results']}")
    print(f"Delivery failures     {counts['delivery_failures']}")
    print()
    for agent in core.AGENTS:
        ts = last_message_ts[agent]
        print(f"Last {agent} message: {ts if ts else 'never'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="relay")
    sub = p.add_subparsers(dest="command")
    sub.required = True  # Python 3.6: add_subparsers() has no required= kwarg

    p_send = sub.add_parser("send", help="drop a new message into an outbox")
    p_send.add_argument("--from", dest="from_", required=True, choices=core.AGENTS)
    p_send.add_argument("--to", required=True, choices=core.AGENTS)
    p_send.add_argument("--file", required=True, help="path to the message body (Markdown)")
    p_send.add_argument("--reply-to", type=int, default=None, help="sequence number being replied to")
    p_send.set_defaults(func=cmd_send)

    p_process = sub.add_parser("process", help="scan outboxes once, validate + archive + log")
    p_process.set_defaults(func=cmd_process)

    p_status = sub.add_parser("status", help="print current state and recent history")
    p_status.add_argument("--n", type=int, default=20)
    p_status.set_defaults(func=cmd_status)

    p_deliver = sub.add_parser("deliver", help="invoke an agent's adapter on its pending messages")
    p_deliver.add_argument("--agent", required=True, choices=core.AGENTS)
    p_deliver.set_defaults(func=cmd_deliver)

    p_recover = sub.add_parser("recover", help="reconcile log/archive/state after an interruption")
    p_recover.set_defaults(func=cmd_recover)

    p_doctor = sub.add_parser("doctor", help="diagnose environment/relay/provider readiness")
    p_doctor.add_argument("--verify-live", action="store_true",
                           help="actually spend one real provider call per configured provider")
    p_doctor.set_defaults(func=cmd_doctor)

    p_loop = sub.add_parser("loop", help="run this agent's continuous transport+delivery loop")
    p_loop.add_argument("--agent", required=True, choices=core.AGENTS)
    p_loop.add_argument("--interval", type=float, default=2.0, help="seconds between cycles")
    p_loop.add_argument("--cycles", type=int, default=None, help="bounded cycle count; omit to run until Ctrl+C")
    p_loop.add_argument("--max-runtime", type=float, default=None, dest="max_runtime", help="seconds before auto-stop")
    p_loop.add_argument("--max-failures", type=int, default=5, dest="max_failures",
                         help="consecutive delivery failures before the circuit breaker trips")
    p_loop.set_defaults(func=cmd_loop)

    p_live = sub.add_parser("live-status", help="small operational view (message 018 section 12)")
    p_live.set_defaults(func=cmd_live_status)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
