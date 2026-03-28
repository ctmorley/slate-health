"""Unit tests for the payer rule engine.

Covers: condition evaluation, rule querying, date-bounded effectiveness,
and the payer registry.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app.core.payer.rule_engine import (
    PayerRuleEngine,
    evaluate_conditions,
    _get_nested_value,
)
from app.core.payer.registry import PayerRegistry, PayerNotFoundError
from app.models.payer import Payer, PayerRule


# ── Condition Evaluation Tests (pure logic, no DB) ──────────────────


class TestEvaluateConditions:
    def test_empty_conditions_match(self):
        assert evaluate_conditions({}, {"any": "context"}) is True

    def test_simple_equality(self):
        assert evaluate_conditions(
            {"name": "John"}, {"name": "John"}
        ) is True

    def test_simple_equality_mismatch(self):
        assert evaluate_conditions(
            {"name": "John"}, {"name": "Jane"}
        ) is False

    def test_eq_operator(self):
        assert evaluate_conditions(
            {"status": {"op": "eq", "value": "active"}},
            {"status": "active"},
        ) is True

    def test_ne_operator(self):
        assert evaluate_conditions(
            {"status": {"op": "ne", "value": "inactive"}},
            {"status": "active"},
        ) is True

    def test_gt_operator(self):
        assert evaluate_conditions(
            {"amount": {"op": "gt", "value": 100}},
            {"amount": 150},
        ) is True

    def test_gte_operator(self):
        assert evaluate_conditions(
            {"amount": {"op": "gte", "value": 100}},
            {"amount": 100},
        ) is True

    def test_lt_operator(self):
        assert evaluate_conditions(
            {"score": {"op": "lt", "value": 0.7}},
            {"score": 0.5},
        ) is True

    def test_lte_operator(self):
        assert evaluate_conditions(
            {"score": {"op": "lte", "value": 0.7}},
            {"score": 0.7},
        ) is True

    def test_in_operator(self):
        assert evaluate_conditions(
            {"code": {"op": "in", "value": ["A", "B", "C"]}},
            {"code": "B"},
        ) is True

    def test_in_operator_no_match(self):
        assert evaluate_conditions(
            {"code": {"op": "in", "value": ["A", "B", "C"]}},
            {"code": "D"},
        ) is False

    def test_not_in_operator(self):
        assert evaluate_conditions(
            {"code": {"op": "not_in", "value": ["X", "Y"]}},
            {"code": "A"},
        ) is True

    def test_contains_operator(self):
        assert evaluate_conditions(
            {"description": {"op": "contains", "value": "urgent"}},
            {"description": "This is an urgent case"},
        ) is True

    def test_starts_with_operator(self):
        assert evaluate_conditions(
            {"code": {"op": "starts_with", "value": "CPT"}},
            {"code": "CPT99213"},
        ) is True

    def test_exists_operator(self):
        assert evaluate_conditions(
            {"field": {"op": "exists", "value": True}},
            {"field": "anything"},
        ) is True
        assert evaluate_conditions(
            {"field": {"op": "exists", "value": True}},
            {"other": "value"},
        ) is False

    def test_nested_field_access(self):
        context = {"patient": {"coverage": {"status": "active"}}}
        assert evaluate_conditions(
            {"patient.coverage.status": {"op": "eq", "value": "active"}},
            context,
        ) is True

    def test_nested_field_missing(self):
        context = {"patient": {"name": "Jane"}}
        assert evaluate_conditions(
            {"patient.coverage.status": {"op": "eq", "value": "active"}},
            context,
        ) is False

    def test_multiple_conditions_all_match(self):
        conditions = {
            "status": "active",
            "amount": {"op": "gt", "value": 50},
            "type": {"op": "in", "value": ["A", "B"]},
        }
        context = {"status": "active", "amount": 100, "type": "A"}
        assert evaluate_conditions(conditions, context) is True

    def test_multiple_conditions_one_fails(self):
        conditions = {
            "status": "active",
            "amount": {"op": "gt", "value": 200},
        }
        context = {"status": "active", "amount": 100}
        assert evaluate_conditions(conditions, context) is False

    def test_all_grouping(self):
        conditions = {
            "all": [
                {"status": "active"},
                {"type": {"op": "eq", "value": "A"}},
            ]
        }
        assert evaluate_conditions(conditions, {"status": "active", "type": "A"}) is True
        assert evaluate_conditions(conditions, {"status": "active", "type": "B"}) is False

    def test_any_grouping(self):
        conditions = {
            "any": [
                {"status": "gold"},
                {"status": "platinum"},
            ]
        }
        assert evaluate_conditions(conditions, {"status": "gold"}) is True
        assert evaluate_conditions(conditions, {"status": "silver"}) is False

    def test_unknown_operator(self):
        assert evaluate_conditions(
            {"field": {"op": "regex_match", "value": ".*"}},
            {"field": "test"},
        ) is False

    def test_type_error_in_comparison(self):
        assert evaluate_conditions(
            {"field": {"op": "gt", "value": 10}},
            {"field": "not_a_number"},
        ) is False


class TestGetNestedValue:
    def test_simple_key(self):
        assert _get_nested_value({"a": 1}, "a") == 1

    def test_nested_key(self):
        assert _get_nested_value({"a": {"b": {"c": 3}}}, "a.b.c") == 3

    def test_missing_key(self):
        assert _get_nested_value({"a": 1}, "b") is None

    def test_missing_nested_key(self):
        assert _get_nested_value({"a": {"b": 1}}, "a.c") is None


# ── Payer Rule Engine (DB) Tests ────────────────────────────────────


class TestPayerRuleEngine:
    """Tests that use the database session fixture."""

    async def _create_payer_with_rules(
        self, session, num_rules=3, agent_type="eligibility"
    ):
        """Helper to create a payer with N rules."""
        payer = Payer(
            name="Test Payer",
            payer_id_code=f"TP-{uuid.uuid4().hex[:8]}",
            is_active=True,
        )
        session.add(payer)
        await session.flush()

        rules = []
        for i in range(num_rules):
            rule = PayerRule(
                payer_id=payer.id,
                agent_type=agent_type,
                rule_type=f"rule_type_{i}",
                description=f"Test rule {i}",
                conditions={"field": {"op": "eq", "value": f"val_{i}"}},
                actions={"action": f"action_{i}"},
                effective_date=date(2024, 1, 1),
                termination_date=None,
                version=1,
                is_active=True,
            )
            session.add(rule)
            rules.append(rule)

        await session.flush()
        return payer, rules

    async def test_get_rules_by_payer_and_agent_type(self, db_session):
        payer, rules = await self._create_payer_with_rules(
            db_session, num_rules=3, agent_type="eligibility"
        )
        # Also create a rule for a different agent type
        other_rule = PayerRule(
            payer_id=payer.id,
            agent_type="claims",
            rule_type="claims_rule",
            conditions={"x": "y"},
            effective_date=date(2024, 1, 1),
            is_active=True,
        )
        db_session.add(other_rule)
        await db_session.flush()

        engine = PayerRuleEngine(db_session)
        result = await engine.get_rules(
            payer_id=str(payer.id),
            agent_type="eligibility",
            as_of_date=date(2025, 6, 1),
        )
        assert len(result) == 3
        assert all(r.agent_type == "eligibility" for r in result)

    async def test_get_rules_respects_effective_date(self, db_session):
        payer = Payer(
            name="Date Payer",
            payer_id_code=f"DP-{uuid.uuid4().hex[:8]}",
            is_active=True,
        )
        db_session.add(payer)
        await db_session.flush()

        # Rule effective in the future
        future_rule = PayerRule(
            payer_id=payer.id,
            agent_type="eligibility",
            rule_type="future_rule",
            conditions={"a": "b"},
            effective_date=date(2030, 1, 1),
            is_active=True,
        )
        # Rule effective now
        current_rule = PayerRule(
            payer_id=payer.id,
            agent_type="eligibility",
            rule_type="current_rule",
            conditions={"c": "d"},
            effective_date=date(2024, 1, 1),
            is_active=True,
        )
        db_session.add_all([future_rule, current_rule])
        await db_session.flush()

        engine = PayerRuleEngine(db_session)
        result = await engine.get_rules(
            payer_id=str(payer.id),
            agent_type="eligibility",
            as_of_date=date(2025, 6, 1),
        )
        assert len(result) == 1
        assert result[0].rule_type == "current_rule"

    async def test_get_rules_filters_terminated(self, db_session):
        payer = Payer(
            name="Term Payer",
            payer_id_code=f"TM-{uuid.uuid4().hex[:8]}",
            is_active=True,
        )
        db_session.add(payer)
        await db_session.flush()

        terminated_rule = PayerRule(
            payer_id=payer.id,
            agent_type="claims",
            rule_type="old_rule",
            conditions={"x": "y"},
            effective_date=date(2023, 1, 1),
            termination_date=date(2024, 6, 1),
            is_active=True,
        )
        active_rule = PayerRule(
            payer_id=payer.id,
            agent_type="claims",
            rule_type="active_rule",
            conditions={"a": "b"},
            effective_date=date(2024, 1, 1),
            termination_date=None,
            is_active=True,
        )
        db_session.add_all([terminated_rule, active_rule])
        await db_session.flush()

        engine = PayerRuleEngine(db_session)
        result = await engine.get_rules(
            payer_id=str(payer.id),
            agent_type="claims",
            as_of_date=date(2025, 1, 1),
        )
        assert len(result) == 1
        assert result[0].rule_type == "active_rule"

    async def test_evaluate_rules_returns_matching(self, db_session):
        payer = Payer(
            name="Eval Payer",
            payer_id_code=f"EP-{uuid.uuid4().hex[:8]}",
            is_active=True,
        )
        db_session.add(payer)
        await db_session.flush()

        matching_rule = PayerRule(
            payer_id=payer.id,
            agent_type="prior_auth",
            rule_type="pa_required",
            conditions={"procedure_code": {"op": "in", "value": ["99213", "99214"]}},
            actions={"require_pa": True},
            effective_date=date(2024, 1, 1),
            is_active=True,
        )
        non_matching_rule = PayerRule(
            payer_id=payer.id,
            agent_type="prior_auth",
            rule_type="pa_exempt",
            conditions={"procedure_code": {"op": "eq", "value": "99201"}},
            actions={"require_pa": False},
            effective_date=date(2024, 1, 1),
            is_active=True,
        )
        db_session.add_all([matching_rule, non_matching_rule])
        await db_session.flush()

        engine = PayerRuleEngine(db_session)
        matches = await engine.evaluate_rules(
            payer_id=str(payer.id),
            agent_type="prior_auth",
            context={"procedure_code": "99213"},
            as_of_date=date(2025, 1, 1),
        )
        assert len(matches) == 1
        assert matches[0]["rule_type"] == "pa_required"
        assert matches[0]["actions"]["require_pa"] is True

    async def test_evaluate_rules_empty_when_no_match(self, db_session):
        payer = Payer(
            name="NoMatch Payer",
            payer_id_code=f"NM-{uuid.uuid4().hex[:8]}",
            is_active=True,
        )
        db_session.add(payer)
        await db_session.flush()

        rule = PayerRule(
            payer_id=payer.id,
            agent_type="claims",
            rule_type="specific_rule",
            conditions={"code": {"op": "eq", "value": "ABCDE"}},
            effective_date=date(2024, 1, 1),
            is_active=True,
        )
        db_session.add(rule)
        await db_session.flush()

        engine = PayerRuleEngine(db_session)
        matches = await engine.evaluate_rules(
            payer_id=str(payer.id),
            agent_type="claims",
            context={"code": "ZZZZZ"},
            as_of_date=date(2025, 1, 1),
        )
        assert len(matches) == 0

    async def test_check_rule_exists(self, db_session):
        payer = Payer(
            name="Exists Payer",
            payer_id_code=f"EX-{uuid.uuid4().hex[:8]}",
            is_active=True,
        )
        db_session.add(payer)
        await db_session.flush()

        rule = PayerRule(
            payer_id=payer.id,
            agent_type="prior_auth",
            rule_type="pa_required",
            conditions={"procedure_code": "99213"},
            effective_date=date(2024, 1, 1),
            is_active=True,
        )
        db_session.add(rule)
        await db_session.flush()

        engine = PayerRuleEngine(db_session)
        assert await engine.check_rule_exists(
            payer_id=str(payer.id),
            agent_type="prior_auth",
            rule_type="pa_required",
            context={"procedure_code": "99213"},
        ) is True

        assert await engine.check_rule_exists(
            payer_id=str(payer.id),
            agent_type="prior_auth",
            rule_type="pa_required",
            context={"procedure_code": "00000"},
        ) is False

    async def test_seed_10_rules_for_3_payers(self, db_session):
        """Seed 10 rules across 3 payers and verify correct rules returned."""
        payers = []
        for i in range(3):
            p = Payer(
                name=f"Payer {i}",
                payer_id_code=f"P{i}-{uuid.uuid4().hex[:6]}",
                is_active=True,
            )
            db_session.add(p)
            payers.append(p)
        await db_session.flush()

        # Create 10 rules: payer0 gets 4, payer1 gets 3, payer2 gets 3
        rule_counts = [4, 3, 3]
        for payer_idx, count in enumerate(rule_counts):
            for j in range(count):
                rule = PayerRule(
                    payer_id=payers[payer_idx].id,
                    agent_type="eligibility" if j % 2 == 0 else "claims",
                    rule_type=f"rule_{payer_idx}_{j}",
                    conditions={"payer_idx": payer_idx, "rule_idx": j},
                    effective_date=date(2024, 1, 1),
                    is_active=True,
                )
                db_session.add(rule)
        await db_session.flush()

        engine = PayerRuleEngine(db_session)

        # Payer 0 should have 2 eligibility rules (indices 0, 2)
        p0_elig = await engine.get_rules(
            payer_id=str(payers[0].id),
            agent_type="eligibility",
            as_of_date=date(2025, 1, 1),
        )
        assert len(p0_elig) == 2

        # Payer 1 should have 1 claims rule (index 1)
        p1_claims = await engine.get_rules(
            payer_id=str(payers[1].id),
            agent_type="claims",
            as_of_date=date(2025, 1, 1),
        )
        assert len(p1_claims) == 1


# ── Payer Registry Tests ───────────────────────────────────────────


class TestPayerRegistry:
    async def test_create_and_get_payer(self, db_session):
        registry = PayerRegistry(db_session)
        payer = await registry.create_payer(
            name="Aetna",
            payer_id_code="AETNA001",
            payer_type="commercial",
        )
        assert payer.name == "Aetna"
        assert payer.payer_id_code == "AETNA001"

        fetched = await registry.get_payer(str(payer.id))
        assert fetched.name == "Aetna"

    async def test_get_payer_by_code(self, db_session):
        registry = PayerRegistry(db_session)
        await registry.create_payer(
            name="BCBS",
            payer_id_code=f"BCBS-{uuid.uuid4().hex[:6]}",
        )
        payer = await registry.get_payer_by_code(
            (await registry.list_payers())[0].payer_id_code
        )
        assert payer is not None

    async def test_get_payer_not_found(self, db_session):
        registry = PayerRegistry(db_session)
        with pytest.raises(PayerNotFoundError):
            await registry.get_payer(str(uuid.uuid4()))

    async def test_list_payers(self, db_session):
        registry = PayerRegistry(db_session)
        for i in range(3):
            await registry.create_payer(
                name=f"Payer {i}",
                payer_id_code=f"LIST-{uuid.uuid4().hex[:6]}",
            )
        payers = await registry.list_payers()
        assert len(payers) >= 3

    async def test_update_payer(self, db_session):
        registry = PayerRegistry(db_session)
        payer = await registry.create_payer(
            name="Old Name",
            payer_id_code=f"UP-{uuid.uuid4().hex[:6]}",
        )
        updated = await registry.update_payer(
            str(payer.id), name="New Name"
        )
        assert updated.name == "New Name"

    async def test_cache_hit(self, db_session):
        registry = PayerRegistry(db_session)
        payer = await registry.create_payer(
            name="Cached",
            payer_id_code=f"CH-{uuid.uuid4().hex[:6]}",
        )
        # First access populates cache
        p1 = await registry.get_payer(str(payer.id))
        # Second access should use cache
        p2 = await registry.get_payer(str(payer.id))
        assert p1.id == p2.id

    async def test_create_rule(self, db_session):
        registry = PayerRegistry(db_session)
        payer = await registry.create_payer(
            name="Rule Payer",
            payer_id_code=f"RP-{uuid.uuid4().hex[:6]}",
        )
        rule = await registry.create_rule(
            payer_id=str(payer.id),
            agent_type="eligibility",
            rule_type="test_rule",
            conditions={"field": "value"},
            effective_date=date(2024, 1, 1),
        )
        assert rule.rule_type == "test_rule"

        rules = await registry.get_rules_for_payer(str(payer.id))
        assert len(rules) == 1

    async def test_deactivate_payer(self, db_session):
        registry = PayerRegistry(db_session)
        payer = await registry.create_payer(
            name="Deactivate Me",
            payer_id_code=f"DM-{uuid.uuid4().hex[:6]}",
        )
        assert payer.is_active is True

        deactivated = await registry.deactivate_payer(str(payer.id))
        assert deactivated.is_active is False

        # Should not appear in active-only listing
        active_payers = await registry.list_payers(active_only=True)
        assert not any(p.id == payer.id for p in active_payers)

        # Should appear when including inactive
        all_payers = await registry.list_payers(active_only=False)
        assert any(p.id == payer.id for p in all_payers)

    async def test_deactivate_nonexistent_payer(self, db_session):
        registry = PayerRegistry(db_session)
        with pytest.raises(PayerNotFoundError):
            await registry.deactivate_payer(str(uuid.uuid4()))

    async def test_delete_payer(self, db_session):
        registry = PayerRegistry(db_session)
        payer = await registry.create_payer(
            name="Delete Me",
            payer_id_code=f"DEL-{uuid.uuid4().hex[:6]}",
        )
        payer_id = str(payer.id)

        await registry.delete_payer(payer_id)

        # Should be completely gone
        with pytest.raises(PayerNotFoundError):
            # Invalidate cache first so we get a DB lookup
            registry._invalidate_cache(payer_id)
            await registry.get_payer(payer_id)

    async def test_delete_nonexistent_payer(self, db_session):
        registry = PayerRegistry(db_session)
        with pytest.raises(PayerNotFoundError):
            await registry.delete_payer(str(uuid.uuid4()))
