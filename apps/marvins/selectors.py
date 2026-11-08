"""Pydantic models describing how Marvin agents select target hosts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from apps.marvins.labels import normalize_labels

# A single label value: a list of allowed values, a scalar, or an explicit
# presence marker (None requires this label key, with any value).
_LabelValue = list[str] | str | None


def _to_list(value: _LabelValue) -> list[str]:
    """Normalize a value to a list; presence is handled separately during merging."""
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

    model_config = {"extra": "forbid"}

    @field_validator("*", mode="before")
    @classmethod
    def validate_shape(cls, value: Any, info: Any) -> Any:
        if value is None:
            return None
        if info.field_name == "labels":
            if not isinstance(value, dict):
                raise ValueError("labels must be a mapping")
            result: dict[str, _LabelValue] = {}
            total_size = 0
            for key, allowed in value.items():
                if not isinstance(key, str) or ":" in key:
                    raise ValueError("Invalid label key")
                key = normalize_labels([key])[0]
                if key in result:
                    raise ValueError("Duplicate normalized label key")
                if allowed is None:
                    result[key] = None
                    continue
                items = allowed if isinstance(allowed, list) else [allowed]
                if len(items) > 128:
                    raise ValueError("At most 128 allowed values per label")
                values = []
                for item in items:
                    if not isinstance(item, str):
                        raise ValueError("Label values must be strings")
                    values.append(normalize_labels([f"{key}:{item}"])[0].split(":", 1)[1])
                    total_size += len(key.encode("utf-8")) + len(values[-1].encode("utf-8"))
                    if total_size > 16384:
                        raise ValueError("Label constraints exceed 16 KiB")
                result[key] = values if isinstance(allowed, list) else values[0]
            normalize_labels(result.keys())
            return result
        if not isinstance(value, list) or any(
            not isinstance(v, str) or not v.strip() for v in value
        ):
            raise ValueError("Selector fields must be lists of nonempty strings")
        values = [v.strip() for v in value]
        if info.field_name in {"marvin_ids", "resource_ids"}:
            values = [str(UUID(v)) for v in values]
        return values

    hostname: list[str] | None = Field(default=None, description="Allowed hostnames.")
    region: list[str] | None = Field(default=None, description="Allowed regions.")
    availability_zone: list[str] | None = Field(
        default=None, description="Allowed availability zones."
    )
    provider: list[str] | None = Field(
        default=None, description="Allowed infrastructure providers."
    )
    labels: dict[str, _LabelValue] | None = Field(
        default=None, description="Label key/value constraints."
    )
    resource_ids: list[str] | None = Field(
        default=None, description="Allowed resource identifiers."
    )
    marvin_ids: list[str] | None = Field(
        default=None, description="Allowed Marvin agent identifiers."
    )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TargetSelector:
        """Build a TargetSelector from an arbitrary dict.

        Unknown keys and malformed shapes are rejected, never ignored.
        """
        if not isinstance(data, Mapping):
            raise ValueError("Selector must be a mapping")
        return cls(**dict(data))

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict, omitting fields whose value is ``None``."""
        return {key: value for key, value in self.model_dump().items() if value is not None}

    def merge_with(self, other: TargetSelector) -> TargetSelector:
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
        own: dict[str, _LabelValue], other: dict[str, _LabelValue]
    ) -> dict[str, _LabelValue]:
        """Merge two label maps using AND semantics per label key.

        Each value is normalized to a list (``None`` means presence). When both
        sides constrain the same label, the lists are intersected. A scalar
        (``str``) is treated as a single-element list.
        """
        merged: dict[str, _LabelValue] = {}
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
