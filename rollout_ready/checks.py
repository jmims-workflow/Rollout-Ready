"""Automatic checks: plain code rules. Free, instant and always consistent."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Check


@dataclass(frozen=True)
class CheckResult:
    label: str
    passed: bool
    detail: str


def word_count(text: str) -> int:
    return len(text.split())


def _show(phrase: str) -> str:
    return "an em dash" if phrase == "\u2014" else f'"{phrase}"'


def run_check(check: Check, text: str) -> CheckResult:
    if check.type == "max_words":
        n = word_count(text)
        ok = n <= check.value
        return CheckResult(check.label, ok, f"{n} words" + ("" if ok else f", over the {check.value} limit"))

    lowered = text.lower()
    if check.type == "must_not_contain":
        found = [p for p in check.value if p.lower() in lowered]
        if found:
            return CheckResult(check.label, False, "Contains " + ", ".join(_show(p) for p in found))
        return CheckResult(check.label, True, "None found")

    # must_contain_any: whole-word match, ignoring capitals
    for phrase in check.value:
        if re.search(r"(?<!\w)" + re.escape(phrase.lower()) + r"(?!\w)", lowered):
            return CheckResult(check.label, True, f'Mentions "{phrase}"')
    return CheckResult(check.label, False, "Mentions none of them")


def run_checks(checks: list[Check], text: str) -> list[CheckResult]:
    return [run_check(c, text) for c in checks]
