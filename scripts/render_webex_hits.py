#!/usr/bin/env python3
"""Render selected Webex hits from a local private store."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-store", required=True)
    parser.add_argument("--selected-hits")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    private_store = json.loads(Path(args.private_store).read_text(encoding="utf-8"))
    by_id = {item["hit_id"]: item for item in private_store.get("items", [])}

    if args.selected_hits:
        selected = json.loads(Path(args.selected_hits).read_text(encoding="utf-8"))
        ids = selected.get("selected_hit_ids", [])
    else:
        ids = list(by_id)

    items = [by_id[hit_id] for hit_id in ids if hit_id in by_id]
    if args.json:
        print(json.dumps({"run_id": private_store.get("run_id"), "items": items}, ensure_ascii=False, indent=2))
        return 0

    if not items:
        print("No selected messages found.")
        return 0
    for item in items:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
