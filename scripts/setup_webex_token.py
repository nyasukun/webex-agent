#!/usr/bin/env python3
"""Open Webex token docs, read a token from stdin, and save it to .env."""

from __future__ import annotations

import argparse
import getpass
import os
import stat
import sys
import webbrowser
from pathlib import Path

from webex_api import TOKEN_DOC_URL


def parse_env(path: Path) -> list[tuple[str, str | None]]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            entries.append((line, None))
            continue
        key, value = line.split("=", 1)
        entries.append((key.strip(), value))
    return entries


def quote_env(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def write_token(path: Path, token: str) -> None:
    entries = parse_env(path)
    updated = False
    lines = []
    for key, value in entries:
        if value is None:
            lines.append(key)
            continue
        if key == "WEBEX_ACCESS_TOKEN":
            lines.append(f"WEBEX_ACCESS_TOKEN={quote_env(token)}")
            updated = True
        else:
            lines.append(f"{key}={value}")
    if not updated:
        lines.append(f"WEBEX_ACCESS_TOKEN={quote_env(token)}")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def read_token(args: argparse.Namespace) -> str:
    if args.token_stdin:
        token = sys.stdin.read().strip()
    elif args.show_input:
        token = input("Paste Webex access token: ").strip()
    else:
        token = getpass.getpass("Paste Webex access token: ").strip()
    if not token:
        raise SystemExit("No token provided.")
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description="Save a Webex access token to .env.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the token documentation URL.")
    parser.add_argument("--token-stdin", action="store_true", help="Read the token from stdin.")
    parser.add_argument("--show-input", action="store_true", help="Show token input while typing.")
    args = parser.parse_args()

    if not args.no_browser:
        print(f"Opening Webex token documentation: {TOKEN_DOC_URL}", file=sys.stderr)
        webbrowser.open(TOKEN_DOC_URL)
    else:
        print(f"Webex token documentation: {TOKEN_DOC_URL}", file=sys.stderr)

    token = read_token(args)
    env_path = Path(args.env_file)
    write_token(env_path, token)
    print(f"Saved WEBEX_ACCESS_TOKEN to {env_path}")
    print("Verify with: python3 scripts/webex_api.py me")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
