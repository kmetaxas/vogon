from typing import Any

class TestCase:
    client: Any
    def assertEqual(self, first: Any, second: Any, msg: str | None = ...) -> None: ...
    def assertContains(
        self,
        response: Any,
        text: str,
        count: int | None = ...,
        status_code: int = ...,
        html: bool = ...,
    ) -> None: ...
    def assertRedirects(
        self, response: Any, expected_url: str, *args: Any, **kwargs: Any
    ) -> None: ...
    def assertTemplateUsed(self, response: Any, template_name: str) -> None: ...
