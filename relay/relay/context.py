"""
Deterministic context assembly (Pixl, message 006, section 3).

The relay decides what a model sees. Adapters never receive the raw
state dict or reach into project files themselves -- they receive one
assembled string built here. That boundary is deliberate: it's the
direct answer to Pixl's own rule that the adapter should not own
"where does project truth live."

Formula, per her spec:

    AGENT IDENTITY
    + CURRENT SHARED STATE
    + RELEVANT MESSAGE
    + RELEVANT RECENT HISTORY
    + OPTIONAL PROJECT FILES
    = MODEL INPUT

What each piece actually is in this milestone, and why:

- AGENT IDENTITY: the recipient's identity/<NAME>.md, verbatim. Not
  summarized -- it's small enough that summarizing it would be
  compression we haven't earned per our own three-exchange rule.

- CURRENT SHARED STATE: the full shared_state.json, serialized. It's
  small (objective/constraints/facts/decisions/disagreements/etc, all
  arrays) precisely because message 006 draws a hard line against
  letting it become a dumping ground. If it ever grows large enough
  that including it whole is wasteful, that's the evidence needed to
  start summarizing it -- not a reason to summarize preemptively.

- RELEVANT MESSAGE: the incoming message body, unmodified.

- RELEVANT RECENT HISTORY: NOT the whole log. Only the causal chain
  reached by following reply_to backwards from the incoming message,
  up to a small hop limit. This is the direct, deterministic reading
  of "if state already has the decision, don't re-feed the paragraphs
  explaining why" -- history here answers "what led to this specific
  message," not "what has ever happened in the project." If there's
  no reply_to, there's no history component: an unprompted message
  stands on state + identity alone.

- OPTIONAL PROJECT FILES: not implemented. Feeding "the project" into
  a model call was explicitly the thing being guarded against -- there
  is no default file to attach. A future call site can pass extra
  material explicitly and deliberately; this module won't guess at it.
"""
import json
from pathlib import Path
from typing import List, Optional

import core

IDENTITY_DIR = core.ROOT.parent / "identity"
MAX_HISTORY_HOPS = 3


def load_identity(agent: str) -> str:
    path = IDENTITY_DIR / f"{agent.upper()}.md"
    if not path.exists():
        return f"(no identity file found at {path})"
    return path.read_text(encoding="utf-8")


def _causal_chain(reply_to: Optional[int], max_hops: int = MAX_HISTORY_HOPS) -> List[dict]:
    """Follow reply_to backwards through the log, oldest first, capped
    at max_hops. Deterministic: same reply_to always yields the same
    chain given the same log contents."""
    if reply_to is None:
        return []
    events_by_seq = {}
    for e in core.tail_events(n=10_000):  # log is small at this stage; revisit if that stops being true
        if e.get("event") == "message_processed":
            events_by_seq[e["seq"]] = e

    chain = []
    current = reply_to
    hops = 0
    while current is not None and hops < max_hops:
        e = events_by_seq.get(current)
        if e is None:
            break
        chain.append(e)
        current = e.get("reply_to")
        hops += 1
    chain.reverse()
    return chain


def build_context(recipient: str, incoming_body: str, reply_to: Optional[int] = None) -> str:
    """Assemble the full model input for `recipient` as a single string.
    This is the only thing an adapter ever sees of project truth."""
    state = core.load_state()
    identity = load_identity(recipient)
    chain = _causal_chain(reply_to)

    parts = [
        "=== AGENT IDENTITY ===",
        identity.strip(),
        "",
        "=== CURRENT SHARED STATE ===",
        json.dumps(state, indent=2),
        "",
    ]

    if chain:
        parts.append("=== RELEVANT RECENT HISTORY (causal chain to this message) ===")
        for e in chain:
            archived_path = core.ARCHIVE_DIR / e["archived_as"]
            body = archived_path.read_text(encoding="utf-8").strip() if archived_path.exists() else "(archived file missing)"
            parts.append(
                f"--- seq {e['seq']:06d} | {e['from']} -> {e['to']} "
                f"(reply_to={e['reply_to']}) ---"
            )
            parts.append(body)
        parts.append("")

    parts.append("=== MESSAGE TO RESPOND TO ===")
    parts.append(incoming_body.strip())

    return "\n".join(parts)
