# Local Webex Agent Architecture

## Goal

Process Webex messages locally and avoid sending sensitive message content to a
cloud LLM.

## Recommended Shape

Do not let a language model choose or execute tools. Use deterministic local
code for Webex operations, and use a local model only for language tasks over
already-selected text.

```text
User request
  -> local command / local UI
  -> deterministic intent routing
  -> local Webex API client
  -> local search / local cache
  -> local Ollama summarizer / classifier / reply drafter
  -> validated response
  -> optional human-approved Webex send
```

## Components

- `webex_api.py`: Webex API access. Owns credentials and HTTP calls.
- `list_webex_rooms.py`: Lists room titles and IDs.
- `search_webex_messages.py`: Fetches and searches messages over a date range.
- `webex_ollama_agent.py`: Processes recent messages with a local Ollama model.
- `output_validator`: Accepts only a small JSON contract from the model.

## Boundary

- Webex tokens stay in environment variables.
- Python code performs Webex API calls.
- Ollama receives selected message text only.
- The model never receives Webex credentials.
- Sending generated output back to Webex requires an explicit `--send`.

## Cloud Parser Handoff

When a cloud coding agent is used as the interactive frontend, keep raw Webex
content out of the cloud context by splitting search into two artifacts:

```text
Local private store
  run_id
  hit_id -> Webex room_id, message_id, author, timestamp, raw_text, local_reason

Cloud-safe handoff
  run_id
  hit_id, score
```

The cloud agent may parse, sort, group, or choose from the cloud-safe handoff,
but it must never receive raw message text, sender names, room names, local
reasons, Webex IDs, or tokens.

Suggested flow:

```text
1. Local script fetches Webex messages.
2. Local script sends raw candidate text to Ollama only.
3. Ollama returns minimal scored output:
     [{"hit_id": "h_001", "score": 0.82}]
4. Local script stores private metadata:
     h_001 -> raw_text, author, room_id, message_id, local_reason
5. Local script emits only cloud-safe scored IDs.
6. Cloud agent parses/sorts the scored IDs.
7. Cloud agent returns selected hit_ids only.
8. Local script resolves hit_ids from the private store and renders the final
   user-facing answer locally.
```

Important: the cloud agent should not see Webex `message_id` values either if
they could be considered sensitive. Use a per-run opaque `hit_id`, and keep the
mapping in a local file or SQLite database excluded from git.

Example cloud-safe handoff:

```json
{
  "run_id": "run_20260602_001",
  "items": [
    {"hit_id": "h_001", "score": 0.82},
    {"hit_id": "h_002", "score": 0.61}
  ]
}
```

Example cloud response:

```json
{
  "run_id": "run_20260602_001",
  "selected_hit_ids": ["h_001", "h_002"]
}
```

The local renderer then joins `selected_hit_ids` against the private store and
prints raw text, author, room, timestamp, and local reason for the user.

## Ollama Calling Pattern

For Qwen thinking models, use Ollama's chat API with `think: false`. The generate
API can spend the token budget on hidden thinking and return an empty response.

```json
{
  "model": "qwen3.5:4b",
  "messages": [{"role": "user", "content": "..."}],
  "stream": false,
  "think": false,
  "format": "json",
  "options": {
    "temperature": 0.1,
    "num_predict": 700
  }
}
```

## Prompt Contract

The model receives a narrow prompt and should return JSON only.

```json
{
  "task": "summarize",
  "confidence": 0.9,
  "summary": ["..."],
  "actions": ["..."],
  "reply_draft": "...",
  "needs_human": true
}
```

The scripts tolerate common local-model deviations, such as returning a top-level
array for search matches or string values for numeric scores.

For semantic search, prefer an even smaller model contract:

```json
[
  {"hit_id": "h_001", "score": 0.82},
  {"hit_id": "h_002", "score": 0.14}
]
```

Keep reasons out of this model output when a cloud parser will be involved. If a
reason is needed, generate or store it locally and reveal it only in the final
local render step.

## Safety Rules

- Search can run automatically.
- Sending messages requires `--send`.
- Destructive actions require exact IDs and user confirmation.
- Keep `.env`, state files, local databases, and caches out of git.

## Local Contract Tests

No real Webex calls are made by these tests.

```bash
python3 scripts/simulate_agent_contract.py
python3 scripts/test_ollama_contract.py --model qwen3.5:4b --json-mode
```
