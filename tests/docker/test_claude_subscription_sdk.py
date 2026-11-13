"""Claude subscription dependency smoke test for the published image."""
from __future__ import annotations

import subprocess


def test_claude_agent_sdk_is_baked_at_reviewed_version(built_image: str) -> None:
    """The immutable Python 3.13 image must import the exact reviewed SDK."""
    r = subprocess.run(
        [
            "docker", "run", "--rm", "--entrypoint", "/opt/hermes/.venv/bin/python",
            built_image,
            "-c",
            (
                "import sys; from importlib.metadata import version; "
                "import claude_agent_sdk; "
                "assert sys.version_info[:2] == (3, 13); "
                "assert version('claude-agent-sdk') == '0.2.159'"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, (
        "claude-agent-sdk image import failed: "
        f"stdout={r.stdout[-2000:]!r} stderr={r.stderr[-2000:]!r}"
    )
