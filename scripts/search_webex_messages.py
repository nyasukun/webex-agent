#!/usr/bin/env python3
"""Search Webex messages over a date range.

This script can search by simple local keywords, or ask a local Ollama model to
judge semantic relevance against a user's context. Webex API calls are made by
Python; the local LLM never receives credentials or tool authority.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from webex_api import DEFAULT_BASE_URL, WebexError, get_token, request


DEFAULT_MODEL = "qwen3.5:4b"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"


@dataclass
class MessageHit:
    room_id: str
    room_title: str
    message_id: str
    created: str
    sender: str
    text: str
    score: float
    reason: str


def status(args: argparse.Namespace, message: str) -> None:
    if not getattr(args, "quiet", False):
        print(message, file=sys.stderr)


def parse_time(value: str | None, default: datetime) -> datetime:
    if value is None:
        return default
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        parsed_date = date.fromisoformat(value)
        return datetime.combine(parsed_date, datetime.min.time()).astimezone()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def webex_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def message_text(message: dict[str, Any]) -> str:
    return (message.get("markdown") or message.get("text") or "").strip()


def message_sender(message: dict[str, Any]) -> str:
    return message.get("personEmail") or message.get("personId") or "unknown"


def parse_created(message: dict[str, Any]) -> datetime | None:
    created = message.get("created")
    if not isinstance(created, str):
        return None
    try:
        return datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_rooms(args: argparse.Namespace, token: str) -> list[dict[str, Any]]:
    if args.room_id:
        rooms = []
        for room_id in args.room_id:
            try:
                room = request("GET", f"/rooms/{room_id}", token=token, base_url=args.base_url)
            except WebexError:
                room = {"id": room_id, "title": room_id, "type": "unknown"}
            rooms.append(room)
        explicit_rooms = rooms
    else:
        explicit_rooms = []

    if args.room_id and not args.include_direct and not args.room_title:
        rooms = dedupe_rooms(explicit_rooms)
        status(args, f"Selected rooms: {len(rooms)}")
        for room in rooms:
            status(args, f"  - {room.get('title') or '(direct space)'} [{room.get('type', '-')}]")
        return rooms

    query = {"max": str(args.max_rooms), "sortBy": args.room_sort_by}
    if args.room_type:
        query["type"] = args.room_type
    result = request("GET", "/rooms", token=token, base_url=args.base_url, query=query)
    discovered_rooms = result.get("items", [])

    if args.include_direct or args.room_title:
        discovered_rooms = filter_rooms(discovered_rooms, args)
    elif args.room_id:
        discovered_rooms = []

    rooms = dedupe_rooms([*explicit_rooms, *discovered_rooms])
    status(args, f"Selected rooms: {len(rooms)}")
    for room in rooms:
        status(args, f"  - {room.get('title') or '(direct space)'} [{room.get('type', '-')}]")
    return rooms


def dedupe_rooms(rooms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    unique = []
    for room in rooms:
        room_id = room.get("id")
        if not room_id or room_id in seen:
            continue
        seen.add(room_id)
        unique.append(room)
    return unique


def filter_rooms(rooms: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    titles = set(args.room_title or [])
    selected = []
    for room in rooms:
        is_direct = room.get("type") == "direct"
        title = room.get("title") or ""
        if args.include_direct and is_direct:
            selected.append(room)
            continue
        if title in titles:
            selected.append(room)
    return selected


def fetch_room_messages(
    args: argparse.Namespace,
    token: str,
    room: dict[str, Any],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    before: str | None = None
    pages = 0
    while pages < args.max_pages_per_room:
        pages += 1
        if args.verbose:
            print(
                f"Fetching room={room.get('title') or room['id']} page={pages} before={before}",
                file=sys.stderr,
            )
        result = request(
            "GET",
            "/messages",
            token=token,
            base_url=args.base_url,
            query={
                key: value
                for key, value in {
                    "roomId": room["id"],
                    "before": before,
                    "max": str(args.page_size),
                }.items()
                if value is not None
            },
        )
        items = result.get("items", [])
        if not items:
            break

        oldest: datetime | None = None
        for item in items:
            created = parse_created(item)
            if created is None:
                continue
            oldest = created if oldest is None or created < oldest else oldest
            if start <= created <= end:
                messages.append(item)

        if oldest is None or oldest < start:
            break
        before = webex_time(oldest)
        time.sleep(args.page_delay)
    return list(reversed(messages))


def keyword_terms(args: argparse.Namespace) -> list[str]:
    terms = list(args.keyword or [])
    if args.keyword_from_context:
        terms.extend(term for term in re.split(r"\s+", args.context.strip()) if term)
    return [term.lower() for term in terms if term]


def keyword_filter(messages: list[MessageHit], terms: list[str], mode: str) -> list[MessageHit]:
    if not terms:
        return messages
    filtered = []
    for hit in messages:
        haystack = f"{hit.room_title}\n{hit.sender}\n{hit.text}".lower()
        matched = [term for term in terms if term in haystack]
        if (mode == "all" and len(matched) == len(terms)) or (mode == "any" and matched):
            hit.score = max(hit.score, 0.4 + min(0.5, 0.1 * len(matched)))
            hit.reason = f"keyword match: {', '.join(matched)}"
            filtered.append(hit)
    return filtered


def ngrams(text: str, min_size: int = 2, max_size: int = 4) -> set[str]:
    normalized = re.sub(r"\s+", "", text.lower())
    grams = set()
    for size in range(min_size, max_size + 1):
        grams.update(normalized[index : index + size] for index in range(0, max(0, len(normalized) - size + 1)))
    return {gram for gram in grams if gram}


def local_rank_filter(hits: list[MessageHit], context: str, limit: int) -> list[MessageHit]:
    if limit <= 0 or len(hits) <= limit:
        return hits
    context_grams = ngrams(context)
    if not context_grams:
        return hits[:limit]
    ranked = []
    for hit in hits:
        text_grams = ngrams(f"{hit.room_title}{hit.sender}{hit.text}")
        overlap = len(context_grams & text_grams)
        score = overlap / max(1, len(context_grams))
        if score > 0:
            hit.score = max(hit.score, score)
            hit.reason = hit.reason or f"local ngram overlap: {overlap}"
        ranked.append((score, hit.created, hit))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [hit for _, _, hit in ranked[:limit]]


def build_hits(room: dict[str, Any], messages: list[dict[str, Any]]) -> list[MessageHit]:
    title = room.get("title") or "(direct space)"
    hits = []
    for message in messages:
        text = message_text(message)
        if not text:
            continue
        hits.append(
            MessageHit(
                room_id=room["id"],
                room_title=title,
                message_id=message.get("id", ""),
                created=message.get("created", ""),
                sender=message_sender(message),
                text=text,
                score=0.0,
                reason="",
            )
        )
    return hits


def chunked(items: list[MessageHit], size: int) -> list[list[MessageHit]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def build_semantic_prompt(context: str, hits: list[MessageHit]) -> str:
    payload = [
        {
            "id": hit.message_id,
            "room_title": hit.room_title,
            "created": hit.created,
            "sender": hit.sender,
            "text": hit.text,
        }
        for hit in hits
    ]
    return f"""あなたはWebexメッセージ検索専用の日本語アシスタントです。
