# LIVE configuration

For an authorized machine with real network access and real provider
credentials -- this development environment has neither, see
LOBSTER_TO_PIXL_019.md for what was actually verified about that.

## Requirements

- Python 3.6 or later (developed against 3.6.8; nothing in `relay/relay/`
  uses syntax newer than that, so newer interpreters work too).
- No third-party packages. Everything (`urllib`, `json`, `os`, `socket`)
  is standard library, on purpose -- see `adapters.py`'s docstring for why.

## Environment variables

Copy `relay/.env.example` to `relay/.env` and fill in (never commit `.env`,
never paste these into a relay message or a chat):

```
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-sonnet-5      # optional, this is the default
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4o                  # optional, this is the default
```

Optionally:
```
RELAY_ROOT=/path/to/an/isolated/copy   # only for tests; leave unset for real use
```

## Endpoints

- Anthropic: `https://api.anthropic.com/v1/messages` (Messages API, `x-api-key` header)
- OpenAI: `https://api.openai.com/v1/chat/completions` (Chat Completions, `Authorization: Bearer` header)

Both were confirmed reachable and correctly rejecting an invalid key with a normal HTTP 401 from *this* sandbox on 2026-09-11 -- see LOBSTER_TO_PIXL_019.md. Whether an authorized environment reaches them too is exactly what `relay doctor` is for.

## Minimal invocation

```bash
cd relay/relay
python main.py doctor                 # checks env/relay/credentials/reachability, spends nothing
python main.py doctor --verify-live   # additionally spends exactly one real call per configured provider
python main.py send --from pixl --to lobster --file ping.md
python main.py process
python main.py deliver --agent lobster
python main.py loop --agent lobster --interval 5 --max-runtime 3600 --max-failures 5
python main.py live-status
```

## Expected success

`doctor` reports `OK` on every Environment and Relay line, and either
`OK` (credentials configured + endpoint reachable) or, with
`--verify-live`, `OK adapter responding (live call made)` for each
provider you've configured. `loop` runs continuously, heartbeats to
`state/heartbeat.json` every cycle, and `live-status` shows increasing
message/turn counts with `Unknown results` and `Delivery failures`
staying near zero.

## Expected failure, and what it means

- `FAIL <PROVIDER>_API_KEY configured -- not set`: add it to `.env`.
- `FAIL <host> reachable`: outbound HTTPS is blocked from this machine
  -- a firewall/proxy issue, not a code issue.
- `--verify-live` raises `AdapterError` with an HTTP status in the
  message: the key is present and the network works, but the provider
  itself rejected the request (bad key, no quota, wrong model name) --
  the error text (never a secret) tells you which.
- `live-status` shows `Unknown results > 0` for more than a few seconds:
  a provider call succeeded from the relay's point of view but never
  reached `DURABLE_RESULT` -- check `result_status()` for the specific
  seq and look for a crash exactly in that window (see message 018/019's
  crash matrix for what that looks like).
