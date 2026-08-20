"""Follow-up priority tests: repair cards must execute NEXT.

A review CONCERNS/FAIL verdict produces follow-up work that must run before
any other ready card — the process is not complete until QA/Review passes.
The contracts pinned here:

* A follow-up child (priority >= FOLLOWUP_PRIORITY) of a ``review`` parent
  promotes to ``ready`` (``recompute_ready``) and is claimable
  (``claim_task``) even though its parent is not done — the scoped exemption
  to the parent-completion invariant (RCA task t_a6acd07d).
* A normal child of a running/todo parent is still demoted (invariant kept).
* ``request_changes`` bumps the reopened card's priority so rework executes
  next; an explicit priority override is honored and never lowers.
* Completing the follow-up does NOT auto-approve the parent.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _row(conn, tid):
    return conn.execute(
        "SELECT status, priority, block_kind, block_recurrences, current_run_id "
        "FROM tasks WHERE id = ?",
        (tid,),
    ).fetchone()


def _events(conn, tid, kind=None):
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    out = [
        (r["kind"], json.loads(r["payload"]) if r["payload"] else None)
        for r in rows
    ]
    if kind is not None:
        out = [e for e in out if e[0] == kind]
    return out


def _send_to_review(conn, tid: str) -> None:
    """Move a claimed implementer task into ``review`` (reviewer reassigned)."""
    run_id = kb.get_task(conn, tid).current_run_id
    assert run_id is not None
    ok = kb.request_review(
        conn, tid, summary="v1", reviewer="reviewer", expected_run_id=run_id,
    )
    assert ok is True
    assert kb.get_task(conn, tid).status == "review"


# ---------------------------------------------------------------------------
# Deadlock resolution: follow-up child of a review parent promotes + claims
# ---------------------------------------------------------------------------


def test_followup_child_of_review_parent_promotes_and_claims(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="impl a feature", assignee="worker")
        kb.claim_task(conn, parent)
        _send_to_review(conn, parent)

        followup = kb.create_task(
            conn,
            title="fix claim X",
            assignee="worker",
            priority=kb.FOLLOWUP_PRIORITY,
            parents=(parent,),
        )
        # Child of an open parent starts in todo.
        assert kb.get_task(conn, followup).status == "todo"

        # recompute_ready promotes the follow-up despite the review parent.
        assert kb.recompute_ready(conn) == 1
        assert kb.get_task(conn, followup).status == "ready"

        # The claim gate exempts the follow-up: no demotion, no rejection.
        claimed = kb.claim_task(conn, followup)
        assert claimed is not None
        assert claimed.id == followup
        assert kb.get_task(conn, parent).status == "review"
        assert _events(conn, followup, kind="claim_rejected") == []


# ---------------------------------------------------------------------------
# Invariant preserved: non-follow-up children are still demoted
# ---------------------------------------------------------------------------


def test_non_followup_child_of_running_parent_still_demoted(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        kb.claim_task(conn, parent)  # running
        child = kb.create_task(
            conn, title="child", assignee="worker", parents=(parent,),
        )
        assert kb.get_task(conn, child).status == "todo"
        # Racy writer (RCA t_a6acd07d): child reaches ready while parent undone.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = ?",
                (child,),
            )
        assert kb.claim_task(conn, child) is None
        assert kb.get_task(conn, child).status == "todo"
        assert _events(conn, child, kind="claim_rejected")


# ---------------------------------------------------------------------------
# Ordering: follow-up claims before normal ready work in the same tick
# ---------------------------------------------------------------------------


def test_followup_claimed_before_normal_ready_in_same_tick(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    # Keep the review lane off so the parent stays parked in review and the
    # ready lane ordering is the only dispatch happening this tick.
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": False}},
    )

    with kb.connect() as conn:
        parent = kb.create_task(conn, title="review me", assignee="worker")
        kb.claim_task(conn, parent)
        _send_to_review(conn, parent)

        followup = kb.create_task(
            conn, title="[FOLLOW-UP] fix", assignee="worker",
            priority=kb.FOLLOWUP_PRIORITY, parents=(parent,),
        )
        normal = kb.create_task(conn, title="normal", assignee="worker")

        res = kb.dispatch_once(
            conn, spawn_fn=lambda *a, **k: 4242, dry_run=False,
            max_spawn=1,
        )
        spawned = [s[0] for s in res.spawned]
        assert spawned and spawned[0] == followup
        assert kb.get_task(conn, followup).status == "running"
        assert kb.get_task(conn, normal).status == "ready"
        assert kb.get_task(conn, parent).status == "review"


# ---------------------------------------------------------------------------
# request_changes: reopened card re-queues at the front
# ---------------------------------------------------------------------------


def test_request_changes_bumps_priority_and_lands_ready(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="worker")
        kb.claim_task(conn, tid)
        _send_to_review(conn, tid)

        claimed_review = kb.claim_review_task(conn, tid)
        assert claimed_review is not None
        ok, detail = kb.request_changes(
            conn, tid, reason="fix the claim X",
            expected_run_id=claimed_review.current_run_id,
        )
        assert ok is True
        assert detail == "worker"

        row = _row(conn, tid)
        assert row["status"] == "ready"
        assert int(row["priority"]) >= kb.FOLLOWUP_PRIORITY
        changes = _events(conn, tid, kind="changes_requested")
        assert changes and changes[-1][1]["priority"] == kb.FOLLOWUP_PRIORITY


def test_request_changes_explicit_priority_override(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="implement", assignee="worker")
        kb.claim_task(conn, tid)
        _send_to_review(conn, tid)
        claimed_review = kb.claim_review_task(conn, tid)
        assert claimed_review is not None

        # priority=0 keeps the current priority (no bump).
        ok, _ = kb.request_changes(
            conn, tid, reason="keep", priority=0,
            expected_run_id=claimed_review.current_run_id,
        )
        assert ok is True
        assert int(_row(conn, tid)["priority"]) == 0

        # A fresh review cycle with an explicit positive override.
        kb.claim_task(conn, tid)
        _send_to_review(conn, tid)
        claimed_review2 = kb.claim_review_task(conn, tid)
        assert claimed_review2 is not None
        ok, _ = kb.request_changes(
            conn, tid, reason="bump to five", priority=5,
            expected_run_id=claimed_review2.current_run_id,
        )
        assert ok is True
        assert int(_row(conn, tid)["priority"]) == 5


# ---------------------------------------------------------------------------
# Parent stays open until approved; follow-up completion is not approval
# ---------------------------------------------------------------------------


def test_completing_followup_does_not_auto_approve_parent(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="impl", assignee="worker")
        kb.claim_task(conn, parent)
        _send_to_review(conn, parent)
        followup = kb.create_task(
            conn, title="fix", assignee="worker",
            priority=kb.FOLLOWUP_PRIORITY, parents=(parent,),
        )
        kb.recompute_ready(conn)
        assert kb.claim_task(conn, followup) is not None
        assert kb.complete_task(conn, followup, result="fixed") is True
        assert kb.get_task(conn, followup).status == "done"
        assert kb.get_task(conn, parent).status == "review"


def test_followup_full_loop_implement_review_concerns_approve(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="impl a feature", assignee="worker")
        kb.claim_task(conn, parent)
        _send_to_review(conn, parent)  # review verdict: CONCERNS pending

        followup = kb.create_task(
            conn, title="[FOLLOW-UP] address concerns", assignee="worker",
            priority=kb.FOLLOWUP_PRIORITY, parents=(parent,),
        )
        # Follow-up executes NEXT while the parent stays in review.
        assert kb.recompute_ready(conn) == 1
        assert kb.claim_task(conn, followup) is not None
        assert kb.get_task(conn, parent).status == "review"
        assert kb.complete_task(conn, followup, result="concerns addressed") is True
        assert kb.get_task(conn, followup).status == "done"

        # Re-review the parent and approve.
        reviewed = kb.claim_review_task(conn, parent)
        assert reviewed is not None
        assert kb.complete_task(conn, parent, summary="approved") is True
        assert kb.get_task(conn, parent).status == "done"
        assert _events(conn, parent, kind="block_loop_detected") == []
        assert _events(conn, followup, kind="block_loop_detected") == []


# ---------------------------------------------------------------------------
# CLI surface: --followup and request-changes --priority
# ---------------------------------------------------------------------------


def test_cli_create_followup_prefixes_title_and_sets_priority(
    kanban_home: Path,
) -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args(
        ["kanban", "create", "fix claim X", "--followup", "--assignee", "worker"],
    )
    assert kc.kanban_command(args) == 0

    with kb.connect() as conn:
        row = conn.execute(
            "SELECT title, priority FROM tasks ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["title"] == "[FOLLOW-UP] fix claim X"
        assert int(row["priority"]) == kb.FOLLOWUP_PRIORITY


def test_cli_request_changes_priority_flag(kanban_home: Path) -> None:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="implement", assignee="worker")
        kb.claim_task(conn, tid)
        _send_to_review(conn, tid)
        claimed_review = kb.claim_review_task(conn, tid)
        assert claimed_review is not None

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args(
        ["kanban", "request-changes", tid, "reason", "--priority", "5"],
    )
    assert kc.kanban_command(args) == 0

    with kb.connect() as conn:
        assert int(_row(conn, tid)["priority"]) == 5
