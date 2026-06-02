#!/usr/bin/env python3
"""Pseudo-test a local Webex agent contract without Webex or Ollama.

This tests the architecture where the orchestrator owns tools and the LLM only
returns constrained JSON for language tasks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


ALLOWED_TASKS = {"summarize", "classify", "reply"}
REQUIRED_KEYS = {"task", "confidence", "summary", "actions", "reply_draft", "needs_human"}


@dataclass
class TestCase:
    name: str
    user_request: str
    tool_output: dict[str, Any]
    llm_output: str
    should_accept: bool


def build_prompt(task: str, tool_output: dict[str, Any]) -> str:
    messages = json.dumps(tool_output["messages"], ensure_ascii=False, indent=2)
    return f"""あなたはWebexメッセージ処理専用の日本語アシスタントです。
ツールは使えません。コマンドも実行できません。
与えられたメッセージだけを根拠に処理してください。
出力はJSONのみです。

schema:
{{
  "task": "summarize|classify|reply",
  "confidence": 0.0,
  "summary": ["..."],
  "actions": ["..."],
  "reply_draft": "...",
  "needs_human": true
}}

task: {task}
messages:
{messages}
"""


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
        value = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def validate_model_output(value: dict[str, Any]) -> tuple[bool, str]:
    missing = REQUIRED_KEYS - set(value)
    if missing:
        return False, f"missing keys: {sorted(missing)}"
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
    return True, "ok"


def safe_agent_response(llm_output: str) -> dict[str, Any]:
    parsed = extract_json(llm_output)
    if parsed is None:
        return {
            "accepted": False,
            "reason": "no valid JSON found",
            "fallback": "モデル出力を検証できませんでした。人手確認が必要です。",
        }
    ok, reason = validate_model_output(parsed)
    if not ok:
        return {
            "accepted": False,
            "reason": reason,
            "fallback": "モデル出力の形式が不正です。人手確認が必要です。",
        }
    parsed["accepted"] = True
    return parsed


def run_tests() -> int:
    cases = [
        TestCase(
            name="clean_japanese_summary",
            user_request="昨日のインシデント対応の要点をまとめて",
            tool_output={
                "source": "local_search",
                "messages": [
                    {"created": "2026-06-01T09:10:00Z", "sender": "a@example.com", "text": "APIの500エラーが増えています。"},
                    {"created": "2026-06-01T09:22:00Z", "sender": "b@example.com", "text": "原因はデータベース接続プール枯渇でした。設定を増やして復旧しました。"},
                ],
            },
            llm_output=json.dumps(
                {
                    "task": "summarize",
                    "confidence": 0.91,
                    "summary": ["APIの500エラー増加はデータベース接続プール枯渇が原因でした。", "接続プール設定を増やして復旧しました。"],
                    "actions": ["再発防止として接続プール監視を追加する。"],
                    "reply_draft": "共有ありがとうございます。再発防止として接続プールの監視追加も進めます。",
                    "needs_human": False,
                },
                ensure_ascii=False,
            ),
            should_accept=True,
        ),
        TestCase(
            name="invented_tool_call_rejected",
            user_request="メッセージを検索して",
            tool_output={"source": "local_search", "messages": [{"text": "明日の会議は10時です。"}]},
            llm_output="./node_modules/.bin/webex-api-search --query 会議\nI will run this command.",
            should_accept=False,
        ),
        TestCase(
            name="json_with_preface_repaired",
            user_request="返信案を作って",
            tool_output={"source": "local_search", "messages": [{"text": "明日10時で大丈夫ですか？"}]},
            llm_output='了解です。JSONは以下です。\n{"task":"reply","confidence":0.82,"summary":["明日10時でよいか確認されています。"],"actions":["可否を返信する。"],"reply_draft":"はい、明日10時で大丈夫です。よろしくお願いします。","needs_human":false}',
            should_accept=True,
        ),
        TestCase(
            name="missing_human_gate_rejected",
            user_request="この返事を送って",
            tool_output={"source": "local_search", "messages": [{"text": "見積もりを送ってください。"}]},
            llm_output='{"task":"reply","confidence":1.2,"summary":["見積もり依頼です。"],"actions":["送信する。"],"reply_draft":"すぐ送ります。","needs_human":false}',
            should_accept=False,
        ),
    ]

    failures = 0
    for case in cases:
        prompt = build_prompt("summarize", case.tool_output)
        result = safe_agent_response(case.llm_output)
        accepted = bool(result.get("accepted"))
        passed = accepted == case.should_accept
        if not passed:
            failures += 1
        print(f"[{'PASS' if passed else 'FAIL'}] {case.name}")
        print(f"  user_request: {case.user_request}")
        print(f"  prompt_chars: {len(prompt)}")
        print(f"  accepted: {accepted}")
        print(f"  result: {json.dumps(result, ensure_ascii=False)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