ツールは使えません。コマンドも実行できません。
下のmessagesだけを根拠に、user_contextに対する各メッセージの関連度を採点してください。
機微情報の内容を理由に長く引用しないでください。

必ずJSONオブジェクトだけを返してください。Markdownは禁止です。
schema:
{{
  "matches": [
    {{"id": "message-id", "score": 0.0, "reason": "短い理由"}}
  ]
}}

ルール:
- messagesに含まれる全idについて、必ず1件ずつmatchesに入れてください。
- scoreは0.0から1.0の数値です。
- 関連が薄い場合もscore 0.0から0.2程度で返してください。
- idはmessagesのidを完全にコピーしてください。

user_context: {context}
messages:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def call_ollama(prompt: str, args: argparse.Namespace) -> dict[str, Any]:
    body = {
        "model": args.model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "format": "json",
        "options": {
            "temperature": 0.1,
            "num_predict": args.num_predict,
        },
    }
    req = urllib.request.Request(
        f"{args.ollama_url.rstrip('/')}/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=args.ollama_timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    content = data.get("message", {}).get("content", "")
    if args.verbose:
        print(f"Ollama raw content: {content}", file=sys.stderr)
    parsed = parse_json_value(content)
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        return {"matches": parsed}
    return {"matches": []}


def parse_json_value(content: str) -> Any:
    content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(content):
        if char not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict | list):
            return value
    return None


