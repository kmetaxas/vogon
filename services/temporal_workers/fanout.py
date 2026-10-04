import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast


class PolicySnapshot(Protocol):
    def model_dump(self) -> dict[str, Any]: ...


RecordCreator = Callable[[str, str, str, Any, Any, dict[str, Any], int], Any]
CapabilityExecutor = Callable[..., Awaitable[dict[str, Any]]]
StatusUpdater = Callable[[str, str, dict[str, Any] | None, str | None], Any | Awaitable[Any]]


def resolve_targets(
    discovery_engine: Any,
    org: Any,
    capability_name: str,
    selector: Any,
    target_scope: Any,
    *,
    session: Any = None,
) -> tuple[Any, Any, list[str], dict[str, Any]]:
    from apps.marvins.models import Capability, Marvin

    target_set = discovery_engine.resolve_targets(
        org,
        capability_name,
        selector=selector,
        session_scope=target_scope,
    )
    capability = Capability.objects.get(id=target_set.capability_id)
    snapshot = [str(marvin_id) for marvin_id in cast(list[str], target_set.snapshot)]
    policy, _budget = discovery_engine.check_execution_policy(
        org,
        capability,
        len(snapshot),
        session=session,
    )
    marvins = list(Marvin.objects.filter(id__in=snapshot, organization=org))
    marvins_by_id = {str(marvin.id): marvin for marvin in marvins}
    marvin_ids = [marvin_id for marvin_id in snapshot if marvin_id in marvins_by_id]
    policy_snapshot = cast(PolicySnapshot, policy).model_dump()
    return target_set, capability, marvin_ids, policy_snapshot


def create_execution_records(
    parent_type: str,
    parent_id: str,
    marvin_ids: list[str],
    capability: Any,
    target_set: Any,
    policy_snapshot: dict[str, Any],
    *,
    create_record: RecordCreator,
) -> list[dict[str, str]]:
    records = [
        create_record(
            parent_type, parent_id, marvin_id, capability, target_set, policy_snapshot, index
        )
        for index, marvin_id in enumerate(marvin_ids)
    ]
    return [{"id": str(record.id)} for record in records]


async def execute_fanout(
    marvin_ids: list[str],
    capability_name: str,
    parameters: dict[str, Any],
    *,
    execute_capability: CapabilityExecutor,
    execution_ids: list[str],
    update_status: StatusUpdater,
    session_id: str,
    thread_id: str,
    target_set_id: str | None = None,
    execution_mode: str | None = None,
) -> list[dict[str, Any]]:
    async def _update_status(
        execution_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        update_result = update_status(execution_id, status, result, error)
        if asyncio.iscoroutine(update_result):
            await update_result

    async def _execute_one(index: int, marvin_id: str) -> dict[str, Any]:
        execution_id = execution_ids[index]
        await _update_status(execution_id, "running")
        try:
            result = await execute_capability(
                session_id,
                thread_id,
                capability_name,
                parameters,
                marvin_id,
                target_set_id=target_set_id,
                execution_mode=execution_mode,
                result_index=index,
            )
        except Exception as exc:
            error = str(exc)
            await _update_status(execution_id, "failed", error=error)
            return {"index": index, "marvin_id": marvin_id, "error": error}

        await _update_status(execution_id, "completed", result=result)
        return {"index": index, "marvin_id": marvin_id, "result": result}

    return await asyncio.gather(
        *[_execute_one(index, marvin_id) for index, marvin_id in enumerate(marvin_ids)]
    )


def aggregate_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [item for item in results if "result" in item]
    errors = [item for item in results if "error" in item]
    return {
        "success": not errors,
        "results": successful,
        "errors": errors,
        "summary": {
            "total": len(results),
            "succeeded": len(successful),
            "failed": len(errors),
        },
    }


def update_execution_status(
    execution_id: str,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    *,
    update_record: StatusUpdater,
) -> Any:
    return update_record(execution_id, status, result, error)
