#!/usr/bin/env python3
"""Test real Ollama models with fake Webex tool outputs.

This never calls Webex. It sends pseudo Webex search/message results to Ollama
and validates whether the model can obey a narrow JSON-only contract.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


ALLOWED_TASKS = {"summarize", "classify", "reply"}
REQUIRED_KEYS = {"task", "confidence", "summary", "actions", "reply_draft", "needs_human"}


@dataclass
class ContractCase:
    name: str
    task: str
    user_request: str
    pseudo_tool_output: dict[str, Any]
    expected_needs_human: bool | None = None


def cases() -> list[ContractCase]:
    return [
        ContractCase(
            name="summarize_incident_japanese",
            task="summarize",
            user_request="昨日のインシデント対応の要点と残タスクをまとめて",
            pseudo_tool_output={
                "tool": "local_webex_search",
                "query": "incident OR error OR database",
                "messages": [
                    {
                        "id": "msg-001",
                        "room": "Engineering",
                        "created": "2026-06-01T09:10:00+09:00",
                        "sender": "sato@example.com",
                        "text": "APIの500エラーが増えています。影響範囲を確認します。",
                    },
                    {
                        "id": "msg-002",
                        "room": "Engineering",
                        "created": "2026-06-01T09:28:00+09:00",
                        "sender": "tanaka@example.com",
                        "text": "原因はDB接続プール枯渇でした。上限を増やして復旧しました。",
                    },
                    {
                        "id": "msg-003",
                        "room": "Engineering",
                        "created": "2026-06-01T10:05:00+09:00",
                        "sender": "sato@example.com",
                        "text": "再発防止として接続プール使用率のアラートを追加しましょう。",
                    },
                ],
            },
            expected_needs_human=False,
        ),
        ContractCase(
            name="reply_schedule_japanese",
            task="reply",
            user_request="最後のメッセージに自然な返信案を作って",
            pseudo_tool_output={
                "tool": "local_webex_messages",
                "messages": [
                    {
                        "id": "msg-101",
                        "room": "Project Room",
                        "created": "2026-06-02T13:00:00+09:00",
                        "sender": "kimura@example.com",
                        "text": "明日のレビュー会、10時開始で大丈夫ですか？難しければ午後にずらせます。",
                    }
                ],
            },
            expected_needs_human=False,
        ),
        ContractCase(
            name="classify_sensitive_request",
            task="classify",
            user_request="この依頼の優先度と注意点を分類して",
            pseudo_tool_output={
                "tool": "local_webex_messages",
                "messages": [
                    {
                        "id": "msg-201",
                        "room": "Operations",
                        "created": "2026-06-02T14:15:00+09:00",
                        "sender": "yamada@example.com",
                        "text": "非公開のプロジェクト情報と担当者情報を今日中に共有してください。外部には出さないでください。",
                    }
                ],
            },
            expected_needs_human=True,
        ),
    ]


def build_prompt(case: ContractCase) -> str:
    messages = json.dumps(case.pseudo_tool_output, ensure_ascii=False, indent=2)
    return f"""あなたはWebexメッセージ処理専用の日本語アシスタントです。
ツールは使えません。コマンドも実行できません。
Webexにはアクセスできません。下にある疑似ツール出力だけを根拠に処理してください。
機微情報を外部送信する提案は避け、人手確認が必要なら needs_human を true にしてください。

必ずJSONオブジェクトだけを返してください。前置き、Markdown、コードフェンスは禁止です。
schema:
{{
  "task": "summarize|classify|reply",
  "confidence": 0.0,
  "summary": ["..."],
  "actions": ["..."],
  "reply_draft": "...",
  "needs_human": true
}}

user_request: {case.user_request}
task: {case.task}
pseudo_tool_output:
{messages}
"""


def call_ollama(model: str, prompt: str, url: str, timeout: int, json_mode: bool) -> tuple[str, float]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 700,
        },
    }
    if json_mode:
        body["format"] = "json"
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data.get("message", {}).get("content", ""), time.monotonic() - start


def extract_json(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        candidate = stripped
    else:
        match = re.search(r"\{.*\}", stripped, flags=re.S)
        if not match:
            return None
        candidate = match.group(0)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def validate(value: dict[str, Any], expected_task: str, expected_needs_human: bool | None) -> tuple[bool, str]:
    missing = REQUIRED_KEYS - set(value)
    if missing:
        return False, f"missing keys: {sorted(missing)}"
    if value["task"] != expected_task:
        return False, f"task mismatch: expected {expected_task}, got {value['task']}"
    if value["task"] not in ALLOWED_TASKS:
        return False, f"invalid task: {value['task']}"
    if not isinstance(value["confidence"], int | float):
        return False, "confidence is not numeric"
    if not 0 <= value["confidence"] <= 1:
        return False, "confidence out of range"
    if not isinstance(value["summary"], list) or not all(isinstance(item, str) for item in value["summary"]):
        return False, "summary must be list[str]"
    if not isinstance(value["actions"], list) or not all(isinstance(item, str) for item in value["actions"]):
        return False, "actions must be list[str]"
    if not isinstance(value["reply_draft"], str):
        return False, "reply_draft must be str"
    if not isinstance(value["needs_human"], bool):
        return False, "needs_human must be bool"
    if expected_needs_human is not None and value["needs_human"] != expected_needs_human:
        return False, f"needs_human mismatch: expected {expected_needs_human}, got {value['needs_human']}"
    return True, "ok"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen3.5:4b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--json-mode", action="store_true", help="Pass format=json to Ollama.")
    parser.add_argument("--show-prompts", action="store_true")
    args = parser.parse_args()

    failures = 0
    for case in cases():
        prompt = build_prompt(case)
        if args.show_prompts:
            print(f"\n--- PROMPT {case.name} ---\n{prompt}")
        print(f"\n--- CASE {case.name} ({args.model}) ---")
        try:
            raw, elapsed = call_ollama(args.model, prompt, args.ollama_url, args.timeout, args.json_mode)
        except (TimeoutError, urllib.error.URLError) as exc:
            failures += 1
            print(f"ERROR: {type(exc).__name__}: {exc}")
            continue
        print(f"elapsed_sec: {elapsed:.1f}")
        print(f"raw_output: {raw.strip()}")
        parsed = extract_json(raw)
        if parsed is None:
            failures += 1
            print("validation: FAIL no JSON object found")
            continue
        ok, reason = validate(parsed, case.task, case.expected_needs_human)
        if not ok:
            failures += 1
        print(f"validation: {'PASS' if ok else 'FAIL'} {reason}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
