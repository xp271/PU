"""Tests for SessionPermissionStore + session-scoped permission memory."""

from __future__ import annotations

import pytest

from agent_core.permissions import (
    PermissionEngine,
    PermissionOutcome,
    SessionPermissionStore,
)
from agent_core.permissions.engine import _coerce_outcome


# ---- SessionPermissionStore ------------------------------------------------


def test_store_add_allow_and_deny():
    store = SessionPermissionStore()
    store.add_allow("Bash")
    store.add_deny("Write")
    rules = store.rules()
    assert {r.tool_pattern for r in rules} == {"Bash", "Write"}
    by_tool = {r.tool_pattern: r for r in rules}
    assert by_tool["Bash"].action == "allow"
    assert by_tool["Write"].action == "deny"
    assert all(r.source == "session" for r in rules)


def test_store_add_is_idempotent():
    store = SessionPermissionStore()
    store.add_allow("Bash")
    store.add_allow("Bash")
    assert len(store.rules()) == 1


def test_store_deny_outranks_allow_by_priority():
    store = SessionPermissionStore()
    store.add_allow("Bash")
    store.add_deny("Bash")
    by_action = {r.action: r.priority for r in store.rules()}
    assert by_action["deny"] < by_action["allow"]


def test_store_clear():
    store = SessionPermissionStore()
    store.add_allow("Bash")
    store.clear()
    assert store.rules() == []


def test_store_rules_returns_copy():
    store = SessionPermissionStore()
    store.add_allow("Bash")
    store.rules().clear()
    assert len(store.rules()) == 1


# ---- PermissionOutcome / _coerce_outcome -----------------------------------


def test_outcome_enum_values():
    assert {o.value for o in PermissionOutcome} == {
        "allow_once",
        "deny_once",
        "allow_session",
        "deny_session",
    }


def test_coerce_outcome_passthrough():
    assert _coerce_outcome(PermissionOutcome.ALLOW_SESSION) is PermissionOutcome.ALLOW_SESSION


def test_coerce_outcome_bool_backward_compat():
    assert _coerce_outcome(True) is PermissionOutcome.ALLOW_ONCE
    assert _coerce_outcome(False) is PermissionOutcome.DENY_ONCE


# ---- Engine: session rule matching -----------------------------------------


@pytest.mark.asyncio
async def test_engine_session_allow_rule_short_circuits():
    store = SessionPermissionStore()
    store.add_allow("Bash")
    engine = PermissionEngine(mode="ask", session_store=store)
    # No callback: would normally fall to default deny for destructive tools,
    # but the session-allow rule matches at step 1–4 first.
    decision = await engine.check(
        "Bash", {"command": "ls"}, callback=None, tool_is_destructive=True
    )
    assert decision.allowed is True
    assert decision.source == "rule"


@pytest.mark.asyncio
async def test_engine_session_deny_outranks_builtin_allow():
    # Read is allowed by a builtin rule; a session-deny (priority 10) must win.
    store = SessionPermissionStore()
    store.add_deny("Read")
    engine = PermissionEngine(mode="ask", session_store=store)
    decision = await engine.check("Read", {}, callback=None, tool_is_read_only=True)
    assert decision.allowed is False
    assert decision.source == "rule"


@pytest.mark.asyncio
async def test_engine_allow_session_writeback_survives_engine_rebuild():
    """Core regression: a remembered allow lives in the store, not the engine,
    so a freshly-rebuilt engine sharing the same store auto-allows."""
    store = SessionPermissionStore()

    async def cb(tool_name, tool_input):
        return PermissionOutcome.ALLOW_SESSION

    engine1 = PermissionEngine(mode="ask", session_store=store)
    d1 = await engine1.check("Bash", {"command": "ls"}, callback=cb, tool_is_destructive=True)
    assert d1.allowed is True
    assert store.rules()  # remembered

    # New engine instance (as happens each prompt), same store, no callback.
    engine2 = PermissionEngine(mode="ask", session_store=store)
    called = False

    async def cb2(tool_name, tool_input):
        nonlocal called
        called = True
        return PermissionOutcome.DENY_ONCE

    d2 = await engine2.check("Bash", {"command": "rm -rf /"}, callback=cb2, tool_is_destructive=True)
    assert d2.allowed is True
    assert d2.source == "rule"
    assert called is False  # never reached the callback


@pytest.mark.asyncio
async def test_engine_deny_session_writeback():
    store = SessionPermissionStore()

    async def cb(tool_name, tool_input):
        return PermissionOutcome.DENY_SESSION

    engine = PermissionEngine(mode="ask", session_store=store)
    d = await engine.check("Bash", {}, callback=cb, tool_is_destructive=True)
    assert d.allowed is False
    assert any(r.action == "deny" and r.tool_pattern == "Bash" for r in store.rules())


@pytest.mark.asyncio
async def test_engine_allow_once_does_not_write_store():
    store = SessionPermissionStore()

    async def cb(tool_name, tool_input):
        return PermissionOutcome.ALLOW_ONCE

    engine = PermissionEngine(mode="ask", session_store=store)
    d = await engine.check("Bash", {}, callback=cb, tool_is_destructive=True)
    assert d.allowed is True
    assert store.rules() == []


@pytest.mark.asyncio
async def test_engine_keyboardinterrupt_is_deny_once():
    async def cb(tool_name, tool_input):
        raise KeyboardInterrupt

    engine = PermissionEngine(mode="ask")
    d = await engine.check("Bash", {}, callback=cb, tool_is_destructive=True)
    assert d.allowed is False
    assert d.source == "callback"


@pytest.mark.asyncio
async def test_engine_bool_callback_backward_compat():
    engine = PermissionEngine(mode="ask")

    async def allow_cb(tool_name, tool_input):
        return True

    async def deny_cb(tool_name, tool_input):
        return False

    assert (await engine.check("Bash", {}, callback=allow_cb, tool_is_destructive=True)).allowed
    assert not (await engine.check("Bash", {}, callback=deny_cb, tool_is_destructive=True)).allowed
