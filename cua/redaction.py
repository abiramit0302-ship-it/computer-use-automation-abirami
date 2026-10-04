"""Redaction: keep secrets and regulated data out of everything we persist or send to an LLM.

Three layers, applied in order:
  1. Known values: every secret, input and output value is registered with its
     sensitivity and replaced wherever it appears. Exact, and catches values no pattern
     could recognize (a password, a member ID).
  2. Labelled fields: the app profile names screen labels whose neighboring value is
     personal data ('Name:'); that value is masked in accessibility-tree text.
  3. Patterns: a backstop for well-known shapes (SSNs, card numbers, dates, money, emails).
No single layer is enough; together they cover each other's gaps.
"""

import re
from collections.abc import Iterable
from typing import Any

from cua.schema import Sensitivity

MIN_VALUE_LENGTH = 3  # shorter non-secret values would cause too many false replacements

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("SSN", re.compile(r"\*{3}-\*{2}-\d{4}")),                # masked form shown on screen
    ("CARD", re.compile(r"\b(?:\d[ -]?){12,18}\d\b")),         # 13-19 digit card numbers
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("DATE", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),            # dates of birth
    ("MONEY", re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?")),
]


def _mask_identifier(value: str) -> str:
    """Keep the last two characters so runs stay distinguishable while debugging."""
    return "***" + value[-2:] if len(value) > 4 else "***"


class Redactor:
    def __init__(self, sensitive_labels: Iterable[str] = ()):
        self._replacements: dict[str, str] = {}
        self._label_rules: list[tuple[re.Pattern[str], str]] = []
        for label in sensitive_labels:
            escaped = re.escape(label)
            # In the accessibility tree a label cell is followed by its value cell:
            #   - cell "Name:"
            #   - cell "Jordan Ellis"
            self._label_rules.append(
                (re.compile(rf'(- cell "{escaped}"\n\s*- cell )"[^"\n]*"'), r'\1"[PII]"'))
            # Container names merge label and value: row "Name: Jordan Ellis SSN ..."
            self._label_rules.append(
                (re.compile(rf'"[^"\n]*{escaped}[^"\n]+"'), '"[PII ROW]"'))

    def register(self, name: str, value: Any, sensitivity: Sensitivity) -> None:
        """Remember a concrete value so it's replaced wherever it appears later."""
        if value is None or sensitivity == Sensitivity.PUBLIC:
            return
        value = str(value)
        if sensitivity != Sensitivity.SECRET and len(value) < MIN_VALUE_LENGTH:
            return
        if sensitivity == Sensitivity.IDENTIFIER:
            token = f"[{name}:{_mask_identifier(value)}]"
        else:
            token = f"[{sensitivity.value.upper()}:{name}]"
        self._replacements[value] = token

    def redact(self, text: str) -> str:
        # Longest values first, so a value containing another is replaced whole.
        for value in sorted(self._replacements, key=len, reverse=True):
            text = text.replace(value, self._replacements[value])
        for pattern, replacement in self._label_rules:
            text = pattern.sub(replacement, text)
        for label, pattern in PATTERNS:
            text = pattern.sub(f"[{label}]", text)
        return text

    def redact_data(self, obj: Any) -> Any:
        """Redact every string inside nested dicts and lists; other values are checked as text."""
        if isinstance(obj, dict):
            return {key: self.redact_data(value) for key, value in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.redact_data(value) for value in obj]
        if obj is None or isinstance(obj, bool):
            return obj
        if isinstance(obj, (int, float)):
            text = str(obj)
            redacted = self.redact(text)
            return obj if redacted == text else redacted
        return self.redact(str(obj))  # strings, Decimals, dates, anything else