from typing import Any

class HttpResponseRedirect:
    status_code: int
    url: str
    def __init__(self, redirect_to: str, *args: Any, **kwargs: Any) -> None: ...
