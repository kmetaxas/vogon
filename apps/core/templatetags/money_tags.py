from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def money(value):
    if value is None:
        return "$0.00"
    try:
        d = Decimal(str(value))
        normalized = d.normalize()
        # Enforce a minimum of 2 decimal places (e.g. 0.5 -> 0.50, 0 -> 0.00)
        # while preserving higher precision values (e.g. 0.123456).
        exponent = normalized.as_tuple().exponent
        if not isinstance(exponent, int) or exponent > -2:
            normalized = normalized.quantize(Decimal("0.01"))
        return f"${normalized}"
    except Exception:
        return "$0.00"
