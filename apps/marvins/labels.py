"""Label selector parsing and matching for Marvin agent routing."""


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
    out: dict[str, str | None] = {}
    for part in (selector or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            raise LabelSelectorError(
                f"Invalid label selector '{part}': labels use a colon (:), not equals (=). "
                f"Examples: 'env:development' or 'team:platform'. "
                f"You cannot use metadata fields like client_id, hostname, or region as labels."
            )
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
        else:
            out[part.strip()] = None
    return out


def marvin_matches_labels(marvin, selector: dict[str, str | None]) -> bool:
    """Check if a Marvin's labels satisfy the given selector."""
    labels = {}
    for label in marvin.labels or []:
        if ":" in label:
            k, v = label.split(":", 1)
            labels[k] = v
        else:
            labels[label] = None
    for k, v in selector.items():
        if k not in labels:
            return False
        if v is not None and labels[k] != v:
            return False
    return True


def resolve_marvins(organization, capability_name, selector=None):
    """Find online Marvins matching the capability and label selector."""
    qs = organization.marvins.filter(
        status="online",
        capabilities__name=capability_name,
    ).distinct()
    if selector:
        qs = [m for m in qs if marvin_matches_labels(m, selector)]
    return list(qs)
