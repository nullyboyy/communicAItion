"""
Relay core: transport + bookkeeping only.

No model calls happen here. This module proves the primitives:
immutable message files, atomic writes, an append-only event log,
and a materialized current-state snapshot. Agent adapters (adapters.py)
are stubbed and deliberately not wired in yet -- that's the next milestone.

Design notes (answers to Pixl's implementation questions in message 004):

1. Language: Python. Reasoning lives in the letter, not here.
2. Drop-file convention (what a sender creates in an outbox):
       {from}_to_{to}[_r{reply_seq:06d}]__{uid}.md
   The sender never claims a sequence number. `uid` is a short random
   token so two drops in the same second never collide on a filename.
3. Sequence numbers are assigned exclusively by the relay, under a lock,
   at processing time -- never self-assigned by an agent. This is what
   makes concurrent origination safe: whoever calls process() first wins
   the lock, increments last_seq by exactly one, and renames the file
   into archive/ with the canonical name. No two files can ever be
   renamed to the same canonical name because the counter only advances
   inside the lock.
4. Event log (history/log.jsonl) is the historical authority -- append
   only, one JSON object per line, never rewritten.
5. shared_state.json is a materialized snapshot derived from the log.
   If the two ever disagree, the log wins; shared_state.json can always
   be rebuilt by replaying the log.
6. Malformed = filename doesn't match the drop convention, `from` doesn't
   match the outbox it was found in, `to` isn't the other known agent,
   body is empty after stripping, or `reply_to` names a sequence number
   that was never archived for this agent pair.
7. Retry / processed / failed / retrying are defined here as a status
   enum even though nothing produces `retrying` yet -- that state only
   exists once adapters.py makes real calls that can transiently fail.
       processed  -> delivered, response captured (or, this milestone,
                     transport completed), archived under its canonical name.
       retrying   -> an adapter call failed but retries remain; message
                     stays out of archive, event logged, tried again next run.
       failed     -> retries exhausted, or the message was malformed;
                     moved to archive/failed/, never silently dropped.
8. Credentials never touch shared_state.json or the log. They live in
   environment variables (see .env.example at the project root); this
   module never reads or writes them, so there is nothing to redact.
"""
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import adapters

AGENTS = ("lobster", "pixl")

ROOT = Path(__file__).resolve().parent.parent  # .../relay
MESSAGES_DIR = ROOT / "messages"
ARCHIVE_DIR = MESSAGES_DIR / "archive"
FAILED_DIR = ARCHIVE_DIR / "failed"
STATE_PATH = ROOT / "state" / "shared_state.json"
LOG_PATH = ROOT / "history" / "log.jsonl"
LOCK_PATH = ROOT / "state" / ".seq.lock"

# Drop convention: sender-authored filename, no sequence number.
# uid is a millisecond timestamp (13 zero-padded decimal digits) followed
# by 4 random hex chars for tie-breaking. This is deliberate: process_once()
# globs a directory and sorts filenames lexicographically to decide
# processing order, so the uid's leading digits must sort chronologically
# or two messages dropped close together can be assigned sequence numbers
# out of creation order. A prior version used a pure random 8-hex uid and
# reproducibly got this wrong on the first real test (see message 006/007).
DROP_RE = re.compile(
    r"^(?P<from>lobster|pixl)_to_(?P<to>lobster|pixl)"
    r"(?:_r(?P<reply_to>\d{6}))?"
    r"__(?P<uid>[0-9a-f]{17})\.md$"
)

# Canonical archived filename: relay-assigned, includes sequence number.
CANONICAL_TMPL = "{seq:06d}_{frm}_to_{to}{reply_part}.md"


class MalformedMessage(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class LogCorruption(Exception):
    """Raised when a non-final line of log.jsonl fails to parse. A
    corrupted line anywhere but the end is not something recovery
    guesses its way past -- see read_log_safe()."""


class ParsedDrop:
    def __init__(self, path: Path, frm: str, to: str, reply_to: Optional[int], uid: str):
        self.path = path
        self.frm = frm
        self.to = to
        self.reply_to = reply_to
        self.uid = uid


def outbox_dir(agent: str) -> Path:
    return MESSAGES_DIR / f"outbox_{agent}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_uid() -> str:
    # 13-digit millisecond timestamp (sorts chronologically as a string
    # until the year ~2286) + 4 random hex chars to break same-millisecond ties.
    return "%013d%s" % (int(time.time() * 1000), secrets.token_hex(2))


def make_drop_filename(frm: str, to: str, reply_to: Optional[int] = None) -> str:
    reply_part = f"_r{reply_to:06d}" if reply_to is not None else ""
    return f"{frm}_to_{to}{reply_part}__{new_uid()}.md"


def atomic_write_text(path: Path, content: str) -> None:
    """Write-then-rename so a reader never observes a partial file."""
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)  # atomic on both Windows and POSIX


