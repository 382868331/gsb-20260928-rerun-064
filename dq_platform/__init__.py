"""Local-first data quality rule platform (round one).

Supports cross-field and cross-record checks against local manual samples.
"""

from .values import MISSING, MALFORMED, is_bad

__all__ = ["MISSING", "MALFORMED", "is_bad"]
