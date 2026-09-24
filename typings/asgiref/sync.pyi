from collections.abc import Callable
from typing import Any

def async_to_sync(awaitable: Callable[..., Any]) -> Callable[..., Any]: ...
