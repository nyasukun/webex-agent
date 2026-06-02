#!/usr/bin/env python3
"""Privacy-preserving Webex search orchestrator.

This script keeps raw Webex data local. It creates:

- a private store containing raw message data and local reasons
- a cloud-safe handoff containing only run_id, hit_id, score, and rank_hint

Optionally it can call a headless parser command to parse the cloud-safe handoff.
The headless command never receives raw text.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import search_webex_messages as search
from webex_api import WebexError, get_token


def new_run_id() -> str:
    return f"run_{datetime.now().astimezone().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def red(text: str) -> str:
    return f"\033[31m{text}\033[0m"


def make_hit_id(index: int) -> str:
    return f"h_{index:04d}"


def build_search_args(args: argparse.Namespace) -> argparse.Namespace:
    parser = search.build_parser()
    search_args = parser.parse_args([args.context])
    for name in [
        "token",
        "base_url",
        "start",
        "end",
        "days",
        "room_id",
        "room_title",
        "include_direct",
        "room_type",
        "max_rooms",
        "room_sort_by",
        "page_size",
        "max_pages_per_room",
        "page_delay",
        "keyword",
        "keyword_from_context",
        "keyword_mode",
        "semantic_candidates",
        "model",
        "ollama_url",
        "ollama_timeout",
        "semantic_batch_size",
        "num_predict",
        "min_score",
        "show_rejected",
        "limit",
        "hide_text",
        "snippet_chars",
        "verbose",
        "quiet",
    ]:
        if hasattr(args, name):
            setattr(search_args, name, getattr(args, name))
    search_args.semantic = True
    search_args.json = False
    return search_args


def collect_hits(args: argparse.Namespace) -> list[search.MessageHit]:
    search_args = build_search_args(args)
    end = search.parse_time(search_args.end, search_args.default_end)
    start = search.parse_time(search_args.start, end - search.timedelta(days=search_args.days))
    if start > end:
        raise SystemExit("--start must be before --end")

    token = get_token(search_args)
    search.status(search_args, f"Search window: {search.webex_time(start)} to {search.webex_time(end)}")
    rooms = search.fetch_rooms(search_args, token)
    hits: list[search.MessageHit] = []
    for room in rooms:
        if "id" not in room:
            continue
        search.status(search_args, f"Fetching messages: {room.get('title') or room['id']}")
        messages = search.fetch_room_messages(search_args, token, room, start, end)
        search.status(search_args, f"  fetched in range: {len(messages)}")
        hits.extend(search.build_hits(room, messages))

    hits = search.keyword_filter(hits, search.keyword_terms(search_args), search_args.keyword_mode)
    search.status(search_args, f"Candidate messages before local rank: {len(hits)}")
    hits = search.local_rank_filter(hits, search_args.context, search_args.semantic_candidates)
    search.status(search_args, f"Candidate messages sent to Ollama: {len(hits)}")
    hits = search.semantic_filter(hits, search_args)
    hits = sorted(hits, key=lambda hit: (hit.score, hit.created), reverse=True)[: search_args.limit]
    return hits


def write_artifacts(
    args: argparse.Namespace,
    run_id: str,
    hits: list[search.MessageHit],
) -> tuple[Path, Path]:
    private_items = []
    handoff_items = []
    for index, hit in enumerate(hits, start=1):
        hit_id = make_hit_id(index)
        private_items.append(
            {
                "hit_id": hit_id,
                "score": hit.score,
                "reason": hit.reason,
                "room_title": hit.room_title,
                "room_id": hit.room_id,
                "message_id": hit.message_id,
                "created": hit.created,
                "author": hit.sender,
                "raw_text": hit.text,
            }
        )
        handoff_items.append(
            {
                "hit_id": hit_id,
                "score": hit.score,
                "rank_hint": index,
            }
        )

    private_store = {
        "run_id": run_id,
        "context": args.context,
        "items": private_items,
    }
    handoff = {
        "run_id": run_id,
        "instruction": "Select relevant hit_id values. Only use hit_id and score. Do not ask for raw text.",
        "items": handoff_items,
    }

    private_path = Path(args.private_store or f"/tmp/webex-private-{run_id}.json")
    handoff_path = Path(args.handoff_out or f"/tmp/webex-handoff-{run_id}.json")
    private_path.write_text(json.dumps(private_store, ensure_ascii=False, indent=2), encoding="utf-8")
    handoff_path.write_text(json.dumps(handoff, ensure_ascii=False, indent=2), encoding="utf-8")
    return private_path, handoff_path


def run_headless_parser(args: argparse.Namespace, handoff_path: Path, run_id: str) -> Path | None:
    if not args.headless_cmd:
        return None
    selected_path = Path(args.selected_out or f"/tmp/webex-selected-{run_id}.json")
    handoff_json = handoff_path.read_text(encoding="utf-8")
    prompt = (
        "You are selecting search hits from a cloud-safe JSON handoff. "
        "The handoff contains only hit_id and score, not raw message text. "
        "Return JSON only with this schema: "
        f'{{"run_id":"{run_id}","selected_hit_ids":["h_0001"]}}. '
        "Prefer higher scores and keep at most the requested limit. "
        "Do not request raw text. Do not use tools. "
    )
    if args.headless_limit:
        prompt += f"Selection limit: {args.headless_limit}\n"
    prompt += f"\nCloud-safe handoff JSON:\n{handoff_json}\n"
    if args.verbose:
        print(red("=== HEADLESS PARSER PROMPT BEGIN ==="), file=sys.stderr)
        print(red(prompt), file=sys.stderr)
        print(red("=== HEADLESS PARSER PROMPT END ==="), file=sys.stderr)

    command = [*shlex.split(args.headless_cmd), *(args.headless_arg or [])]
    if args.headless_prompt_as_stdin:
        result = subprocess.run(command, check=False, capture_output=True, text=True, input=prompt)
    else:
        result = subprocess.run([*command, prompt], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        if result.stdout:
            print("headless parser stdout:", file=sys.stderr)
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print("headless parser stderr:", file=sys.stderr)
            print(result.stderr, file=sys.stderr)
        raise SystemExit(f"headless parser failed with exit code {result.returncode}")

    parsed = parse_json_value(result.stdout)
    if not isinstance(parsed, dict):
        raise SystemExit("headless parser did not return a JSON object")
    selected_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    return selected_path


def parse_json_value(text: str) -> Any:
    decoder = json.JSONDecoder()
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    for index, char in enumerate(stripped):
        if char not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        return value
    return None


def render_selected(private_path: Path, selected_path: Path | None, args: argparse.Namespace) -> None:
    private_store = json.loads(private_path.read_text(encoding="utf-8"))
    items = private_store.get("items", [])
    by_id = {item["hit_id"]: item for item in items}

    if selected_path and selected_path.exists():
        selected = json.loads(selected_path.read_text(encoding="utf-8"))
        selected_ids = selected.get("selected_hit_ids", [])
    else:
        selected_ids = [item["hit_id"] for item in items]

    rendered = []
    for hit_id in selected_ids:
        item = by_id.get(hit_id)
        if item:
            rendered.append(item)

    if args.render_json:
        print(json.dumps({"run_id": private_store.get("run_id"), "items": rendered}, ensure_ascii=False, indent=2))
        return

    if not rendered:
        print("No selected messages found.")
        return
    for item in rendered:
        print(f"[{item['score']:.2f}] {item['created']}  {item['room_title']}")
        print(f"  hit_id: {item['hit_id']}")
        print(f"  author: {item['author']}")
        print(f"  message_id: {item['message_id']}")
        if item.get("reason"):
            print(f"  reason: {item['reason']}")
        print("  raw_text:")
        for line in item.get("raw_text", "").splitlines() or [""]:
            print(f"    {line}")
        print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a privacy-preserving Webex search workflow.")
    parser.add_argument("context")
    parser.add_argument("--token", help="Webex bearer token. Defaults to WEBEX_ACCESS_TOKEN.")
    parser.add_argument("--base-url", default=os.environ.get("WEBEX_BASE_URL", search.DEFAULT_BASE_URL))
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--room-id", action="append")
    parser.add_argument("--room-title", action="append")
    parser.add_argument("--include-direct", action="store_true")
    parser.add_argument("--room-type", choices=["direct", "group"])
    parser.add_argument("--max-rooms", type=int, default=100)
    parser.add_argument("--room-sort-by", choices=["id", "lastactivity", "created"], default="lastactivity")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages-per-room", type=int, default=20)
    parser.add_argument("--page-delay", type=float, default=0.1)
    parser.add_argument("--keyword", action="append")
    parser.add_argument("--keyword-from-context", action="store_true")
    parser.add_argument("--keyword-mode", choices=["any", "all"], default="any")
    parser.add_argument("--semantic", action="store_true", help="Accepted for compatibility; semantic scoring is always used.")
    parser.add_argument("--semantic-candidates", type=int, default=60)
    parser.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", search.DEFAULT_MODEL))
    parser.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", search.DEFAULT_OLLAMA_URL))
    parser.add_argument("--ollama-timeout", type=int, default=240)
    parser.add_argument("--semantic-batch-size", type=int, default=12)
    parser.add_argument("--num-predict", type=int, default=900)
    parser.add_argument("--min-score", type=float, default=0.55)
    parser.add_argument("--show-rejected", type=int, default=0)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--private-store")
    parser.add_argument("--handoff-out")
    parser.add_argument("--selected-out")
    parser.add_argument("--headless-cmd", help='Command that parses the cloud-safe handoff, e.g. "claude -p".')
    parser.add_argument("--headless-arg", action="append", help="Extra headless parser argument. Repeatable.")
    parser.add_argument("--headless-prompt-as-stdin", action="store_true", help="Send parser prompt via stdin instead of argv.")
    parser.add_argument("--headless-limit", type=int)
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument("--render-json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--hide-text", action="store_true")
    parser.add_argument("--snippet-chars", type=int, default=0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    run_id = new_run_id()
    try:
        hits = collect_hits(args)
        private_path, handoff_path = write_artifacts(args, run_id, hits)
        print(f"Private store: {private_path}", file=sys.stderr)
        print(f"Cloud-safe handoff: {handoff_path}", file=sys.stderr)
        selected_path = run_headless_parser(args, handoff_path, run_id)
        if selected_path:
            print(f"Selected hits: {selected_path}", file=sys.stderr)
        if not args.no_render:
            render_selected(private_path, selected_path, args)
    except WebexError as exc:
        print(f"safe_webex_search.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
