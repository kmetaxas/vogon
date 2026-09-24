from typing import Any

class View:
    request: Any
    def dispatch(self, request: Any, *args: Any, **kwargs: Any) -> Any: ...
