"""Lookup result state for Multi-Booru Tag Importer.

Keeps transient provider failures distinct from authoritative misses so a temporary
network/rate-limit problem never becomes a persistent ``Multi-Booru No Match`` tag.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Optional


class LookupStatus(str, Enum):
    MATCH = "match"
    MISS = "miss"
    RETRYABLE_ERROR = "retryable_error"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class LookupOutcome:
    provider: str
    stage: str
    status: LookupStatus
    value: Optional[Any] = None
    detail: str = ""

    @property
    def matched(self) -> bool:
        return self.status == LookupStatus.MATCH and self.value is not None


def can_mark_no_match(outcomes: Iterable[LookupOutcome]) -> bool:
    """Return True only when every applicable lookup definitively missed.

    Disabled providers are not represented in ``outcomes``. Any configured lookup that
    became unavailable or failed transiently makes the result non-authoritative and keeps
    the image eligible for a later run.
    """
    items = list(outcomes)
    return bool(items) and all(item.status == LookupStatus.MISS for item in items)


def has_retryable_or_unavailable(outcomes: Iterable[LookupOutcome]) -> bool:
    return any(
        item.status in {LookupStatus.RETRYABLE_ERROR, LookupStatus.UNAVAILABLE}
        for item in outcomes
    )
