"""Pydantic models describing how Marvin agents select target hosts."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Union

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# A single label value: a list of allowed values, a scalar, or an explicit
# "any" marker (None means no filtering on this label).
_LabelValue = Union[List[str], str, None]


def _to_list(value: _LabelValue) -> List[str]:
    """Normalize a label value to a list; ``None`` (any) yields an empty list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return value


class TargetSelector(BaseModel):
    """Criteria selecting which targets a Marvin operation applies to.

    Each field is optional; ``None`` means "no constraint for this key".
    When a field is a list, an empty list matches nothing while ``None``
    matches everything (AND semantics are applied during merges).
    """

    model_config = {"extra": "ignore"}

    hostname: Optional[List[str]] = Field(default=None, description="Allowed hostnames.")
    region: Optional[List[str]] = Field(default=None, description="Allowed regions.")
    availability_zone: Optional[List[str]] = Field(
        default=None, description="Allowed availability zones."
    )
    provider: Optional[List[str]] = Field(
        default=None, description="Allowed infrastructure providers."
    )
    labels: Optional[Dict[str, _LabelValue]] = Field(
        default=None, description="Label key/value constraints."
    )
    resource_ids: Optional[List[str]] = Field(
        default=None, description="Allowed resource identifiers."
    )
    marvin_ids: Optional[List[str]] = Field(
        default=None, description="Allowed Marvin agent identifiers."
    )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TargetSelector":
        """Build a TargetSelector from an arbitrary dict.

        Unknown keys are dropped (and logged as a warning); known ``None``
        entries are kept so callers can distinguish "unset" from "set".
        """
        if not data:
            return cls()
        known = set(cls.model_fields)
        payload: Dict[str, Any] = {}
        for key, value in data.items():
            if key in known:
                payload[key] = value
            else:
                logger.warning("Ignoring unknown TargetSelector key %r", key)
        return cls(**payload)

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain dict, omitting fields whose value is ``None``."""
        return {key: value for key, value in self.model_dump().items() if value is not None}

    def merge_with(self, other: "TargetSelector") -> "TargetSelector":
        """Combine with another selector using AND semantics.

        For keys present (non-``None``) on *both* sides, list values are
        intersected—a target must satisfy both constraints. Keys set on only
        one side are preserved. ``labels`` maps are merged per-label, again
        intersecting list values where both sides constrain the same label.
        """
        merged_own = self.to_dict()
        for key, other_value in other.to_dict().items():
            own_value = merged_own.get(key)
            if own_value is None:
                merged_own[key] = other_value
            elif key == "labels":
                merged_own[key] = self._merge_labels(own_value, other_value)
            else:
                # Both sides are lists; intersect them.
                intersection = [item for item in own_value if item in other_value]
                merged_own[key] = intersection
        return TargetSelector(**merged_own)

    @staticmethod
    def _merge_labels(
        own: Dict[str, _LabelValue], other: Dict[str, _LabelValue]
    ) -> Dict[str, _LabelValue]:
        """Merge two label maps using AND semantics per label key.

        Each value is normalized to a list (``None`` means "any"). When both
        sides constrain the same label, the lists are intersected. A scalar
        (``str``) is treated as a single-element list.
        """
        merged: Dict[str, _LabelValue] = {}
        all_keys = set(own) | set(other)
        for key in all_keys:
            own_value = own.get(key)
            other_value = other.get(key)
            if own_value is None:
                merged[key] = other_value
            elif other_value is None:
                merged[key] = own_value
            else:
                own_items, other_items = _to_list(own_value), _to_list(other_value)
                intersected = [item for item in own_items if item in other_items]
                merged[key] = intersected[0] if len(intersected) == 1 else intersected
        return merged
