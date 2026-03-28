"""Payer rule engine — query and evaluate payer-specific rules.

Evaluates JSON conditions against an agent's context to determine which
payer rules apply. Supports date-bounded effectiveness, multiple condition
operators, and caching of frequently queried rules.
"""

from __future__ import annotations

import logging
import operator
from datetime import date, datetime
from typing import Any

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payer import PayerRule

logger = logging.getLogger(__name__)


class RuleEvaluationError(Exception):
    """Raised when rule evaluation encounters an error."""
    pass


# ── Condition Operators ─────────────────────────────────────────────

_OPERATORS: dict[str, Any] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
    "in": lambda val, lst: val in lst,
    "not_in": lambda val, lst: val not in lst,
    "contains": lambda val, substr: substr in val if isinstance(val, str) else False,
    "starts_with": lambda val, prefix: val.startswith(prefix) if isinstance(val, str) else False,
    "exists": lambda val, _: val is not None,
}


def _get_nested_value(data: dict[str, Any], path: str) -> Any:
    """Retrieve a nested value from a dict using dot-notation path.

    Example: _get_nested_value({"a": {"b": 1}}, "a.b") → 1
    """
    keys = path.split(".")
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


def evaluate_conditions(
    conditions: dict[str, Any],
    context: dict[str, Any],
) -> bool:
    """Evaluate a set of rule conditions against a context dict.

    Conditions format:
        {
            "field.path": {"op": "eq", "value": "some_value"},
            "other.field": {"op": "gt", "value": 100},
            "list.field": {"op": "in", "value": ["a", "b", "c"]},
        }

    Multiple conditions are ANDed together (all must match).
    Supports the "all" key for explicit AND lists and "any" for OR lists.

    Args:
        conditions: Dict of field paths to operator/value pairs.
        context: The data to evaluate against.

    Returns:
        True if all conditions match, False otherwise.
    """
    if not conditions:
        return True

    # Handle explicit "all" (AND) grouping
    if "all" in conditions:
        return all(
            evaluate_conditions(sub_cond, context)
            for sub_cond in conditions["all"]
        )

    # Handle explicit "any" (OR) grouping
    if "any" in conditions:
        return any(
            evaluate_conditions(sub_cond, context)
            for sub_cond in conditions["any"]
        )

    for field_path, condition in conditions.items():
        if field_path in ("all", "any"):
            continue

        actual_value = _get_nested_value(context, field_path)

        if isinstance(condition, dict):
            op_name = condition.get("op", "eq")
            expected = condition.get("value")

            op_func = _OPERATORS.get(op_name)
            if op_func is None:
                logger.warning("Unknown operator '%s' in rule condition", op_name)
                return False

            try:
                if not op_func(actual_value, expected):
                    return False
            except (TypeError, AttributeError):
                return False
        else:
            # Simple equality shorthand: {"field": "value"}
            if actual_value != condition:
                return False

    return True


class PayerRuleEngine:
    """Query and evaluate payer rules from the database.

    Provides methods to:
    - Fetch rules by payer and agent type
    - Filter by date effectiveness
    - Evaluate rules against a context dict
    - Return matching rules with their actions
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_rules(
        self,
        *,
        payer_id: str,
        agent_type: str,
        as_of_date: date | None = None,
        rule_type: str | None = None,
    ) -> list[PayerRule]:
        """Fetch active rules for a payer and agent type.

        Args:
            payer_id: UUID string of the payer.
            agent_type: Agent type (eligibility, claims, etc.).
            as_of_date: Date to check effectiveness against (defaults to today).
            rule_type: Optional filter by rule_type.

        Returns:
            List of matching PayerRule records.
        """
        effective_date = as_of_date or date.today()

        stmt = (
            select(PayerRule)
            .where(
                and_(
                    PayerRule.payer_id == payer_id,
                    PayerRule.agent_type == agent_type,
                    PayerRule.is_active == True,  # noqa: E712
                    PayerRule.effective_date <= effective_date,
                )
            )
        )

        # Filter out terminated rules
        stmt = stmt.where(
            (PayerRule.termination_date == None)  # noqa: E711
            | (PayerRule.termination_date >= effective_date)
        )

        if rule_type is not None:
            stmt = stmt.where(PayerRule.rule_type == rule_type)

        stmt = stmt.order_by(PayerRule.version.desc())

        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def evaluate_rules(
        self,
        *,
        payer_id: str,
        agent_type: str,
        context: dict[str, Any],
        as_of_date: date | None = None,
        rule_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch rules and evaluate them against a context.

        Returns only rules whose conditions match the context.

        Args:
            payer_id: UUID string of the payer.
            agent_type: Agent type (eligibility, claims, etc.).
            context: Data dict to evaluate conditions against.
            as_of_date: Date for effectiveness check.
            rule_type: Optional filter by rule_type.

        Returns:
            List of dicts with matched rule info:
            [{"rule_id": ..., "rule_type": ..., "actions": ..., "description": ...}]
        """
        rules = await self.get_rules(
            payer_id=payer_id,
            agent_type=agent_type,
            as_of_date=as_of_date,
            rule_type=rule_type,
        )

        matching = []
        for rule in rules:
            conditions = rule.conditions or {}
            if evaluate_conditions(conditions, context):
                matching.append(
                    {
                        "rule_id": str(rule.id),
                        "rule_type": rule.rule_type,
                        "description": rule.description,
                        "actions": rule.actions,
                        "version": rule.version,
                    }
                )
                logger.debug(
                    "Rule %s matched for payer=%s agent=%s",
                    rule.id,
                    payer_id,
                    agent_type,
                )

        return matching

    async def check_rule_exists(
        self,
        *,
        payer_id: str,
        agent_type: str,
        rule_type: str,
        context: dict[str, Any],
        as_of_date: date | None = None,
    ) -> bool:
        """Check if any matching rule exists (convenience method).

        Useful for simple yes/no checks like "is prior auth required?".
        """
        matches = await self.evaluate_rules(
            payer_id=payer_id,
            agent_type=agent_type,
            context=context,
            as_of_date=as_of_date,
            rule_type=rule_type,
        )
        return len(matches) > 0