def semantic_filter(hits: list[MessageHit], args: argparse.Namespace) -> list[MessageHit]:
    by_id = {hit.message_id: hit for hit in hits}
    selected: list[MessageHit] = []
    rejected: list[MessageHit] = []
    batches = chunked(hits, args.semantic_batch_size)
    for index, batch in enumerate(batches, start=1):
        status(args, f"Ollama semantic batch {index}/{len(batches)} ({len(batch)} messages)")
        prompt = build_semantic_prompt(args.context, batch)
        try:
            result = call_ollama(prompt, args)
        except (TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
            print(f"search_webex_messages.py: Ollama batch failed: {exc}", file=sys.stderr)
            continue
        matches = result.get("matches", [])
        status(args, f"  Ollama returned matches: {len(matches) if isinstance(matches, list) else 0}")
        for item in matches if isinstance(matches, list) else []:
            if not isinstance(item, dict):
                continue
            message_id = item.get("id")
            hit = by_id.get(message_id)
            if hit is None:
                if args.verbose:
                    print(f"Ollama returned unknown message id: {message_id}", file=sys.stderr)
                continue
            score = parse_score(item.get("score", 0))
            if score is None:
                if args.verbose:
                    print(f"Ollama returned non-numeric score for {message_id}: {item.get('score')}", file=sys.stderr)
                continue
            hit.score = score
            hit.reason = str(item.get("reason", "semantic match"))
            if hit.score < args.min_score:
                rejected.append(hit)
                continue
            selected.append(hit)
    if args.show_rejected and rejected:
        status(args, f"Rejected semantic matches below min-score ({args.min_score}): {len(rejected)}")
        rejected = sorted(rejected, key=lambda hit: (hit.score, hit.created), reverse=True)[: args.show_rejected]
        for hit in rejected:
            status(args, f"  [{hit.score:.2f}] {hit.created} {hit.sender}: {hit.reason}")
            if not args.hide_text:
                status(args, f"    {hit.text[:200].replace(chr(10), ' ')}")
    return selected


def parse_score(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def print_hits(hits: list[MessageHit], args: argparse.Namespace) -> None:
    hits = sorted(hits, key=lambda hit: (hit.score, hit.created), reverse=True)[: args.limit]
    if not hits:
        print("No matching messages found.")
        return
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "room_title": hit.room_title,
                        "room_id": hit.room_id,
                        "message_id": hit.message_id,
                        "created": hit.created,
                        "author": hit.sender,
                        "sender": hit.sender,
                        "score": hit.score,
                        "reason": hit.reason,
                        "raw_text": None if args.hide_text else hit.text,
                        "text": None if args.hide_text else hit.text,
                    }
                    for hit in hits
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    for hit in hits:
        print(f"[{hit.score:.2f}] {hit.created}  {hit.room_title}")
        print(f"  author: {hit.sender}")
        print(f"  room_id: {hit.room_id}")
        print(f"  message_id: {hit.message_id}")
        if hit.reason:
            print(f"  reason: {hit.reason}")
        if not args.hide_text:
            text = hit.text
            if args.snippet_chars and args.snippet_chars > 0:
                text = text[: args.snippet_chars]
            print("  raw_text:")
            for line in text.splitlines() or [""]:
                print(f"    {line}")
        print()


def build_parser() -> argparse.ArgumentParser:
    now = datetime.now().astimezone()
    parser = argparse.ArgumentParser(description="Search Webex messages over an arbitrary date range.")
    parser.add_argument("context", help="User context or natural-language search request.")
    parser.add_argument("--token", help="Webex bearer token. Defaults to WEBEX_ACCESS_TOKEN.")
    parser.add_argument("--base-url", default=os.environ.get("WEBEX_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--start", help="Start date/time. Default: --days days ago. Example: 2026-06-01")
    parser.add_argument("--end", help="End date/time. Default: now.")
    parser.add_argument("--days", type=int, default=30, help="Search this many days back when --start is omitted.")
    parser.add_argument("--room-id", action="append", help="Limit search to a room. Repeatable.")
    parser.add_argument("--room-title", action="append", help="Limit search to an exact room title. Repeatable.")
    parser.add_argument("--include-direct", action="store_true", help="Include all direct-message rooms.")
    parser.add_argument("--room-type", choices=["direct", "group"], help="Filter rooms when searching all rooms.")
    parser.add_argument("--max-rooms", type=int, default=100)
    parser.add_argument("--room-sort-by", choices=["id", "lastactivity", "created"], default="lastactivity")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages-per-room", type=int, default=20)
    parser.add_argument("--page-delay", type=float, default=0.1)
    parser.add_argument("--keyword", action="append", help="Local keyword prefilter. Repeatable.")
    parser.add_argument("--keyword-from-context", action="store_true", help="Use whitespace terms from context as keywords.")
    parser.add_argument("--keyword-mode", choices=["any", "all"], default="any")
    parser.add_argument("--semantic", action="store_true", help="Use local Ollama semantic relevance judging.")
    parser.add_argument(
        "--semantic-candidates",
        type=int,
        default=60,
        help="Locally rank and send only this many candidates to Ollama. Use 0 to disable.",
    )
    parser.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL))
    parser.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL))
    parser.add_argument("--ollama-timeout", type=int, default=240)
    parser.add_argument("--semantic-batch-size", type=int, default=12)
    parser.add_argument("--num-predict", type=int, default=900)
    parser.add_argument("--min-score", type=float, default=0.55)
    parser.add_argument("--show-rejected", type=int, default=5, help="Show this many semantic matches below --min-score.")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--hide-text", action="store_true", help="Do not print message text.")
    parser.add_argument("--snippet-chars", type=int, default=0, help="Limit raw text length. Default: 0 means full text.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress messages.")
    parser.set_defaults(default_start=now - timedelta(days=30), default_end=now)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    end = parse_time(args.end, args.default_end)
    default_start = end - timedelta(days=args.days)
    start = parse_time(args.start, default_start)
    if start > end:
        print("search_webex_messages.py: --start must be before --end", file=sys.stderr)
        return 1

    try:
        token = get_token(args)
        status(args, f"Search window: {webex_time(start)} to {webex_time(end)}")
        rooms = fetch_rooms(args, token)
        hits: list[MessageHit] = []
        for room in rooms:
            if "id" not in room:
                continue
            status(args, f"Fetching messages: {room.get('title') or room['id']}")
            messages = fetch_room_messages(args, token, room, start, end)
            status(args, f"  fetched in range: {len(messages)}")
            hits.extend(build_hits(room, messages))
    except WebexError as exc:
        print(f"search_webex_messages.py: {exc}", file=sys.stderr)
        return 1

    hits = keyword_filter(hits, keyword_terms(args), args.keyword_mode)
    if args.semantic:
        status(args, f"Candidate messages before local rank: {len(hits)}")
        hits = local_rank_filter(hits, args.context, args.semantic_candidates)
        status(args, f"Candidate messages sent to Ollama: {len(hits)}")
        hits = semantic_filter(hits, args)
    elif not keyword_terms(args):
        for hit in hits:
            hit.score = 0.1
            hit.reason = "within date range"

    print_hits(hits, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
