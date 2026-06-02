#!/usr/bin/env python3
"""Process Webex messages with a local Ollama model.

The default behavior is safe: read messages, generate a Japanese summary and
reply draft, and print the result. Use --send only when you intentionally want
to post the generated output back to Webex.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from webex_api import DEFAULT_BASE_URL, WebexError, get_token, request


DEFAULT_MODEL = "qwen3.5:4b"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_STATE = ".webex-agent-state.json"


class OllamaError(RuntimeError):
    """Raised when Ollama cannot produce a response."""


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def message_text(message: dict[str, Any]) -> str:
    return message.get("markdown") or message.get("text") or ""


def format_messages(messages: list[dict[str, Any]]) -> str:
    lines = []
    for message in messages:
        sender = message.get("personEmail") or message.get("personId") or "unknown"
        created = message.get("created") or "unknown-time"
        text = message_text(message).strip()
        lines.append(f"- [{created}] {sender}: {text}")
    return "\n".join(lines)


def build_prompt(messages: list[dict[str, Any]], mode: str) -> str:
    formatted = format_messages(messages)
    if mode == "reply":
        task = (
            "最後のメッセージに対する返信案を1つ作ってください。"
            "丁寧すぎず、仕事のチャットとして自然な日本語にしてください。"
        )
    elif mode == "classify":
        task = (
            "メッセージを分類してください。分類は urgent, needs_reply, fyi, ignore のいずれかです。"
            "理由と次のアクションも短く書いてください。"
        )
    else:
        task = (
            "会話の要点、決定事項、未対応タスク、返信案を短くまとめてください。"
            "返信不要なら返信案は「不要」と書いてください。"
        )

    return f"""あなたは日本語に強いWebexメッセージ処理エージェントです。
Webexの会話を読み、事実に基づいて簡潔に処理してください。
推測が必要な場合は、その旨を明記してください。

タスク:
{task}

Webexメッセージ:
{formatted}

出力は日本語で、見出し付きの短い箇条書きにしてください。
"""


def call_ollama(model: str, prompt: str, ollama_url: str, timeout: int) -> str:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.2,
        },
    }
    url = f"{ollama_url.rstrip('/')}/api/chat"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise OllamaError(f"Ollama request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise OllamaError(f"Ollama timed out after {timeout} seconds") from exc

    text = data.get("message", {}).get("content")
    if not text:
        raise OllamaError(f"Ollama returned no response: {data}")
    return text.strip()


def fetch_messages(args: argparse.Namespace, token: str) -> list[dict[str, Any]]:
    result = request(
        "GET",
        "/messages",
        token=token,
        base_url=args.base_url,
        query={"roomId": args.room_id, "max": str(args.max)},
    )
    return list(reversed(result.get("items", [])))


def unseen_messages(
    messages: list[dict[str, Any]],
    state: dict[str, Any],
    room_id: str,
    include_seen: bool,
) -> list[dict[str, Any]]:
    if include_seen:
        return messages
    last_seen = state.get("rooms", {}).get(room_id, {}).get("last_message_id")
    if not last_seen:
        return messages
    for index, message in enumerate(messages):
        if message.get("id") == last_seen:
            return messages[index + 1 :]
    return messages


def update_last_seen(state: dict[str, Any], room_id: str, messages: list[dict[str, Any]]) -> None:
    if not messages:
        return
    state.setdefault("rooms", {}).setdefault(room_id, {})["last_message_id"] = messages[-1].get("id")
    state["rooms"][room_id]["last_seen_at"] = messages[-1].get("created")


def post_message(args: argparse.Namespace, token: str, text: str) -> None:
    request(
        "POST",
        "/messages",
        token=token,
        base_url=args.base_url,
        body={"roomId": args.room_id, "markdown": text},
    )


def process_once(args: argparse.Namespace, state: dict[str, Any]) -> bool:
    token = get_token(args)
    messages = fetch_messages(args, token)
    batch = unseen_messages(messages, state, args.room_id, args.include_seen)
    if not batch:
        if args.verbose:
            print("No new Webex messages.")
        return False

    if args.ignore_self:
        me = request("GET", "/people/me", token=token, base_url=args.base_url)
        my_email = me.get("emails", [None])[0]
        batch = [message for message in batch if message.get("personEmail") != my_email]
        if not batch:
            if args.verbose:
                print("Only self-authored messages were found.")
            update_last_seen(state, args.room_id, messages)
            return False

    prompt = build_prompt(batch, args.mode)
    result = call_ollama(args.model, prompt, args.ollama_url, args.timeout)
    print(result)
    if args.send:
        post_message(args, token, result)
        if args.verbose:
            print("Posted generated output to Webex.", file=sys.stderr)
    update_last_seen(state, args.room_id, messages)
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Process Webex messages with a local Ollama model.")
    parser.add_argument("--token", help="Webex bearer token. Defaults to WEBEX_ACCESS_TOKEN.")
    parser.add_argument("--base-url", default=os.environ.get("WEBEX_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--room-id", default=os.environ.get("WEBEX_ROOM_ID"), required=not os.environ.get("WEBEX_ROOM_ID"))
    parser.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL))
    parser.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL))
    parser.add_argument("--state", default=os.environ.get("WEBEX_AGENT_STATE", DEFAULT_STATE))
    parser.add_argument("--max", type=int, default=20, help="Maximum recent Webex messages to fetch.")
    parser.add_argument("--timeout", type=int, default=180, help="Ollama HTTP timeout in seconds.")
    parser.add_argument("--mode", choices=["summarize", "reply", "classify"], default="summarize")
    parser.add_argument("--include-seen", action="store_true", help="Process fetched messages even if already seen.")
    parser.add_argument("--ignore-self", action="store_true", help="Ignore messages from the authenticated Webex user.")
    parser.add_argument("--send", action="store_true", help="Post the generated output back to the same Webex room.")
    parser.add_argument("--watch", action="store_true", help="Poll repeatedly.")
    parser.add_argument("--interval", type=int, default=60, help="Polling interval for --watch.")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    state_path = Path(args.state)
    try:
        state = load_state(state_path)
        while True:
            changed = process_once(args, state)
            if changed or not state_path.exists():
                save_state(state_path, state)
            if not args.watch:
                break
            time.sleep(args.interval)
    except (WebexError, OllamaError) as exc:
        print(f"webex_ollama_agent.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
