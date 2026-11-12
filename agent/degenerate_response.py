"""Conservative detection for HTTP-200 provider token-salad responses.

A few OpenAI-compatible routes have returned syntactically successful completions whose
text is incoherent mixed-script punctuation.  The turn loop treats a positive report as a
retryable provider failure so its existing fallback chain can recover.  This module is
pure and deliberately fail-closed: ordinary non-English and code-heavy replies are not
classified without multiple independent corruption signals.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Any


_MIN_TEXT_LENGTH = 120
_MAX_TEXT_LENGTH = 800
_SCRIPT_WINDOW = 120


@dataclass(frozen=True)
class DegenerateResponseReport:
    """Privacy-safe detector result suitable for logs and retry diagnostics."""

    digest: str
    length: int
    parentheses: int
    unbalanced_parentheses: int
    script_count: int
    script_switches: int
    window_script_count: int
    window_script_switches: int


def _script_for(char: str) -> str | None:
    """Return a coarse Unicode script family for the scripts relevant to the incident."""
    name = unicodedata.name(char, "")
    for marker, script in (
        ("LATIN", "latin"),
        ("GREEK", "greek"),
        ("CYRILLIC", "cyrillic"),
        ("ARABIC", "arabic"),
        ("HEBREW", "hebrew"),
        ("ARMENIAN", "armenian"),
        ("HIRAGANA", "japanese"),
        ("KATAKANA", "japanese"),
        ("CJK UNIFIED", "han"),
        ("HANGUL", "korean"),
        ("DEVANAGARI", "devanagari"),
        ("THAI", "thai"),
    ):
        if marker in name:
            return script
    return None


def _switches(labels: list[str]) -> int:
    return sum(left != right for left, right in zip(labels, labels[1:]))


def detect_degenerate_response(content: Any, tool_calls: Any = None) -> DegenerateResponseReport | None:
    """Return a report for the known token-salad shape, otherwise ``None``.

    Tool-bearing responses are always trusted: providers may legitimately combine source
    snippets, JSON, and several scripts while requesting an action.  Text-only responses
    require a short-ish payload plus either strongly imbalanced parenthetical noise or
    unusually dense script switching alongside punctuation noise.
    """
    if tool_calls or not isinstance(content, str):
        return None
    length = len(content)
    if not _MIN_TEXT_LENGTH <= length <= _MAX_TEXT_LENGTH:
        return None

    labels = [_script_for(char) for char in content]
    written = [label for label in labels if label]
    script_count = len(set(written))
    script_switches = _switches(written)
    parentheses = content.count("(") + content.count(")")
    unbalanced = abs(content.count("(") - content.count(")"))

    max_window_scripts = 0
    max_window_switches = 0
    for start in range(max(1, length - _SCRIPT_WINDOW + 1)):
        window = [label for label in labels[start : start + _SCRIPT_WINDOW] if label]
        max_window_scripts = max(max_window_scripts, len(set(window)))
        max_window_switches = max(max_window_switches, _switches(window))

    suspicious = (
        (parentheses >= 20 and unbalanced >= 6)
        or (parentheses >= 16 and script_switches >= 8)
        or (parentheses >= 10 and max_window_scripts >= 4 and max_window_switches >= 8)
    )
    if not suspicious:
        return None
    return DegenerateResponseReport(
        digest=hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:16],
        length=length,
        parentheses=parentheses,
        unbalanced_parentheses=unbalanced,
        script_count=script_count,
        script_switches=script_switches,
        window_script_count=max_window_scripts,
        window_script_switches=max_window_switches,
    )
