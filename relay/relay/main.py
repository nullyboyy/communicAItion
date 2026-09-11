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


def cmd_loop(args: argparse.Namespace) -> int:
    agent = _agent_for(args.agent)
    events = loop_module.run_loop(args.agent, agent, poll_interval=args.interval, max_cycles=args.cycles)
    print(f"ran {args.cycles if args.cycles is not None else 'unbounded'} cycle(s), "
          f"{len(events)} events -- see log.jsonl for detail")
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

    p_loop = sub.add_parser("loop", help="run this agent's continuous transport+delivery loop")
    p_loop.add_argument("--agent", required=True, choices=core.AGENTS)
    p_loop.add_argument("--interval", type=float, default=2.0, help="seconds between cycles")
    p_loop.add_argument("--cycles", type=int, default=None, help="bounded cycle count; omit to run until Ctrl+C")
    p_loop.set_defaults(func=cmd_loop)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
