"""Sentinels for values that must never be coerced to zero/False.

A missing cell and an unparseable (malformed) cell are distinct error
conditions, not ordinary scalar values.
"""


class _BadValue:
    __slots__ = ("label", "reason")

    def __init__(self, label, reason):
        self.label = label
        self.reason = reason

    def __repr__(self):
        return self.label

    def __bool__(self):
        # Explicitly forbid truthiness-based treatment (e.g. ``if not value``
        # turning the condition into a zero/false comparison).
        raise TypeError(f"{self.label} is not a comparable scalar: {self.reason}")


MISSING = _BadValue("<MISSING>", "field is absent or empty")
MALFORMED = _BadValue("<MALFORMED>", "value cannot be parsed")


def is_bad(value):
    return value is MISSING or value is MALFORMED
