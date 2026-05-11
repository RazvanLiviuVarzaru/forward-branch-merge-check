#!/usr/bin/env python3

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


API_VERSION = "2022-11-28"


def api_request(token: str, method: str, url: str, payload: dict | None = None) -> int:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": API_VERSION,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 404
        if exc.code == 403:
            raise PermissionError(
                "GitHub API returned 403 while updating repository variables. "
                "Use a token that has Variables write permission for the target repository. "
                "The workflow GITHUB_TOKEN is not sufficient for this endpoint."
            ) from exc
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or update a GitHub repository variable.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--value-file", type=Path, required=True)
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY")

    if not token:
        print("GITHUB_TOKEN is required.", file=sys.stderr)
        return 1

    if not repository:
        print("GITHUB_REPOSITORY is required.", file=sys.stderr)
        return 1

    value = args.value_file.read_text(encoding="utf-8")
    base_url = f"https://api.github.com/repos/{repository}/actions/variables"

    try:
        status = api_request(
            token,
            "PATCH",
            f"{base_url}/{args.name}",
            {"name": args.name, "value": value},
        )

        if status == 404:
            api_request(
                token,
                "POST",
                base_url,
                {"name": args.name, "value": value},
            )
            print(f"Created repository variable {args.name}.")
        else:
            print(f"Updated repository variable {args.name}.")

    except PermissionError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
