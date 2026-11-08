"""Canonical label validation and routing (128 entries, 16 KiB UTF-8 total)."""

from collections.abc import Iterable, Mapping


def normalize_labels(labels: Iterable[str]) -> list[str]:
    """Trim labels; bound keys to 128 and values to 1024 characters.

    Snapshots must have unique nonempty keys and nonempty explicit values.
    Commas and equals signs in values are literal, not delimiters.
    """
    if isinstance(labels, (str, bytes, Mapping)) or not isinstance(labels, Iterable):
        raise LabelSelectorError("Labels must be an iterable of strings.")
    result: list[str] = []
    seen: set[str] = set()
    size = 0
    for label in labels:
        if not isinstance(label, str):
            raise LabelSelectorError("Labels must be strings.")
        key, sep, value = label.partition(":")
        key, value = key.strip(), value.strip()
        if not key or len(key) > 128 or any(c in key for c in ",=\n\r\x00"):
            raise LabelSelectorError("Invalid label key.")
        if sep and (not value or len(value) > 1024):
            raise LabelSelectorError("Invalid label value.")
        if any(c in value for c in "\n\r\x00") or key in seen:
            raise LabelSelectorError("Invalid value or duplicate label key.")
        seen.add(key)
        normalized = f"{key}:{value}" if sep else key
        result.append(normalized)
        size += len(normalized.encode("utf-8"))
        if len(result) > 128 or size > 16384:
            raise LabelSelectorError("Labels exceed 128 entries or 16 KiB.")
    return result


def labels_as_dict(labels: Iterable[str] | None) -> dict[str, str | None]:
    """Convert validated snapshot labels to their canonical map."""
    result: dict[str, str | None] = {}
    for label in normalize_labels(labels if labels is not None else []):
        key, sep, value = label.partition(":")
        result[key] = value if sep else None
    return result


def label_matches(actual: dict[str, str | None], wanted: Mapping) -> bool:
    """Match presence or scalar/list values; an empty allowed list matches nothing."""
    return all(
        key in actual
        and (value is None or actual[key] in (value if isinstance(value, list) else [value]))
        for key, value in wanted.items()
    )


labels_to_dict = labels_as_dict


class LabelSelectorError(ValueError):
    """Raised when a label selector string is malformed."""

    pass


def parse_label_selector(selector: str) -> dict[str, str | None]:
    """
    Parse a label selector string into a dictionary.

    Labels use a colon (:) as the key/value separator, e.g.
    'env:production,team:platform,canary'
    -> {'env': 'production', 'team': 'platform', 'canary': None}

    Raises:
        LabelSelectorError: If the selector contains '=' which is a common
        mistake when confusing metadata fields with labels.
    """
    if not isinstance(selector, str):
        raise LabelSelectorError("Selector must be a string.")
    if not selector.strip():
        return {}
    parts = []
    for part in (selector or "").split(","):
        part = part.strip()
        if not part:
            raise LabelSelectorError("Empty comma-delimited label selector segment.")
        if "=" in part:
            raise LabelSelectorError(
                f"Invalid label selector '{part}': labels use a colon (:), not equals (=). "
                f"Examples: 'env:development' or 'team:platform'. "
                f"Use selector.hostname (or selector.region) for metadata constraints. "
                f"Labels match only actual registered labels, including custom hostname labels."
            )
        parts.append(part)
    return labels_as_dict(parts)


def marvin_matches_labels(marvin, selector: dict[str, str | None]) -> bool:
    """Check if a Marvin's labels satisfy the given selector."""
    try:
        return label_matches(labels_as_dict(marvin.labels), selector)
    except LabelSelectorError:
        return False


def resolve_marvins(organization, capability_name, selector=None):
    """Find online Marvins matching the capability and label selector."""
    qs = organization.marvins.filter(
        status="online",
        capabilities__name=capability_name,
    ).distinct()
    if selector:
        qs = [m for m in qs if marvin_matches_labels(m, selector)]
    return list(qs)
