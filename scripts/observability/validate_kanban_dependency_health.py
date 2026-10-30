#!/usr/bin/env python3
"""Fail a post-deployment check when Kanban dependency promotion is stranded.

This intentionally reads the deployed Hermes API rather than opening the
runner's local board database. The API credential is read only from a named
environment variable and is never rendered in status or error output.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import os
from pathlib import Path
import sys
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kanban_dependency_health_client import DependencyHealthClient, DependencyHealthClientError


DependencyHealthAPIError = DependencyHealthClientError


def stranded_task_count(
    *,
    api_url: str,
    bearer_credential: str,
    board: Optional[str] = None,
    timeout: float = 10.0,
    cache_ttl: float = 30.0,
) -> int:
    """Return the deployed board's dependency-stranded count through Hermes API."""
    return DependencyHealthClient(
        api_url=api_url,
        bearer_credential=bearer_credential,
        timeout=timeout,
        cache_ttl=cache_ttl,
    ).stranded_task_count(board=board)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True, help="Base URL of the deployed Hermes API")
    parser.add_argument("--credential-env", default="HERMES_API_CREDENTIAL", help="Environment variable holding the API credential")
    parser.add_argument("--board", help="Kanban board slug (defaults to the active board)")
    parser.add_argument("--timeout", type=float, default=10.0, help="API request timeout in seconds (default: 10)")
    parser.add_argument("--cache-ttl", type=float, default=30.0, help="In-process cache TTL in seconds (default: 30)")
    args = parser.parse_args(argv)
    credential = os.environ.get(args.credential_env, "")
    if not credential:
        print(f"ERROR: required API credential environment variable {args.credential_env} is not set.")
        return 2

    try:
        count = stranded_task_count(
            api_url=args.api_url,
            bearer_credential=credential,
            board=args.board,
            timeout=args.timeout,
            cache_ttl=args.cache_ttl,
        )
    except (DependencyHealthAPIError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    if count:
        print(f"FAIL: {count} dependency-stranded Kanban task(s) detected.")
        return 1
    print("PASS: no dependency-stranded Kanban tasks detected.")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
