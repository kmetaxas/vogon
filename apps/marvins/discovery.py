"""Unified capability discovery and target resolution for Marvins."""

from __future__ import annotations

import asyncio
import logging
import math
from collections import Counter, OrderedDict, defaultdict
from datetime import timedelta
from typing import Any, Iterable, cast

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Count, Q
from django.db.transaction import atomic
from django.utils import timezone

from apps.marvins.models import Capability, Marvin, ResolvedTargetSet
from apps.marvins.policies import ExecutionPolicy
from apps.marvins.selectors import TargetSelector
from apps.sessions.budget import SessionBudget
from services import metrics

logger = logging.getLogger(__name__)

RRF_K = 60
AVAILABILITY_BOOST = 0.05


class DiscoveryError(Exception):
    """Base exception for discovery and target-resolution failures."""


class CapabilityNotFound(DiscoveryError):
    """Raised when a requested capability is missing, disabled, or out of scope."""


class FanOutLimitExceeded(DiscoveryError):
    """Raised when target resolution exceeds a hard execution-policy limit."""


class BudgetExceeded(DiscoveryError):
    """Raised when a session budget cannot reserve another execution."""


class SelectorRequiresResource(DiscoveryError):
    """Raised when resource-scoped resolution cannot identify any resource targets."""


def _classify_fanout(count: int) -> str:
    """Classify fan-out size for target-set metadata."""
    if count <= 1:
        return "single"
    if count <= 10:
        return "small"
    if count <= 50:
        return "medium"
    return "large"


def _extract_sample_labels(marvins: Iterable[Any], max_samples: int = 10) -> list[str]:
    """Return stable unique labels sampled from Marvin JSON label lists."""
    labels: list[str] = []
    seen: set[str] = set()
    for marvin in marvins:
        for label in marvin.labels or []:
            if label not in seen:
                seen.add(label)
                labels.append(label)
                if len(labels) >= max_samples:
                    return labels
    return labels


def _build_aggregates(marvins: Iterable[Any]) -> dict[str, dict[str, int]]:
    """Build region and provider count aggregates from Marvin host metadata."""
    regions: Counter[str] = Counter()
    providers: Counter[str] = Counter()
    for marvin in marvins:
        if marvin.region:
            regions[marvin.region] += 1
        if marvin.provider:
            providers[marvin.provider] += 1
    return {"regions": dict(regions), "providers": dict(providers)}


def _merge_selectors(session_selector: Any, requested_selector: Any) -> dict[str, Any]:
    """Merge session and requested selectors using TargetSelector AND semantics."""
    session = _coerce_selector(session_selector)
    requested = _coerce_selector(requested_selector)
    return session.merge_with(requested).to_dict()


def _coerce_selector(selector: Any) -> TargetSelector:
    """Normalize selector inputs to TargetSelector instances."""
    if selector is None:
        return TargetSelector()
    if isinstance(selector, TargetSelector):
        return selector
    if hasattr(selector, "selector"):
        return TargetSelector.from_dict(selector.selector or {})
    if isinstance(selector, dict):
        return TargetSelector.from_dict(selector)
    return TargetSelector.from_dict(dict(selector))


def _labels_as_dict(labels: Iterable[str] | None) -> dict[str, str | None]:
    """Convert Marvin label strings into a key/value map."""
    parsed: dict[str, str | None] = {}
    for label in labels or []:
        if ":" in label:
            key, value = label.split(":", 1)
            parsed[key] = value
        else:
            parsed[label] = None
    return parsed


def _label_matches(actual: dict[str, str | None], wanted: dict[str, Any]) -> bool:
    """Return whether parsed Marvin labels satisfy selector label constraints."""
    for key, value in wanted.items():
        if key not in actual:
            return False
        if value is None:
            continue
        allowed = value if isinstance(value, list) else [value]
        if actual[key] not in allowed:
            return False
    return True


def _selector_has_resource_constraint(selector: dict[str, Any]) -> bool:
    """Return whether a selector explicitly constrains resource identifiers."""
    return bool(selector.get("resource_ids"))


