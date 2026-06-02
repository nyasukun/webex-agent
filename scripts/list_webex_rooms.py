#!/usr/bin/env python3
"""List Webex rooms with their ROOM_ID values."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from webex_api import DEFAULT_BASE_URL, WebexError, get_token, request


def room_type(room: dict[str, Any]) -> str:
    value = room.get("type")
    return value if isinstance(value, str) else "-"


def room_title(room: dict[str, Any]) -> str:
    title = room.get("title")
    return title if isinstance(title, str) and title else "(direct space)"


def print_table(rooms: list[dict[str, Any]]) -> None:
    rows = [(room_title(room), room_type(room), room.get("id", "")) for room in rooms]
    title_width = max([len("TITLE"), *(len(row[0]) for row in rows)], default=5)
    type_width = max([len("TYPE"), *(len(row[1]) for row in rows)], default=4)
    print(f"{'TITLE'.ljust(title_width)}  {'TYPE'.ljust(type_width)}  ROOM_ID")
    print(f"{'-' * title_width}  {'-' * type_width}  {'-' * 7}")
    for title, typ, room_id in rows:
        print(f"{title.ljust(title_width)}  {typ.ljust(type_width)}  {room_id}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List Webex ROOM_ID values.")
    parser.add_argument("--token", help="Webex bearer token. Defaults to WEBEX_ACCESS_TOKEN.")
    parser.add_argument("--base-url", default=os.environ.get("WEBEX_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--type", choices=["direct", "group"], help="Filter by room type.")
    parser.add_argument("--max", type=int, default=100)
    parser.add_argument("--sort-by", choices=["id", "lastactivity", "created"], default="lastactivity")
    parser.add_argument("--ids-only", action="store_true", help="Print only ROOM_ID values.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    query = {"max": str(args.max), "sortBy": args.sort_by}
    if args.type:
        query["type"] = args.type
    try:
        result = request("GET", "/rooms", token=get_token(args), base_url=args.base_url, query=query)
    except WebexError as exc:
        print(f"list_webex_rooms.py: {exc}", file=sys.stderr)
        return 1

    rooms = result.get("items", [])
    if args.ids_only:
        for room in rooms:
            print(room.get("id", ""))
    else:
        print_table(rooms)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
