"""Deterministic evaluation engine for Check results."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class HealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Finding:
    severity: Severity
    message: str
    path: str = ""
    expected: Any = None
    actual: Any = None


@dataclass
class EvaluationResult:
    state: HealthState
    findings: list[Finding] = field(default_factory=list)
    confidence: float = 1.0


class EvaluationError(Exception):
    """Raised when a rule cannot be evaluated."""


class EvaluationEngine:
    """Evaluate execution results against deterministic rules."""

    OPERATORS = {
        "gt": lambda a, b: a > b,
        "gte": lambda a, b: a >= b,
        "lt": lambda a, b: a < b,
        "lte": lambda a, b: a <= b,
        "eq": lambda a, b: a == b,
        "ne": lambda a, b: a != b,
    }

    AGGREGATIONS = {
        "avg": lambda vals: sum(vals) / len(vals) if vals else 0,
        "sum": sum,
        "max": max,
        "min": min,
        "count": len,
    }

    @classmethod
    def evaluate(cls, execution_result: dict, rules: list[dict]) -> EvaluationResult:
        """Evaluate execution_result against a list of rules.

        Returns EvaluationResult with the worst state from all rules.
        Does NOT mutate execution_result.
        """
        findings = []
        worst_state = HealthState.HEALTHY

        for rule in rules:
            try:
                finding = cls._evaluate_rule(execution_result, rule)
                if finding:
                    findings.append(finding)
                    worst_state = cls._worse_state(worst_state, finding.severity)
            except EvaluationError:
                raise
            except Exception as exc:
                raise EvaluationError(f"Rule evaluation failed: {exc}") from exc

        return EvaluationResult(
            state=worst_state if findings else HealthState.HEALTHY,
            findings=findings,
            confidence=1.0,
        )

    @classmethod
    def _evaluate_rule(cls, execution_result: dict, rule: dict) -> Finding | None:
        rule_type = rule.get("type")
        if rule_type == "numeric_comparison":
            return cls._numeric_comparison(execution_result, rule)
        if rule_type == "boolean_expression":
            return cls._boolean_expression(execution_result, rule)
        if rule_type == "aggregation":
            return cls._aggregation(execution_result, rule)
        if rule_type == "presence":
            return cls._presence(execution_result, rule)
        raise EvaluationError(f"Unknown rule type: {rule_type}")

    @classmethod
    def _numeric_comparison(cls, execution_result: dict, rule: dict) -> Finding | None:
        path = rule["path"]
        operator = rule["operator"]
        threshold = rule["threshold"]

        if operator not in cls.OPERATORS:
            raise EvaluationError(f"Unknown operator: {operator}")

        value = cls._get_value_at_path(execution_result, path)
        if value is None:
            raise EvaluationError(f"Path not found: {path}")

        try:
            value = float(value)
            threshold = float(threshold)
        except (TypeError, ValueError) as exc:
            raise EvaluationError(f"Cannot compare non-numeric values: {exc}") from exc

        result = cls.OPERATORS[operator](value, threshold)
        if result:
            severity = cls._threshold_to_severity(rule)
            return Finding(
                severity=severity,
                message=rule.get("message", f"{path} {operator} {threshold}: {value}"),
                path=path,
                expected=f"{operator} {threshold}",
                actual=value,
            )
        return None

    @classmethod
    def _boolean_expression(cls, execution_result: dict, rule: dict) -> Finding | None:
        expression = rule["expression"]
        conditions = rule.get("conditions", [])

        condition_results = {}
        for i, condition in enumerate(conditions):
            cond_rule = {"type": condition.get("type", "numeric_comparison"), **condition}
            try:
                finding = cls._evaluate_rule(execution_result, cond_rule)
                condition_results[f"cond_{i}"] = finding is not None
            except EvaluationError:
                condition_results[f"cond_{i}"] = False

        expression_lower = expression.lower()
        if "and" in expression_lower:
            result = all(condition_results.values())
        elif "or" in expression_lower:
            result = any(condition_results.values())
        else:
            result = next(iter(condition_results.values()), False)

        if result:
            return Finding(
                severity=Severity.CRITICAL if "critical" in expression_lower else Severity.WARNING,
                message=rule.get("message", f"Boolean expression '{expression}' is TRUE"),
                path=expression,
                expected=False,
                actual=True,
            )
        return None

    @classmethod
    def _aggregation(cls, execution_result: dict, rule: dict) -> Finding | None:
        path = rule["path"]
        agg_operator = rule["operator"]
        threshold = rule["threshold"]

        if agg_operator not in cls.AGGREGATIONS:
            raise EvaluationError(f"Unknown aggregation operator: {agg_operator}")

        values = cls._get_values_at_wildcard_path(execution_result, path)
        if not values:
            raise EvaluationError(f"No values found at path: {path}")

        try:
            values = [float(v) for v in values]
            threshold = float(threshold)
        except (TypeError, ValueError) as exc:
            raise EvaluationError(f"Cannot aggregate non-numeric values: {exc}") from exc

        aggregated = cls.AGGREGATIONS[agg_operator](values)
        comparison_op = rule.get("comparison_operator", "gt")

        if comparison_op not in cls.OPERATORS:
            raise EvaluationError(f"Unknown comparison operator: {comparison_op}")

        result = cls.OPERATORS[comparison_op](aggregated, threshold)
        if result:
            return Finding(
                severity=cls._threshold_to_severity(rule),
                message=rule.get(
                    "message",
                    f"{path} {agg_operator}={aggregated:.2f} {comparison_op} {threshold}",
                ),
                path=path,
                expected=f"{comparison_op} {threshold}",
                actual=aggregated,
            )
        return None

    @classmethod
    def _presence(cls, execution_result: dict, rule: dict) -> Finding | None:
        path = rule["path"]
        expected = rule.get("expected", True)

        value = cls._get_value_at_path(execution_result, path)
        present = value is not None and (not isinstance(value, bool) or value)

        if present != expected:
            return Finding(
                severity=Severity.WARNING,
                message=rule.get("message", f"Presence check failed at {path}"),
                path=path,
                expected=expected,
                actual=present,
            )
        return None

    @classmethod
    def _get_value_at_path(cls, data: dict, path: str) -> Any:
        parts = path.split(".")
        current = data
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                try:
                    idx = int(part)
                    current = current[idx] if 0 <= idx < len(current) else None
                except (ValueError, IndexError):
                    return None
            else:
                return None
            if current is None:
                return None
        return current

    @classmethod
    def _get_values_at_wildcard_path(cls, data: dict, path: str) -> list[Any]:
        parts = path.split(".")
        return cls._get_values_recursive(data, parts)

    @classmethod
    def _get_values_recursive(cls, current: Any, parts: list[str]) -> list[Any]:
        if not parts:
            return [current] if current is not None else []

        part = parts[0]
        remaining = parts[1:]

        if part == "*":
            if isinstance(current, list):
                results = []
                for item in current:
                    results.extend(cls._get_values_recursive(item, remaining))
                return results
            if isinstance(current, dict):
                results = []
                for item in current.values():
                    results.extend(cls._get_values_recursive(item, remaining))
                return results
            return []

        if isinstance(current, dict):
            next_val = current.get(part)
            return cls._get_values_recursive(next_val, remaining) if next_val is not None else []
        if isinstance(current, list):
            try:
                idx = int(part)
                next_val = current[idx] if 0 <= idx < len(current) else None
                return (
                    cls._get_values_recursive(next_val, remaining) if next_val is not None else []
                )
            except ValueError:
                return []
        return []

    @classmethod
    def _worse_state(cls, current: HealthState, severity: Severity) -> HealthState:
        order = {
            HealthState.HEALTHY: 0,
            HealthState.DEGRADED: 1,
            HealthState.CRITICAL: 2,
            HealthState.UNKNOWN: 3,
        }
        sev_order = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}
        current_val = order.get(current, 0)
        new_val = sev_order.get(severity, 0)
        if new_val >= current_val:
            return cls._severity_to_state(severity)
        return current

    @classmethod
    def _severity_to_state(cls, severity: Severity) -> HealthState:
        mapping = {
            Severity.INFO: HealthState.HEALTHY,
            Severity.WARNING: HealthState.DEGRADED,
            Severity.CRITICAL: HealthState.CRITICAL,
        }
        return mapping.get(severity, HealthState.UNKNOWN)

    @classmethod
    def _threshold_to_severity(cls, rule: dict) -> Severity:
        return Severity(rule.get("severity", "warning"))
