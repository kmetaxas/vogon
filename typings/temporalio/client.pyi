from collections.abc import Callable
from typing import Any, Generic, TypeVar

_TResult = TypeVar("_TResult")
_TArg = TypeVar("_TArg")

class WorkflowHandle(Generic[_TResult, _TArg]):
    async def signal(self, signal: Callable[..., Any], arg: Any = ...) -> Any: ...

class Client:
    @staticmethod
    async def connect(target_host: str, namespace: str = ...) -> "Client": ...
    async def start_workflow(
        self, workflow: Callable[..., Any], *args: Any, id: str, task_queue: str, **kwargs: Any
    ) -> WorkflowHandle[Any, Any]: ...
    def get_workflow_handle(self, workflow_id: str) -> WorkflowHandle[Any, Any]: ...
    async def close(self) -> None: ...
