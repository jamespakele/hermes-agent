"""Tests for kb.decompose_triage_task — the DB-layer atomic fan-out
from the triage column. LLM-free by design.
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


def _create_triage(conn, title="rough idea", body=None, assignee=None, tenant=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        tenant=tenant,
        triage=True,
    )


def test_decompose_creates_children_and_promotes_root(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="ship a feature")
        assert kb.get_task(conn, tid).status == "triage"

    children = [
        {"title": "research", "body": "look at prior art", "assignee": "researcher", "parents": []},
        {"title": "build it", "body": "write code", "assignee": "engineer", "parents": [0]},
    ]
    with kb.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=children,
            author="decomposer",
        )
    assert child_ids is not None
    assert len(child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, child_ids[0])
        c1 = kb.get_task(conn, child_ids[1])

    # Root flipped to todo with orchestrator assignee, gated by children.
    assert root.status == "todo"
    assert root.assignee == "orchestrator"
    # First child has no internal parents → ready on recompute_ready.
    assert c0.status == "ready"
    assert c0.assignee == "researcher"
    # Second child has parents=[0] → stays in todo until c0 completes.
    assert c1.status == "todo"
    assert c1.assignee == "engineer"


def test_decompose_records_audit_comment_and_event(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "task A", "assignee": "researcher"}],
            author="alice",
        )
    assert child_ids is not None

    with kb.connect() as conn:
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)

    assert any("Decomposed into" in (c.body or "") for c in comments)
    assert any(ev.kind == "decomposed" for ev in events)


def test_decompose_test_epic_impl_precedes_tests(kanban_home):
    # Simulates the dispatch script: an impl story already exists as a child
    # of the triage epic before decompose runs. The test-executor must wait
    # on the impl story (by id), and the epic must NOT gate the impl story —
    # otherwise the graph deadlocks: root waits on executor, executor waits
    # on impl, impl waits on root (2 occurrences, t_495e5d6f / t_535273e8).
    with kb.connect() as conn:
        root = _create_triage(conn, title="Test Epic: Add Widgets")
        impl = kb.create_task(
            conn,
            title="s01: Widget API endpoint",
            parents=[root],
            triage=False,
        )
        children = [
            {"title": "test-plan", "assignee": "tester", "parents": []},
            {"title": "test-executor", "assignee": "tester", "parents": [0, impl]},
        ]
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=children,
            author="decomposer",
        )
        assert child_ids is not None
        plan_id, executor_id = child_ids

        # The epic no longer gates the impl story it depends on.
        assert impl not in kb.child_ids(conn, root)
        # The test-executor waits on the impl story — impl precedes tests.
        assert impl in kb.parent_ids(conn, executor_id)
        # The root still waits on every new child (root is the child of the
        # executor link — task_links(parent_id=executor, child_id=root)).
        assert executor_id in kb.parent_ids(conn, root)

        # Whole graph is acyclic: no node is reachable from itself.
        for start in [root, impl, plan_id, executor_id]:
            stack = [start]
            seen = set()
            while stack:
                n = stack.pop()
                if n in seen:
                    continue
                seen.add(n)
                for c in kb.child_ids(conn, n):
                    assert c != start, f"cycle: {start} reachable from itself via {n}"
                    stack.append(c)

        # recompute_ready: the impl story promotes to ready (released from
        # root gating); the test-executor stays todo until impl completes.
        assert kb.get_task(conn, impl).status == "ready"
        assert kb.get_task(conn, plan_id).status == "ready"
        assert kb.get_task(conn, executor_id).status == "todo"


def test_decompose_rejects_unresolvable_cycle(kanban_home):
    # A pre-existing cycle that auto-repair cannot break must reject the
    # whole decompose — no orphan children, no partial writes.
    with kb.connect() as conn:
        root = _create_triage(conn)
        x = kb.create_task(conn, title="pre-existing X", parents=[root], triage=False)
        y = kb.create_task(conn, title="pre-existing Y", parents=[root], triage=False)
        # Direct row insert: link_tasks() would refuse (it checks cycles),
        # but the DB itself does not enforce acyclicity. Y -> root means the
        # root waits on Y while Y waits on root — cyclic before decompose.
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (y, root),
        )
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

        with pytest.raises(ValueError, match="cyclic"):
            kb.decompose_triage_task(
                conn,
                root,
                root_assignee="orchestrator",
                children=[{"title": "A", "parents": [x]}],
            )

        # Nothing was committed: same task count, same children of root.
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
        assert set(kb.child_ids(conn, root)) == {x, y}
