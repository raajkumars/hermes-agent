#!/usr/bin/env python3
"""Fail a post-deployment check when Kanban dependency promotion is stranded.

This intentionally reads the deployed Hermes API rather than opening the
deployment runner's local board database. Deploy automation can invoke it after
rolling out dispatcher changes; a nonzero exit status prevents a deployment from
being declared healthy while terminal-parent todo cards remain unpromoted.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen


class DependencyHealthAPIError(RuntimeError):
    """The deployed dependency-health endpoint could not be read safely."""


def stranded_task_count(*, api_url: str, board: Optional[str] = None, timeout: float = 10.0) -> int:
    """Return the deployed board's dependency-stranded count through Hermes API."""
    query = urlencode({"board": board}) if board else ""
    endpoint = f"{api_url.rstrip('/')}/api/plugins/kanban/health/dependencies"
    if query:
        endpoint = f"{endpoint}?{query}"
    try:
        with urlopen(endpoint, timeout=timeout) as response:
            payload = json.load(response)
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
        raise DependencyHealthAPIError(f"could not read dependency health from {endpoint}: {exc}") from exc

    count = payload.get("dependency_stranded_task_count") if isinstance(payload, dict) else None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise DependencyHealthAPIError(
            f"dependency health response from {endpoint} did not contain a non-negative integer "
            "dependency_stranded_task_count"
        )
    return count


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True, help="Base URL of the deployed Hermes API")
    parser.add_argument("--board", help="Kanban board slug (defaults to the active board)")
    parser.add_argument("--timeout", type=float, default=10.0, help="API request timeout in seconds (default: 10)")
    args = parser.parse_args(argv)

    try:
        count = stranded_task_count(api_url=args.api_url, board=args.board, timeout=args.timeout)
    except DependencyHealthAPIError as exc:
        print(f"ERROR: {exc}")
        return 2
    if count:
        print(f"FAIL: {count} dependency-stranded Kanban task(s) detected.")
        return 1
    print("PASS: no dependency-stranded Kanban tasks detected.")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())