"""Blocking semantics for tasks parked in ``todo`` (hermesteam#11).

Before this, ``block_task`` only accepted ``running``/``ready`` sources, so a
card that was merely queued behind a parent could not be stopped by an
operator — the only options were to race the dispatcher until the card became
``ready``, or archive it and lose the audit trail.

Covered here:
  * a dependency wait on a ``todo`` card is a no-op success (already parked
    where a dependency wait belongs) and must not churn state
  * an operator block moves ``todo`` -> ``blocked``
  * unblock returns the card to ``todo`` while its parent is unfinished, and
    the card only reaches ``ready`` once the parent completes
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _parent_and_child(conn):
    parent = kb.create_task(conn, title="parent")
    child = kb.create_task(conn, title="child", parents=[parent])
    return parent, child


def test_child_starts_in_todo(kanban_home):
    with kb.connect_closing() as conn:
        _, child = _parent_and_child(conn)
        assert kb.get_task(conn, child).status == "todo"


def test_dependency_block_on_todo_is_noop_success(kanban_home):
    with kb.connect_closing() as conn:
        _, child = _parent_and_child(conn)
        before = kb.get_task(conn, child)

        assert kb.block_task(conn, child, reason="waiting", kind="dependency") is True

        after = kb.get_task(conn, child)
        assert after.status == "todo"
        # No churn: a dependency wait on an already-parked card must not
        # manufacture a lifecycle transition.
        assert after.block_kind == before.block_kind


def test_operator_block_moves_todo_to_blocked(kanban_home):
    with kb.connect_closing() as conn:
        _, child = _parent_and_child(conn)

        assert kb.block_task(
            conn, child, reason="operator stop", kind="needs_input"
        ) is True
        assert kb.get_task(conn, child).status == "blocked"


@pytest.mark.parametrize("kind", ["needs_input", "capability", "transient", None])
def test_operator_block_kinds_all_accept_todo(kanban_home, kind):
    with kb.connect_closing() as conn:
        _, child = _parent_and_child(conn)
        assert kb.block_task(conn, child, reason="stop", kind=kind) is True
        assert kb.get_task(conn, child).status == "blocked"


def test_unblock_returns_to_todo_then_ready_after_parent(kanban_home):
    with kb.connect_closing() as conn:
        parent, child = _parent_and_child(conn)
        kb.block_task(conn, child, reason="operator stop", kind="needs_input")
        assert kb.get_task(conn, child).status == "blocked"

        assert kb.unblock_task(conn, child) is True
        # Parent is still open, so the card goes back to waiting — NOT ready.
        assert kb.get_task(conn, child).status == "todo"

        kb.complete_task(conn, parent)
        assert kb.get_task(conn, child).status == "ready"


def test_block_unknown_task_is_false(kanban_home):
    with kb.connect_closing() as conn:
        assert kb.block_task(conn, "t_missing", reason="x", kind="needs_input") is False


def test_done_task_is_not_blockable(kanban_home):
    with kb.connect_closing() as conn:
        task = kb.create_task(conn, title="standalone")
        kb.complete_task(conn, task)
        assert kb.get_task(conn, task).status == "done"
        # Blocking must not resurrect finished work.
        assert kb.block_task(conn, task, reason="x", kind="needs_input") is False
        assert kb.get_task(conn, task).status == "done"