def atomic_write_json(path: Path, data: dict) -> None:
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")


def append_event(event: dict) -> None:
    """Append-only. Never opened in a mode that could truncate."""
    line = json.dumps(event, ensure_ascii=False)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state() -> dict:
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def default_state() -> dict:
    """Schema v3 (message 012). Adds hierarchy/roles/technical_debt/
    implementation_status/continuity/protocol -- the categories message
    011's fresh-process test showed were durably empty despite being
    real, agreed, and in several cases already implemented in code.
    These are still just empty defaults for a fresh install; the actual
    populated content lives only in state/shared_state.json on disk,
    written deliberately, not derived from this function."""
    return {
        "version": 3,
        "objective": "",
        "hierarchy": "",
        "roles": {a: "" for a in AGENTS},
        "constraints": [],
        "facts": [],
        "decisions": [],
        "disagreements": [],
        "open_questions": [],
        "risks": [],
        "research_needed": [],
        "technical_debt": [],
        "implementation_status": {},
        "continuity": {},
        "protocol": {"vocabulary": [], "compression_level": 0, "rule": ""},
        "last_seq": 0,
        "last_processed_seq": {a: 0 for a in AGENTS},
    }


def read_log_safe() -> "tuple[List[dict], bool]":
    """Read log.jsonl, tolerating exactly one thing: a truncated or
    corrupted FINAL line, which a crash mid-write can produce (the
    write is a plain append, not atomic-by-rename, because an
    append-only file can't be rewritten via tmp+rename without
    becoming rewritable, which defeats the point).

    A corrupted line anywhere else raises LogCorruption -- that's real
    data damage, not a torn write, and guessing how to repair it is
    exactly the "silently invent the missing event" failure mode this
    is supposed to prevent. Returns (events, truncated_line_dropped).
    """
    if not LOG_PATH.exists():
        return [], False
    raw_lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    events = []
    truncated = False
    for i, line in enumerate(raw_lines):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(raw_lines) - 1:
                truncated = True
                break
            raise LogCorruption(
                f"log.jsonl line {i + 1} of {len(raw_lines)} is corrupted "
                f"and is NOT the final line -- refusing to guess a repair: {line!r}"
            )
    return events, truncated


def max_committed_seq(events: Optional[List[dict]] = None) -> int:
    """The next sequence number always comes from the log, never from
    the cached shared_state.json -- if state is stale after a crash,
    trusting it here could hand out a sequence number that's already
    committed in the log."""
    if events is None:
        events, _ = read_log_safe()
    return max((e["seq"] for e in events if e.get("event") == "message_processed"), default=0)


def _repair_truncated_log(events: List[dict]) -> None:
    """The one sanctioned rewrite of log.jsonl. A plain append_event()
    would leave the dropped bad line sitting in the middle of the file
    once something new gets appended after it -- read_log_safe() would
    then see it as a NON-final corruption and refuse to proceed. So
    this atomically rewrites the file to contain exactly the valid
    events plus one explicit recovery marker, via the same
    write-tmp-then-rename pattern as every other file here. `events`
    must already exclude the bad tail line; it's mutated in place to
    append the marker so the caller's in-memory view matches disk."""
    marker = {
        "event": "log_recovery", "ts": now_iso(),
        "detail": "dropped a truncated/corrupt final log.jsonl line",
    }
    lines = [json.dumps(e, ensure_ascii=False) for e in events] + [json.dumps(marker, ensure_ascii=False)]
    atomic_write_text(LOG_PATH, "\n".join(lines) + "\n")
    events.append(marker)


def rebuild_state_from_log() -> dict:
    """shared_state.json's bookkeeping fields (last_seq,
    last_processed_seq) are a materialized cache of the log, never
    trusted on their own -- this is the only place that writes them.
    Human-curated fields (objective/decisions/facts/etc) are NOT
    derivable from the log and are preserved as-is; the log has no
    opinion about them because nothing automated ever writes them
    (see LOBSTER_TO_PIXL_009.md section on speech vs. state)."""
    events, truncated = read_log_safe()
    if truncated:
        _repair_truncated_log(events)  # rewrites the file once; idempotent after that

    processed = [e for e in events if e.get("event") == "message_processed"]

    try:
        state = load_state()
    except FileNotFoundError:
        state = default_state()

    state["last_seq"] = max((e["seq"] for e in processed), default=0)
    last_processed = {a: 0 for a in AGENTS}
    for e in processed:
        last_processed[e["from"]] = max(last_processed[e["from"]], e["seq"])
    state["last_processed_seq"] = last_processed

    atomic_write_json(STATE_PATH, state)
    return state