def _run_coroutine(coro: Any) -> Any:
    """Run a coroutine to completion, safe in both sync and async contexts."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class DiscoveryEngine:
    """Find capabilities and resolve concrete Marvin executors for targets."""

    RESOURCE_SCOPES = {"cluster", "namespace", "service", "environment"}

    def find_capabilities(
        self,
        organization: Any,
        query: str | None = None,
        selector: TargetSelector | dict[str, Any] | None = None,
        limit: int = 10,
        capability_name: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with metrics.timer("find_tools.total"):
            limit = min(limit, 20)
            selector_dict = _coerce_selector(selector).to_dict()
            filters = filters or {}
            capability_qs = Capability.objects.filter(
                organization=organization,
                enabled=True,
                marvins__organization=organization,
                marvins__status=Marvin.Status.ONLINE,
            ).distinct()

            capability_qs = self._apply_capability_filters(capability_qs, filters)

            if capability_name:
                capability_qs = capability_qs.filter(name=capability_name)

            if query:
                ordered, search_method = self._rank_capabilities(capability_qs, query, limit)
            else:
                ordered = [(capability, None) for capability in capability_qs.order_by("name")]
                search_method = "lexical"

            results: list[dict[str, Any]] = []
            for capability, score in ordered:
                marvins_qs = self._apply_selector(
                    self._base_marvin_queryset(organization, capability), selector_dict
                )
                marvins = list(marvins_qs.order_by("id").distinct())
                if not marvins:
                    continue

                environments = sorted(
                    label_value
                    for labels in (_labels_as_dict(marvin.labels) for marvin in marvins)
                    if (label_value := labels.get("env")) is not None
                )
                clusters = sorted(
                    label_value
                    for labels in (_labels_as_dict(marvin.labels) for marvin in marvins)
                    if (label_value := labels.get("cluster")) is not None
                )
                regions = sorted({marvin.region for marvin in marvins if marvin.region})

                result: dict[str, Any] = {
                    "name": capability.name,
                    "description": capability.description,
                    "scope": capability.execution_scope,
                    "strategy": capability.selection_strategy,
                    "schema": capability.json_schema,
                    "keywords": capability.keywords,
                    "online_count": len(marvins),
                    "environments": environments,
                    "clusters": clusters or regions,
                    "sample_labels": _extract_sample_labels(marvins),
                    "search_method": search_method,
                }
                if score is not None:
                    result["score"] = score
                results.append(result)
                if len(results) >= limit:
                    break

            metrics.incr("find_tools.requests")
            if results:
                metrics.incr("find_tools.results", len(results))
            else:
                metrics.incr("find_tools.empty_results")
            return {"results": results, "total_found": len(results), "search_method": search_method}

    def _apply_capability_filters(self, queryset: Any, filters: dict[str, Any]) -> Any:
        """Apply capability-level filters (providers, scopes) to a queryset."""
        providers = filters.get("providers")
        if providers:
            queryset = queryset.filter(provider__in=providers)
        scopes = filters.get("scopes")
        if scopes:
            queryset = queryset.filter(execution_scope__in=scopes)
        return queryset

    def _rank_capabilities(
        self, base_qs: Any, query: str, limit: int
    ) -> tuple[list[tuple[Capability, float | None]], str]:
        """Rank capabilities for a query using hybrid or lexical search."""
        search_limit = limit * 2
        lexical_results = self._lexical_search(base_qs, query, search_limit)

        vector_results: list[tuple[str, int]] = []
        search_method = "lexical"

        if getattr(settings, "EMBEDDING_SEARCH_ENABLED", False):
            query_embedding = self._embed_query(query)
            if query_embedding is not None:
                try:
                    vector_results = self._vector_search(base_qs, query_embedding, search_limit)
                    search_method = "hybrid"
                except Exception as exc:
                    logger.warning("Vector search failed, falling back to lexical: %s", exc)

        if vector_results:
            raw_scores = self._rrf_fusion(lexical_results, vector_results)
            max_possible = 2.0 / RRF_K
        else:
            raw_scores = {cap_id: 1.0 / (RRF_K + rank) for cap_id, rank in lexical_results}
            max_possible = 1.0 / (RRF_K + 1)

        online_counts = {
            str(cap.id): cap.online_marvin_count
            for cap in base_qs.filter(id__in=raw_scores.keys()).annotate(
                online_marvin_count=Count("marvins", filter=Q(marvins__status=Marvin.Status.ONLINE))
            )
        }
        exact_match_cap_id: str | None = None
        query_normalized = query.strip().lower()
        for cap in base_qs.filter(id__in=raw_scores.keys()):
            if cap.name.lower() == query_normalized:
                exact_match_cap_id = str(cap.id)
                break
        for cap_id in raw_scores:
            raw_scores[cap_id] += AVAILABILITY_BOOST * min(online_counts.get(cap_id, 0), 10)
            if cap_id == exact_match_cap_id:
                raw_scores[cap_id] += 1.0

        ranked_ids = sorted(raw_scores, key=lambda cap_id: raw_scores[cap_id], reverse=True)
        capabilities = {str(cap.id): cap for cap in base_qs.filter(id__in=ranked_ids)}
        ordered: list[tuple[Capability, float | None]] = []
        for cap_id in ranked_ids:
            capability = capabilities.get(cap_id)
            if capability is None:
                continue
            score = min(1.0, raw_scores[cap_id] / max_possible)
            ordered.append((capability, score))
        return ordered, search_method

    def _embed_query(self, query: str) -> list[float] | None:
        """Generate a query embedding, returning None on failure."""
        try:
            from services.embeddings.registry import EmbeddingProviderFactory

            provider = EmbeddingProviderFactory.get_default_provider()
            result = _run_coroutine(provider.embed(query))
            return result.embedding
        except Exception as exc:
            logger.warning("Query embedding failed, falling back to lexical: %s", exc)
            return None

    def _lexical_search(self, base_qs: Any, query: str, limit: int) -> list[tuple[str, int]]:
        """Return ranked (capability_id, rank) using full-text search with icontains fallback."""
        try:
            from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector

            vector = (
                SearchVector("search_document", weight="A")
                + SearchVector("name", weight="B")
                + SearchVector("description", weight="C")
            )
            search_query = SearchQuery(query, config="english", search_type="plain")
            qs = (
                base_qs.annotate(rank=SearchRank(vector, search_query))
                .filter(rank__gt=0)
                .order_by("-rank")
            )
            return [(str(cap.id), index) for index, cap in enumerate(qs[:limit], start=1)]
        except Exception as exc:
            logger.warning("Lexical full-text search failed, falling back to icontains: %s", exc)
            return self._icontains_search(base_qs, query, limit)

    def _icontains_search(self, base_qs: Any, query: str, limit: int) -> list[tuple[str, int]]:
        """Fallback keyword search using icontains on name/description/keywords."""
        terms = [term.strip() for term in query.split() if term.strip()]
        if not terms:
            return []
        matching_qs = base_qs.none()
        for term in terms:
            matching_qs = matching_qs | base_qs.filter(name__icontains=term)
            matching_qs = matching_qs | base_qs.filter(description__icontains=term)
            matching_qs = matching_qs | base_qs.filter(keywords__icontains=term)
        return [
            (str(cap.id), index)
            for index, cap in enumerate(matching_qs.distinct().order_by("name")[:limit], start=1)
        ]

    def _vector_search(
        self, base_qs: Any, query_embedding: list[float], limit: int
    ) -> list[tuple[str, int]]:
        """Return ranked (capability_id, rank) using pgvector cosine distance."""
        from django.db.models.expressions import RawSQL
        from django.db.models.fields import FloatField

        vector_str = "[" + ",".join(str(float(part)) for part in query_embedding) + "]"
        expected_model = getattr(settings, "EMBEDDING_MODEL", "")
        qs = base_qs.filter(embedding__isnull=False, needs_re_embedding=False)
        if expected_model:
            qs = qs.filter(embedding_model=expected_model)
        qs = qs.annotate(
            distance=RawSQL("embedding <=> %s::vector", [vector_str], output_field=FloatField())
        ).order_by("distance")
        return [(str(cap.id), index) for index, cap in enumerate(qs[:limit], start=1)]

    def _rrf_fusion(
        self,
        lexical_results: list[tuple[str, int]],
        vector_results: list[tuple[str, int]],
        k: int = RRF_K,
    ) -> dict[str, float]:
        """Fuse lexical and vector ranks using Reciprocal Rank Fusion."""
        scores: dict[str, float] = defaultdict(float)
        for cap_id, rank in lexical_results:
            scores[cap_id] += 1.0 / (k + rank)
        for cap_id, rank in vector_results:
            scores[cap_id] += 1.0 / (k + rank)
        return scores

    def resolve_targets(
        self,
        organization: Any,
        capability: Capability | str,
        selector: TargetSelector | dict[str, Any] | None = None,
        session_scope: Any = None,
    ) -> ResolvedTargetSet:
        """Resolve a capability and selector into an immutable target-set snapshot."""
        capability = self._get_capability(organization, capability)
        session_selector = getattr(session_scope, "selector", None)
        merged_selector = _merge_selectors(session_selector, selector)
        policy = ExecutionPolicy.from_capability(capability)
        if policy.requires_resource and not _selector_has_resource_constraint(merged_selector):
            raise SelectorRequiresResource("Capability policy requires selector.resource_ids.")

        marvins = list(
            self._apply_selector(
                self._base_marvin_queryset(organization, capability), merged_selector
            )
            .prefetch_related("resource_attachments__resource__resource_type")
            .order_by("id")
            .distinct()
        )
        logical_targets = self._resolve_logical_targets(capability, marvins, merged_selector)

        selected_by_target: dict[str, list[Marvin]] = {}
        selected: OrderedDict[str, Marvin] = OrderedDict()
        for target_key, eligible in sorted(logical_targets.items(), key=lambda item: item[0]):
            executors = self.select_executors(eligible, cast(str, capability.selection_strategy))
            selected_by_target[target_key] = executors
            for executor in executors:
                selected[str(executor.id)] = executor

        self.check_execution_policy(organization, capability, len(selected))
        selected_marvins = list(selected.values())
        snapshot = list(selected.keys())
        metadata = {
            "count": len(snapshot),
            "logical_target_count": len(logical_targets),
            "fanout_class": _classify_fanout(len(snapshot)),
            "sample_labels": _extract_sample_labels(selected_marvins),
            "aggregates": _build_aggregates(selected_marvins),
            "targets": {
                target: [str(marvin.id) for marvin in executors]
                for target, executors in selected_by_target.items()
            },
        }

        return ResolvedTargetSet.objects.create(
            organization=organization,
            capability=capability,
            selector=merged_selector,
            session_scope=session_scope,
            snapshot=snapshot,
            snapshot_metadata=metadata,
            expires_at=timezone.now() + timedelta(minutes=5),
        )

    @staticmethod
    def select_executors(marvins: Iterable[Marvin], strategy: str) -> list[Marvin]:
        """Select executors deterministically by lexicographic Marvin UUID."""
        ordered = sorted(marvins, key=lambda marvin: str(marvin.id))
        if strategy == "all":
            return ordered
        if strategy == "quorum":
            return ordered[: math.ceil(len(ordered) / 2)]
        return ordered[:1]

    def check_execution_policy(
        self,
        organization: Any,
        capability: Capability,
        target_count: int,
        session: Any = None,
    ) -> tuple[ExecutionPolicy, SessionBudget | None]:
        """Validate execution and optional session-budget limits."""
        policy = ExecutionPolicy.from_capability(capability)
        if target_count > policy.absolute_max_targets:
            raise FanOutLimitExceeded(
                f"{capability.name} resolved {target_count} targets; "
                f"hard limit is {policy.absolute_max_targets}."
            )
        if target_count > policy.default_max_targets:
            logger.warning(
                "Capability %s resolved %s targets above default limit %s for org %s",
                capability.name,
                target_count,
                policy.default_max_targets,
                organization.id,
            )

        if session is None:
            return policy, None

        with atomic():
            locked_session = session.__class__.objects.select_for_update().get(
                id=session.id,
                organization=organization,
            )
            budget = SessionBudget.from_session(locked_session)
            if budget.executions_used >= budget.max_executions_per_session:
                raise BudgetExceeded("Session execution budget is exhausted.")
            if budget.targets_used + target_count > budget.max_targets_per_session:
                raise BudgetExceeded("Session target budget would be exceeded.")
            if budget.concurrent_running >= budget.max_concurrent_executions:
                raise BudgetExceeded("Session concurrent execution budget is exhausted.")

            budget.executions_used += 1
            budget.targets_used += target_count
            budget.concurrent_running += 1
            locked_session.execution_budget = budget.to_dict()
            locked_session.save(update_fields=["execution_budget"])

        return policy, budget

    def _get_capability(self, organization: Any, capability: Capability | str) -> Capability:
        """Fetch and validate an enabled organization-scoped capability."""
        if isinstance(capability, Capability):
            if getattr(capability, "organization_id") == organization.id and capability.enabled:
                return capability
            raise CapabilityNotFound("Capability is disabled or outside the organization.")
        try:
            return Capability.objects.get(organization=organization, enabled=True, name=capability)
        except ObjectDoesNotExist as exc:
            raise CapabilityNotFound(f"Capability not found: {capability}") from exc

    def _base_marvin_queryset(self, organization: Any, capability: Capability) -> Any:
        """Return online organization Marvins advertising the capability."""
        return Marvin.objects.filter(
            organization=organization,
            status=Marvin.Status.ONLINE,
            capabilities=capability,
        )

    def _apply_selector(
        self,
        queryset: Any,
        selector: dict[str, Any],
    ) -> Any:
        """Apply selector filters to a Marvin queryset where the ORM can express them."""
        if selector.get("hostname") is not None:
            queryset = queryset.filter(hostname__in=selector["hostname"])
        if selector.get("region") is not None:
            queryset = queryset.filter(region__in=selector["region"])
        if selector.get("availability_zone") is not None:
            queryset = queryset.filter(availability_zone__in=selector["availability_zone"])
        if selector.get("provider") is not None:
            queryset = queryset.filter(provider__in=selector["provider"])
        if selector.get("resource_ids") is not None:
            queryset = queryset.filter(
                resource_attachments__resource_id__in=selector["resource_ids"]
            )
        if selector.get("marvin_ids") is not None:
            queryset = queryset.filter(id__in=selector["marvin_ids"])

        labels = selector.get("labels")
        if labels:
            matched_ids = [
                marvin.id
                for marvin in queryset.only("id", "labels")
                if _label_matches(_labels_as_dict(marvin.labels), labels)
            ]
            queryset = queryset.filter(id__in=matched_ids)

        return queryset.distinct()

    def _resolve_logical_targets(
        self,
        capability: Capability,
        marvins: list[Marvin],
        selector: dict[str, Any],
    ) -> dict[str, list[Marvin]]:
        """Resolve executor candidates grouped by the capability's execution scope."""
        scope = cast(str, capability.execution_scope)
        if scope == "marvin-local":
            return {str(marvin.id): [marvin] for marvin in marvins}
        if scope == "host":
            grouped: dict[str, list[Marvin]] = defaultdict(list)
            for marvin in marvins:
                hostname = cast(str, marvin.hostname)
                grouped[hostname or str(marvin.id)].append(marvin)
            return dict(grouped)
        if scope in self.RESOURCE_SCOPES:
            return self._resolve_resource_targets(scope, marvins, selector)
        return {str(marvin.id): [marvin] for marvin in marvins}

    def _resolve_resource_targets(
        self,
        scope: str,
        marvins: list[Marvin],
        selector: dict[str, Any],
    ) -> dict[str, list[Marvin]]:
        """Group Marvins by attached resources matching the requested logical scope."""
        grouped: dict[str, list[Marvin]] = defaultdict(list)
        resource_ids = {str(resource_id) for resource_id in selector.get("resource_ids") or []}
        for marvin in marvins:
            for attachment in getattr(marvin, "resource_attachments").all():
                resource = attachment.resource
                if (
                    resource.organization_id != getattr(marvin, "organization_id")
                    or not resource.enabled
                ):
                    continue
                if resource.resource_type.scope_type != scope:
                    continue
                if resource_ids and str(resource.id) not in resource_ids:
                    continue
                grouped[str(resource.id)].append(marvin)

        if not grouped and _selector_has_resource_constraint(selector):
            raise SelectorRequiresResource("Selector resource_ids matched no attached resources.")
        if not grouped:
            raise SelectorRequiresResource(
                f"Capability scope {scope!r} requires a resource target."
            )
        return dict(grouped)
