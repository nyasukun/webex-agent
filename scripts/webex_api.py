#!/usr/bin/env python3
"""Small Webex REST API CLI.

Set WEBEX_ACCESS_TOKEN to a Webex personal access token, bot token, or OAuth
access token before running commands.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "https://webexapis.com/v1"


class WebexError(RuntimeError):
    """Raised when the Webex API returns an error or cannot be reached."""


def load_json(value: str | None) -> Any:
    if value is None:
        return None
    if value == "-":
        return json.load(sys.stdin)
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON: {exc}") from exc


def parse_kv(values: list[str] | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise SystemExit(f"Expected KEY=VALUE, got: {value}")
        key, val = value.split("=", 1)
        parsed[key] = val
    return parsed


def compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


def request(
    method: str,
    path: str,
    *,
    token: str,
    base_url: str,
    query: dict[str, str] | None = None,
    body: Any = None,
) -> Any:
    url = build_url(base_url, path, query)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = compact(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise WebexError(f"{exc.code} {exc.reason}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise WebexError(f"Request failed: {exc.reason}") from exc


def build_url(base_url: str, path: str, query: dict[str, str] | None) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        url = path
    else:
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"

    if query:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urllib.parse.urlencode(query)}"
    return url


def get_token(args: argparse.Namespace) -> str:
    token = args.token or os.environ.get("WEBEX_ACCESS_TOKEN")
    if not token:
        raise SystemExit("Set WEBEX_ACCESS_TOKEN or pass --token.")
    return token


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--token", help="Webex bearer token. Defaults to WEBEX_ACCESS_TOKEN.")
    parser.add_argument("--base-url", default=os.environ.get("WEBEX_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--raw", action="store_true", help="Print compact JSON instead of pretty JSON.")


def print_result(result: Any, raw: bool) -> None:
    if result is None:
        return
    print(compact(result) if raw else pretty(result))


def cmd_request(args: argparse.Namespace) -> None:
    query = parse_kv(args.query)
    body = load_json(args.json)
    result = request(
        args.method,
        args.path,
        token=get_token(args),
        base_url=args.base_url,
        query=query,
        body=body,
    )
    print_result(result, args.raw)


def cmd_me(args: argparse.Namespace) -> None:
    result = request("GET", "/people/me", token=get_token(args), base_url=args.base_url)
    print_result(result, args.raw)


def cmd_rooms_list(args: argparse.Namespace) -> None:
    query = {
        key: str(value)
        for key, value in {
            "type": args.type,
            "sortBy": args.sort_by,
            "max": args.max,
        }.items()
        if value is not None
    }
    result = request("GET", "/rooms", token=get_token(args), base_url=args.base_url, query=query)
    print_result(result, args.raw)


def cmd_rooms_create(args: argparse.Namespace) -> None:
    result = request(
        "POST",
        "/rooms",
        token=get_token(args),
        base_url=args.base_url,
        body={"title": args.title},
    )
    print_result(result, args.raw)


def cmd_rooms_delete(args: argparse.Namespace) -> None:
    result = request("DELETE", f"/rooms/{args.room_id}", token=get_token(args), base_url=args.base_url)
    print_result(result, args.raw)


def cmd_messages_list(args: argparse.Namespace) -> None:
    query = {
        key: str(value)
        for key, value in {
            "roomId": args.room_id,
            "mentionedPeople": args.mentioned_people,
            "before": args.before,
            "beforeMessage": args.before_message,
            "max": args.max,
        }.items()
        if value is not None
    }
    result = request("GET", "/messages", token=get_token(args), base_url=args.base_url, query=query)
    print_result(result, args.raw)


def cmd_messages_send(args: argparse.Namespace) -> None:
    body: dict[str, Any] = {}
    if args.room_id:
        body["roomId"] = args.room_id
    if args.to_person_email:
        body["toPersonEmail"] = args.to_person_email
    if args.text:
        body["text"] = args.text
    if args.markdown:
        body["markdown"] = args.markdown
    if args.files:
        body["files"] = args.files
    if "roomId" not in body and "toPersonEmail" not in body:
        raise SystemExit("Pass --room-id or --to-person-email.")
    if "text" not in body and "markdown" not in body and "files" not in body:
        raise SystemExit("Pass --text, --markdown, or --file.")

    result = request("POST", "/messages", token=get_token(args), base_url=args.base_url, body=body)
    print_result(result, args.raw)


def cmd_messages_delete(args: argparse.Namespace) -> None:
    result = request(
        "DELETE",
        f"/messages/{args.message_id}",
        token=get_token(args),
        base_url=args.base_url,
    )
    print_result(result, args.raw)


def cmd_memberships_list(args: argparse.Namespace) -> None:
    query = {"roomId": args.room_id}
    if args.max is not None:
        query["max"] = str(args.max)
    result = request("GET", "/memberships", token=get_token(args), base_url=args.base_url, query=query)
    print_result(result, args.raw)


def cmd_memberships_add(args: argparse.Namespace) -> None:
    body: dict[str, Any] = {
        "roomId": args.room_id,
        "personEmail": args.person_email,
        "isModerator": args.moderator,
    }
    result = request("POST", "/memberships", token=get_token(args), base_url=args.base_url, body=body)
    print_result(result, args.raw)


def cmd_memberships_delete(args: argparse.Namespace) -> None:
    result = request(
        "DELETE",
        f"/memberships/{args.membership_id}",
        token=get_token(args),
        base_url=args.base_url,
    )
    print_result(result, args.raw)


def cmd_people_search(args: argparse.Namespace) -> None:
    query = {}
    if args.email:
        query["email"] = args.email
    if args.display_name:
        query["displayName"] = args.display_name
    if args.max is not None:
        query["max"] = str(args.max)
    if not query:
        raise SystemExit("Pass --email or --display-name.")
    result = request("GET", "/people", token=get_token(args), base_url=args.base_url, query=query)
    print_result(result, args.raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operate Webex through the REST API.")
    add_common(parser)
    subparsers = parser.add_subparsers(required=True)

    req = subparsers.add_parser("request", help="Call an arbitrary Webex API endpoint.")
    req.add_argument("method", choices=["GET", "POST", "PUT", "PATCH", "DELETE"])
    req.add_argument("path", help="API path such as /rooms, or a full URL.")
    req.add_argument("--query", action="append", help="Query parameter as KEY=VALUE. Repeatable.")
    req.add_argument("--json", help="JSON request body, or '-' to read JSON from stdin.")
    req.set_defaults(func=cmd_request)

    me = subparsers.add_parser("me", help="Show the authenticated Webex identity.")
    me.set_defaults(func=cmd_me)

    rooms = subparsers.add_parser("rooms", help="List, create, or delete spaces.")
    rooms_sub = rooms.add_subparsers(required=True)
    rooms_list = rooms_sub.add_parser("list", help="List spaces.")
    rooms_list.add_argument("--type", choices=["direct", "group"])
    rooms_list.add_argument("--sort-by", choices=["id", "lastactivity", "created"])
    rooms_list.add_argument("--max", type=int)
    rooms_list.set_defaults(func=cmd_rooms_list)
    rooms_create = rooms_sub.add_parser("create", help="Create a group space.")
    rooms_create.add_argument("title")
    rooms_create.set_defaults(func=cmd_rooms_create)
    rooms_delete = rooms_sub.add_parser("delete", help="Delete a space.")
    rooms_delete.add_argument("room_id")
    rooms_delete.set_defaults(func=cmd_rooms_delete)

    messages = subparsers.add_parser("messages", help="List, send, or delete messages.")
    messages_sub = messages.add_subparsers(required=True)
    messages_list = messages_sub.add_parser("list", help="List messages in a space.")
    messages_list.add_argument("--room-id", required=True)
    messages_list.add_argument("--mentioned-people")
    messages_list.add_argument("--before")
    messages_list.add_argument("--before-message")
    messages_list.add_argument("--max", type=int)
    messages_list.set_defaults(func=cmd_messages_list)
    messages_send = messages_sub.add_parser("send", help="Send a message.")
    messages_send.add_argument("--room-id")
    messages_send.add_argument("--to-person-email")
    messages_send.add_argument("--text")
    messages_send.add_argument("--markdown")
    messages_send.add_argument("--file", dest="files", action="append", help="Public file URL. Repeatable.")
    messages_send.set_defaults(func=cmd_messages_send)
    messages_delete = messages_sub.add_parser("delete", help="Delete a message.")
    messages_delete.add_argument("message_id")
    messages_delete.set_defaults(func=cmd_messages_delete)

    memberships = subparsers.add_parser("memberships", help="List, add, or delete space memberships.")
    memberships_sub = memberships.add_subparsers(required=True)
    memberships_list = memberships_sub.add_parser("list", help="List members in a space.")
    memberships_list.add_argument("--room-id", required=True)
    memberships_list.add_argument("--max", type=int)
    memberships_list.set_defaults(func=cmd_memberships_list)
    memberships_add = memberships_sub.add_parser("add", help="Add a person to a space.")
    memberships_add.add_argument("--room-id", required=True)
    memberships_add.add_argument("--person-email", required=True)
    memberships_add.add_argument("--moderator", action="store_true")
    memberships_add.set_defaults(func=cmd_memberships_add)
    memberships_delete = memberships_sub.add_parser("delete", help="Remove a membership.")
    memberships_delete.add_argument("membership_id")
    memberships_delete.set_defaults(func=cmd_memberships_delete)

    people = subparsers.add_parser("people", help="Search people.")
    people_sub = people.add_subparsers(required=True)
    people_search = people_sub.add_parser("search", help="Search people by email or display name.")
    people_search.add_argument("--email")
    people_search.add_argument("--display-name")
    people_search.add_argument("--max", type=int)
    people_search.set_defaults(func=cmd_people_search)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except WebexError as exc:
        print(f"webex_api.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
