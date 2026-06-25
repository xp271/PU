"""Tests for the SessionState runtime container."""

from __future__ import annotations

from agent_core.permissions import SessionPermissionStore
from agent_core.session import SessionState


def test_defaults():
    s = SessionState()
    assert s.messages == []
    assert isinstance(s.permission_store, SessionPermissionStore)
    assert s.session_id
    assert s.transcript_path is None


def test_distinct_sessions_have_distinct_stores():
    a, b = SessionState(), SessionState()
    assert a.permission_store is not b.permission_store
    assert a.session_id != b.session_id


def test_snapshot_returns_copy():
    s = SessionState(messages=[{"role": "user", "content": "hi"}])
    snap = s.snapshot_for_next_turn()
    snap.append({"role": "user", "content": "extra"})
    assert len(s.messages) == 1


def test_record_turn_result_replaces_history():
    s = SessionState(messages=[{"role": "user", "content": "old"}])
    final = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "reply"},
    ]
    s.record_turn_result(final)
    assert s.messages == final
    # stored as a copy, not the same list object
    assert s.messages is not final


def test_record_turn_result_none_keeps_history():
    s = SessionState(messages=[{"role": "user", "content": "old"}])
    s.record_turn_result(None)
    assert s.messages == [{"role": "user", "content": "old"}]


def test_clear_history_keeps_permission_memory():
    s = SessionState()
    s.permission_store.add_allow("Bash")
    s.messages.append({"role": "user", "content": "hi"})
    s.clear_history()
    assert s.messages == []
    assert s.permission_store.rules()  # permissions survive /clear
