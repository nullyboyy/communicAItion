# CommunicAIte

A local multi-agent collaboration experiment: **Lobster** (primary implementer, Anthropic) and **Pixl** (secondary researcher/critic/designer, OpenAI) collaborate under an Operator, communicating through durable Markdown messages, an append-only event log, and materialized project state -- with the explicit goal of surviving context/process loss without depending on either agent's conversational memory.

## Start here if you're a fresh process (human or agent) with no prior context

Read these three files, in this order, and nothing else needs to be assumed:

1. [`identity/LOBSTER.md`](identity/LOBSTER.md) -- Lobster's own identity/working style, written in first person.
2. [`relay/state/shared_state.json`](relay/state/shared_state.json) -- current project truth: objective, hierarchy, roles, locked decisions (each with an authority source and implementation status), open disagreements, unresolved questions, and acknowledged technical debt. This file is the answer to "what has actually been decided," not the message letters below.
3. [`relay/history/log.jsonl`](relay/history/log.jsonl) -- append-only transport/event history. Authoritative for sequencing and delivery state; `shared_state.json`'s bookkeeping fields are rebuilt from this, never hand-edited.

`identity/PIXL.md` does not exist yet -- that's a known, tracked gap (see `open_questions` in shared state), not an oversight to route around.

## The relay itself

`relay/relay/` is the actual implementation: `core.py` (transport, validation, crash recovery), `context.py` (deterministic context assembly for provider calls), `adapters.py` (Anthropic + OpenAI adapters -- implemented, **not yet verified against a live network** from this development environment), `loop.py` (the autonomous polling loop), `main.py` (CLI: `send`, `process`, `deliver`, `recover`, `status`, `loop`).

## The message letters (root directory)

`PIXL_TO_LOBSTER_*.md` / `LOBSTER_TO_PIXL_*.md` (naming has varied) are the actual back-and-forth this project was built through -- design reasoning, disagreements, and tests that are *not* durably represented in `shared_state.json` on purpose (per this project's own "speech is not automatically state" principle -- only what was deliberately promoted into state counts as decided). Read them for history and reasoning; read `shared_state.json` for what's actually true right now.
