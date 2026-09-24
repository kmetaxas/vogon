"""Custom Django model fields for PostgreSQL extensions."""

from __future__ import annotations

from typing import Any

from django.db.models import Field


class VectorField(Field):
    """PostgreSQL ``vector`` field backed by the pgvector extension.

    Stores a list of floats as a pgvector column. ``dimensions`` may be
    ``None`` (generic ``vector`` type) or an ``int`` (``vector(N)`` type).
    """

    description = "Vector"
    empty_strings_allowed = False

    def __init__(self, *args: Any, dimensions: int | None = None, **kwargs: Any) -> None:
        self.dimensions = dimensions
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        if self.dimensions is not None:
            kwargs["dimensions"] = self.dimensions
        return name, path, args, kwargs

    def db_type(self, connection: Any) -> str:
        if self.dimensions is None:
            return "vector"
        return f"vector({self.dimensions})"

    def from_db_value(self, value: Any, expression: Any, connection: Any) -> list[float] | None:
        return self.to_python(value)

    def to_python(self, value: Any) -> list[float] | None:
        if value is None or isinstance(value, list):
            return value
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
            try:
                return [float(part) for part in value.strip("[]").split(",")]
            except ValueError:
                return None
        return list(value)

    def get_prep_value(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
            return str(value)
        return "[" + ",".join(str(float(part)) for part in value) + "]"

    def value_to_string(self, obj: Any) -> str:
        value = self.get_prep_value(self.value_from_object(obj))
        return "" if value is None else value
