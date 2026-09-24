from typing import Any

def path(
    route: str, view: Any, kwargs: dict[str, Any] | None = ..., name: str | None = ...
) -> Any: ...
def reverse(
    viewname: str,
    urlconf: str | None = ...,
    args: list[Any] | None = ...,
    kwargs: dict[str, Any] | None = ...,
    current_app: str | None = ...,
) -> str: ...