def send_message(frm: str, to: str, body: str, reply_to: Optional[int] = None) -> Path:
    """Drop a new outgoing message. Callable by either agent's side of
    the collaboration (or, right now, by us on their behalf) without
    ever touching the sequence counter -- that only happens in process()."""
    if frm not in AGENTS or to not in AGENTS or frm == to:
        raise ValueError(f"invalid from/to: {frm} -> {to}")
    if not body.strip():
        raise ValueError("refusing to send an empty message body")
    filename = make_drop_filename(frm, to, reply_to)
    path = outbox_dir(frm) / filename
    atomic_write_text(path, body)
    return path


class SeqLock:
    """Exclusive-create spinlock so sequence assignment is safe even if
    two relay processes run concurrently later. O_CREAT|O_EXCL is atomic
    on both platforms; the loser spins until the winner removes the lock."""

    def __init__(self, path: Path, timeout: float = 5.0, poll: float = 0.02):
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self._fd = None

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                return self
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not acquire {self.path} within {self.timeout}s")
                time.sleep(self.poll)

    def __exit__(self, *exc):
        if self._fd is not None:
            os.close(self._fd)
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass


def _parse_drop(path: Path, expected_from: str) -> ParsedDrop:
    m = DROP_RE.match(path.name)
    if not m:
        raise MalformedMessage(f"filename does not match drop convention: {path.name}")
    frm, to, reply_to, uid = m.group("from"), m.group("to"), m.group("reply_to"), m.group("uid")
    if frm != expected_from:
        raise MalformedMessage(
            f"message in outbox_{expected_from} claims from={frm}"
        )
    if to == frm:
        raise MalformedMessage("from and to are the same agent")
    body = path.read_text(encoding="utf-8")
    if not body.strip():
        raise MalformedMessage("empty body")
    return ParsedDrop(
        path=path,
        frm=frm,
        to=to,
        reply_to=int(reply_to) if reply_to is not None else None,
        uid=uid,
    )


def _validate_reply_to(reply_to: Optional[int], state: dict) -> None:
    if reply_to is None:
        return
    highest_known = max(state["last_processed_seq"].values(), default=0)
    if reply_to > highest_known or reply_to < 1:
        raise MalformedMessage(
            f"reply_to={reply_to} does not reference an already-processed message "
            f"(highest known seq is {highest_known})"
        )


def _relpath(path: Path) -> str:
    return str(path.resolve().relative_to(ROOT))


def _reject(path: Path, reason: str) -> dict:
    """Log-ahead, same as process_once()'s success path: commit the
    intent to the log before touching the filesystem, so a crash
    between the two leaves evidence recover() can finish, instead of a
    file silently sitting in archive/failed/ with nothing in the log
    explaining why (or vice versa)."""
    dest = FAILED_DIR / path.name
    event = {
        "event": "message_rejected",
        "ts": now_iso(),
        "raw_file": path.name,
        "reason": reason,
        "status": "failed",
        "source_path": _relpath(path),
        "dest_path": _relpath(dest),
    }
    append_event(event)          # 1. commit
    if path.exists():
        os.replace(path, dest)   # 2. effect (idempotent: skipped if already applied)
    return event


def _apply_missing_effects(events: List[dict]) -> List[dict]:
    """The actual recovery mechanism. For every committed event that
    has (source_path, dest_path), check whether the effect already
    happened; if not, redo it from the recorded source. This is what
    makes restart safe against a crash landing between 'log says it
    happened' and 'the file actually moved': the log always commits
    first, so this function is the only place effects get applied,
    and it's idempotent -- calling it when nothing is missing is a
    no-op, calling it twice does not double-apply anything.

    Returns a list of {"archived_as"/"raw_file": ..., "status": "reapplied"|"unrecoverable"}."""
    report = []
    for e in events:
        if e.get("event") not in ("message_processed", "message_rejected"):
            continue
        if "dest_path" not in e or "source_path" not in e:
            continue  # events logged before this recovery mechanism existed
        dest = ROOT / e["dest_path"]
        if dest.exists():
            continue  # effect already applied -- nothing to do, by design
        source = ROOT / e["source_path"]
        label = e.get("archived_as") or e.get("raw_file")
        if source.exists():
            os.replace(source, dest)
            report.append({"target": label, "status": "reapplied"})
        else:
            report.append({"target": label, "status": "unrecoverable"})
    return report


