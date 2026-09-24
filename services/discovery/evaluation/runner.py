"""Benchmark runner for capability discovery retrieval evaluation."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class TestCase:
    """A single retrieval evaluation test case."""

    id: str
    query: str
    filters: dict[str, Any] = field(default_factory=dict)
    expected: list[dict[str, Any]] = field(default_factory=list)
    not_expected: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TestCase:
        return cls(
            id=data["id"],
            query=data["query"],
            filters=data.get("filters", {}),
            expected=data.get("expected", []),
            not_expected=data.get("not_expected", []),
        )


@dataclass
class BenchmarkResult:
    """Aggregated results for a benchmark run."""

    config_name: str
    recall_at_5: float
    precision_at_5: float
    mrr: float
    ndcg_at_5: float
    test_results: list[dict[str, Any]] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            "# Capability Discovery Benchmark Results",
            "",
            f"## Config: {self.config_name}",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Recall@5 | {self.recall_at_5:.3f} |",
            f"| Precision@5 | {self.precision_at_5:.3f} |",
            f"| MRR | {self.mrr:.3f} |",
            f"| nDCG@5 | {self.ndcg_at_5:.3f} |",
            "",
            "## Per-Test Results",
            "",
            "| Test | Query | Recall | Top Results |",
            "|------|-------|--------|-------------|",
        ]
        for tr in self.test_results:
            results_str = ", ".join(tr.get("results", [])[:3])
            lines.append(
                f"| {tr['id']} | {tr['query'][:30]}... | {tr['recall']:.2f} | {results_str} |"
            )
        return "\n".join(lines)


class BenchmarkRunner:
    """Run retrieval benchmark against a DiscoveryEngine."""

    def __init__(self, test_cases_path: str | Path):
        self.test_cases_path = Path(test_cases_path)
        self.test_cases: list[TestCase] = []
        self._load_test_cases()

    def _load_test_cases(self) -> None:
        with open(self.test_cases_path) as f:
            data = yaml.safe_load(f)
        raw_cases = data.get("test_cases", []) if isinstance(data, dict) else []
        self.test_cases = [TestCase.from_dict(tc) for tc in raw_cases]
        logger.info("Loaded %d test cases from %s", len(self.test_cases), self.test_cases_path)

    def run(
        self,
        discovery_engine: Any,
        organization: Any,
        config_name: str = "unknown",
    ) -> BenchmarkResult:
        """Run benchmark against a DiscoveryEngine instance.

        The discovery_engine must have a find_capabilities(query, filters, limit) method.
        """
        test_results: list[dict[str, Any]] = []
        recalls: list[float] = []
        precisions: list[float] = []
        mrrs: list[float] = []
        ndcgs: list[float] = []

        for tc in self.test_cases:
            # Skip empty queries
            if not tc.query.strip():
                continue

            results = discovery_engine.find_capabilities(
                organization=organization,
                query=tc.query,
                filters=tc.filters,
                limit=5,
            )
            result_names = [r["name"] for r in results["results"]]

            expected_map = {e["name"]: e["relevance"] for e in tc.expected}
            expected_names = set(expected_map.keys())
            not_expected_names = set(tc.not_expected)

            # Calculate per-test metrics
            relevant_in_top_5 = set(result_names) & expected_names
            irrelevant_in_top_5 = set(result_names) & not_expected_names

            recall = len(relevant_in_top_5) / len(expected_names) if expected_names else 1.0
            precision = len(relevant_in_top_5) / max(len(result_names), 1)

            # MRR: reciprocal rank of first relevant result
            mrr = 0.0
            for rank, name in enumerate(result_names, start=1):
                if name in expected_names:
                    mrr = 1.0 / rank
                    break

            # nDCG@5
            dcg = 0.0
            for rank, name in enumerate(result_names[:5], start=1):
                rel = expected_map.get(name, 0.0)
                dcg += rel / math.log2(rank + 1)

            idcg = 0.0
            sorted_rels = sorted(expected_map.values(), reverse=True)
            for rank, rel in enumerate(sorted_rels[:5], start=1):
                idcg += rel / math.log2(rank + 1)

            ndcg = dcg / idcg if idcg > 0 else 0.0

            recalls.append(recall)
            precisions.append(precision)
            mrrs.append(mrr)
            ndcgs.append(ndcg)

            test_results.append(
                {
                    "id": tc.id,
                    "query": tc.query,
                    "results": result_names,
                    "recall": recall,
                    "precision": precision,
                    "mrr": mrr,
                    "ndcg": ndcg,
                    "irrelevant_found": list(irrelevant_in_top_5),
                }
            )

        avg_recall = sum(recalls) / len(recalls) if recalls else 0.0
        avg_precision = sum(precisions) / len(precisions) if precisions else 0.0
        avg_mrr = sum(mrrs) / len(mrrs) if mrrs else 0.0
        avg_ndcg = sum(ndcgs) / len(ndcgs) if ndcgs else 0.0

        return BenchmarkResult(
            config_name=config_name,
            recall_at_5=avg_recall,
            precision_at_5=avg_precision,
            mrr=avg_mrr,
            ndcg_at_5=avg_ndcg,
            test_results=test_results,
        )

    def compare(
        self,
        results: list[BenchmarkResult],
    ) -> str:
        """Generate a comparison matrix of multiple benchmark runs."""
        lines = [
            "# Benchmark Comparison Matrix",
            "",
            "| Config | Recall@5 | Precision@5 | MRR | nDCG@5 |",
            "|--------|----------|-------------|-----|--------|",
        ]
        for r in results:
            lines.append(
                f"| {r.config_name} | {r.recall_at_5:.3f} | "
                f"{r.precision_at_5:.3f} | {r.mrr:.3f} | {r.ndcg_at_5:.3f} |"
            )

        # Per-test breakdown
        lines.extend(
            [
                "",
                "## Per-Test Breakdown",
                "",
            ]
        )

        # Build a map of test_id -> {config_name -> recall}
        test_recalls: dict[str, dict[str, float]] = defaultdict(dict)
        for r in results:
            for tr in r.test_results:
                test_recalls[tr["id"]][r.config_name] = tr["recall"]

        for test_id, config_map in test_recalls.items():
            lines.append(f"### {test_id}")
            for config_name, recall in config_map.items():
                lines.append(f"- {config_name}: {recall:.3f}")
            lines.append("")

        return "\n".join(lines)
