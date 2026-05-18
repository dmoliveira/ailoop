#!/usr/bin/env python3
"""Create a GitHub pull request via the REST API.

This helper avoids repeating ad hoc one-off `urllib` snippets during local
maintenance or CI-style non-interactive workflows.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path


def _token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    return subprocess.check_output(["gh", "auth", "token"], text=True).strip()


def _body(args: argparse.Namespace) -> str:
    if args.body is not None:
        return args.body
    if args.body_file is not None:
        return Path(args.body_file).read_text()
    return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a GitHub PR via the REST API.")
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--title", required=True, help="PR title")
    parser.add_argument("--head", required=True, help="head ref, e.g. owner:branch")
    parser.add_argument("--base", default="main", help="base branch")
    parser.add_argument("--body", help="PR body text")
    parser.add_argument("--body-file", help="Read PR body from a file")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.body is not None and args.body_file is not None:
        parser.error("use either --body or --body-file, not both")

    token = _token()
    payload = {
        "title": args.title,
        "head": args.head,
        "base": args.base,
        "body": _body(args),
    }
    request = urllib.request.Request(
        f"https://api.github.com/repos/{args.repo}/pulls",
        data=json.dumps(payload).encode(),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-loop-github-pr-create",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.load(response)
    json.dump(data, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