def recover() -> dict:
    """The smallest mechanism that makes the invariants demonstrable:
    1. read the log, tolerating a torn final line (read_log_safe)
    2. finish any effect the log says happened but the filesystem
       doesn't yet show (_apply_missing_effects)
    3. rebuild shared_state.json purely from the log's committed events

    Idempotent by construction: run it zero, one, or a hundred times
    with nothing wrong and every step is a no-op. This is what
    process_once() and deliver_pending() call before doing anything
    else, so restart-safety isn't a special mode you invoke -- it's
    the normal first step of every operation."""
    events, truncated = read_log_safe()
    if truncated:
        _repair_truncated_log(events)
    reapplied = _apply_missing_effects(events)
    if reapplied:
        append_event({
            "event": "recovery_applied", "ts": now_iso(),
            "effects": reapplied, "truncated_line_dropped": truncated,
        })
    state = rebuild_state_from_log()
    return {"truncated_line_dropped": truncated, "effects": reapplied, "state": state}


def process_once() -> List[dict]:
    """Reconcile first (recover()), then scan both outboxes once. For
    each genuinely new pending file: validate, commit it to the log
    (before any filesystem effect), then apply the effect. No adapter
    calls. Returns the list of events produced, in the order they
    happened -- recovery-only reapplications are not included, since
    nothing new happened from the caller's point of view."""
    recover()  # heal any half-committed effect from a prior crash before looking for new work

    events: List[dict] = []
    for agent in AGENTS:
        for path in sorted(outbox_dir(agent).glob("*.md")):
            append_event({"event": "message_discovered", "ts": now_iso(), "file": path.name})
            try:
                drop = _parse_drop(path, expected_from=agent)
            except MalformedMessage as e:
                events.append(_reject(path, e.reason))  # message_rejected == validation_failed
                continue

            with SeqLock(LOCK_PATH):
                # recover() may have run before the lock was acquired by a
                # concurrent process; check again now that we hold it.
                log_events, _ = read_log_safe()
                try:
                    _validate_reply_to(drop.reply_to, {
                        "last_processed_seq": _last_processed_seq_from(log_events)
                    })
                except MalformedMessage as e:
                    events.append(_reject(path, e.reason))
                    continue

                append_event({"event": "message_validated", "ts": now_iso(), "file": drop.path.name})
                seq = max_committed_seq(log_events) + 1
                reply_part = f"_r{drop.reply_to:06d}" if drop.reply_to is not None else ""
                canonical_name = CANONICAL_TMPL.format(
                    seq=seq, frm=drop.frm, to=drop.to, reply_part=reply_part
                )
                dest = ARCHIVE_DIR / canonical_name

                event = {
                    "event": "message_processed",
                    "ts": now_iso(),
                    "seq": seq,
                    "from": drop.frm,
                    "to": drop.to,
                    "reply_to": drop.reply_to,
                    "archived_as": canonical_name,
                    "status": "processed",
                    "source_path": _relpath(drop.path),
                    "dest_path": _relpath(dest),
                }
                append_event(event)          # 1. COMMIT -- log first
                os.replace(drop.path, dest)  # 2. EFFECT -- still inside the lock
                rebuild_state_from_log()     # 3. CACHE -- derived, never hand-mutated
                events.append(event)
    return events


def _last_processed_seq_from(events: List[dict]) -> dict:
    result = {a: 0 for a in AGENTS}
    for e in events:
        if e.get("event") == "message_processed":
            result[e["from"]] = max(result[e["from"]], e["seq"])
    return result


def tail_events(n: int = 20) -> List[dict]:
    events, _ = read_log_safe()
    return events[-n:]


DEFAULT_BACKOFF = (1, 2, 4)


def _reply_already_attempted(agent_name: str, seq: int) -> bool:
    """Closes a residual race the log alone can't: send_message() drops
    the reply file, THEN the message_delivered event is appended (see
    deliver_pending below) -- deliberately in that order, so a crash
    can't log a delivery that never produced a file. But that still
    leaves a small window: if the process dies after the file is
    written and before the event is appended, the log doesn't yet know
    delivery happened, and a naive retry would call the adapter again,
    producing a second reply draft for the same message. This checks
    the filesystem directly (both the pending outbox and the archive)
    for a file that already claims to be a reply to `seq`, independent
    of whether the log recorded it yet. Not a full transaction --
    still a real (much smaller) window between the adapter call
    returning and this check being consulted next time -- but it
    closes the specific gap that a log-only check can't."""
    marker = f"_r{seq:06d}"
    prefix = f"{agent_name}_to_"
    for d in (outbox_dir(agent_name), ARCHIVE_DIR):
        for p in d.glob("*.md"):
            if marker in p.name and p.name.startswith(prefix):
                return True
    return False


def deliver_pending(agent_name: str, agent: "adapters.AnthropicAgent", backoff=DEFAULT_BACKOFF) -> List[dict]:
    """Find archived, processed messages addressed to `agent_name` that
    have no reply yet, ask `agent` to answer each, and drop the reply
    into the sender's outbox (unnumbered -- process_once() assigns its
    real sequence next run).

    Idempotency has two layers: the log (a message_delivered marker, a
    processed reply whose reply_to names it, or a terminal
    message_delivery_failed record -- that last one is deliberate, like
    a malformed message, an exhausted delivery is not silently retried,
    it requires a human to look) and, because the log alone has a real
    gap, a filesystem check (_reply_already_attempted) for a reply file
    that already exists but hasn't made it into the log yet.

    Retries happen here, not inside the adapter: the adapter does one
    attempt and raises AdapterError; this function owns backoff and
    logging, per the "relay owns orchestration" boundary.
    """
    import context  # local import: context.py imports core, so importing
                     # it at module load time would be circular.

    recover()  # same reconciliation process_once() runs -- cheap, idempotent, do it first

    all_events, _ = read_log_safe()
    delivered = {e["seq"] for e in all_events if e.get("event") == "message_delivered"}
    failed = {e["seq"] for e in all_events if e.get("event") == "message_delivery_failed"}
    replied_to = {
        e["reply_to"] for e in all_events
        if e.get("event") == "message_processed" and e.get("reply_to") is not None
    }
    already_handled = delivered | failed | replied_to

    pending = [
        e for e in all_events
        if e.get("event") == "message_processed"
        and e["to"] == agent_name
        and e["seq"] not in already_handled
        and not _reply_already_attempted(agent_name, e["seq"])
    ]

    results = []
    for e in pending:
        seq = e["seq"]
        incoming_path = ARCHIVE_DIR / e["archived_as"]
        incoming_body = incoming_path.read_text(encoding="utf-8")
        assembled = context.build_context(agent_name, incoming_body, reply_to=seq)

        # Deliberately does not log the assembled context itself, or any
        # credential -- only that a turn started and for which message.
        append_event({"event": "agent_turn_started", "ts": now_iso(), "seq": seq, "agent": agent_name})

        reply_text = None
        last_error = None
        for attempt, delay in enumerate((0,) + tuple(backoff), start=1):
            if delay:
                append_event({
                    "event": "message_retrying", "ts": now_iso(),
                    "seq": seq, "to": agent_name, "attempt": attempt, "delay_s": delay,
                })
                time.sleep(delay)
            append_event({
                "event": "provider_request_started", "ts": now_iso(),
                "seq": seq, "agent": agent_name, "attempt": attempt,
                "provider": getattr(agent, "provider_name", type(agent).__name__),
            })
            try:
                reply_text = agent.send(assembled)
                append_event({
                    "event": "provider_request_succeeded", "ts": now_iso(),
                    "seq": seq, "agent": agent_name, "attempt": attempt,
                })
                break
            except adapters.AdapterError as err:
                last_error = str(err)
                append_event({
                    "event": "provider_request_failed", "ts": now_iso(),
                    "seq": seq, "agent": agent_name, "attempt": attempt, "reason": last_error,
                })

        if reply_text is None:
            event = {
                "event": "message_delivery_failed", "ts": now_iso(),
                "seq": seq, "to": agent_name, "reason": last_error, "status": "failed",
            }
            append_event(event)
            results.append(event)
            continue

        # This is the boundary message 016 section 11 asks to measure: the
        # provider has already answered (provider_request_succeeded is
        # durable) but the reply file below does not exist yet. A crash in
        # this exact gap loses the answer with no record of what it was --
        # see LOBSTER_TO_PIXL_017.md for a real, induced measurement of it.
        try:
            reply_path = send_message(agent_name, e["from"], reply_text, reply_to=seq)
        except Exception as err:
            event = {
                "event": "reply_stage_failed", "ts": now_iso(),
                "seq": seq, "to": agent_name, "reason": str(err), "status": "failed",
            }
            append_event(event)
            results.append(event)
            continue

        append_event({
            "event": "reply_staged", "ts": now_iso(),
            "seq": seq, "agent": agent_name, "reply_draft": reply_path.name,
        })
        event = {
            "event": "message_delivered", "ts": now_iso(),
            "seq": seq, "to": agent_name, "reply_draft": reply_path.name, "status": "delivered",
        }
        append_event(event)
        results.append(event)

    return results
