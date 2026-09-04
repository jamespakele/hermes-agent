"""Tests for kb.decompose_triage_task — the DB-layer atomic fan-out
from the triage column. LLM-free by design.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _make_git_repo(path: Path, remote: Optional[str] = None) -> Path:
    """Create an offline tmp git repo with an initial commit (and optional
    origin remote). No network, no host git config (GIT_CONFIG_GLOBAL/SYSTEM
    pinned to /dev/null so the developer's user config can't leak in)."""
    path.mkdir(parents=True, exist_ok=True)
    env = dict(
        os.environ,
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
    )

    def run(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(path), *args],
            check=True, capture_output=True, env=env,
        )

    run("init", "-q")
    run("config", "user.name", "test")
    run("config", "user.email", "test@test")
    (path / "README.md").write_text("repo\n")
    run("add", "README.md")
    run("commit", "-q", "-m", "init")
    if remote:
        run("remote", "add", "origin", remote)
    return path


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


# ---------------------------------------------------------------------------
# Cross-repo anchor inheritance / detection (t_65ca3bd5 incident class)
# ---------------------------------------------------------------------------


def _create_worktree_triage(conn, repo: Path, *, project_id=None, title="epic"):
    return kb.create_task(
        conn,
        title=title,
        workspace_kind="worktree",
        workspace_path=str(repo),
        project_id=project_id,
        triage=True,
    )


def test_decompose_worktree_children_inherit_root_repo_anchor(kanban_home):
    # Root is a worktree-kind triage task anchored on a real git checkout;
    # children carry no repo evidence -> they must inherit the root's repo
    # (workspace_path + workspace_kind + project_id), never the board
    # default_workdir (the t_65ca3bd5 misroute).
    repo = _make_git_repo(kanban_home.parent / "repos" / "recipe-base",
                          remote="https://github.com/crabby-apps/recipe-base.git")
    with pdb.connect_closing() as pconn:
        pid = pdb.create_project(
            pconn, name="recipe-base", folders=[str(repo)], primary_path=str(repo),
        )
    with kb.connect() as conn:
        root = _create_worktree_triage(conn, repo, project_id=pid)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "regression guard", "body": "add a test", "assignee": "researcher"},
                {"title": "impl", "body": "write the code", "assignee": "engineer"},
            ],
            author="decomposer",
        )
    assert child_ids is not None and len(child_ids) == 2
    with kb.connect() as conn:
        c0 = kb.get_task(conn, child_ids[0])
        c1 = kb.get_task(conn, child_ids[1])
    for c in (c0, c1):
        assert c.workspace_kind == "worktree"
        assert c.workspace_path == str(repo)
        assert c.project_id == pid


def test_decompose_reanchors_child_with_alternate_repo_evidence(kanban_home):
    # repoB is a depth-1 sibling of the root's repo with remote
    # jamespakele/iq-kip-v2.git. A child that positively locates that repo
    # re-anchors to repoB; its sibling stays on the root repo.
    parent = kanban_home.parent / "repos"
    repo_a = _make_git_repo(parent / "recipe-base",
                            remote="https://github.com/crabby-apps/recipe-base.git")
    repo_b = _make_git_repo(parent / "iq-kip-v2",
                            remote="git@github.com:jamespakele/iq-kip-v2.git")
    with kb.connect() as conn:
        root = _create_worktree_triage(conn, repo_a)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "port guard", "body": "the code lives in jamespakele/iq-kip-v2 — port the sanitizer"},
                {"title": "other", "body": "do the rest"},
            ],
            author="decomposer",
        )
    assert child_ids is not None and len(child_ids) == 2
    with kb.connect() as conn:
        c0 = kb.get_task(conn, child_ids[0])
        c1 = kb.get_task(conn, child_ids[1])
    assert c0.workspace_path == str(repo_b)
    assert c1.workspace_path == str(repo_a)


def test_decompose_rejects_conflicting_repo_evidence(kanban_home):
    # A child naming TWO distinct resolvable non-root repos in an anchoring
    # context must reject the whole decompose: ValueError, zero rows written,
    # root still triage (no orphan children).
    parent = kanban_home.parent / "repos"
    repo_a = _make_git_repo(parent / "recipe-base",
                            remote="https://github.com/crabby-apps/recipe-base.git")
    _make_git_repo(parent / "iq-kip-v2",
                   remote="git@github.com:jamespakele/iq-kip-v2.git")
    _make_git_repo(parent / "analytics",
                   remote="https://github.com/crabby-apps/analytics.git")
    with kb.connect() as conn:
        root = _create_worktree_triage(conn, repo_a)
        x = kb.create_task(conn, title="pre-existing X", parents=[root], triage=False)
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

        with pytest.raises(ValueError, match="multiple distinct"):
            kb.decompose_triage_task(
                conn,
                root,
                root_assignee="orchestrator",
                children=[{
                    "title": "ambiguous",
                    "body": "code lives in jamespakele/iq-kip-v2 and the checkout of crabby-apps/analytics",
                }],
            )

        # Atomic rollback: no orphan children, root still triage.
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
        assert set(kb.child_ids(conn, root)) == {x}
        assert kb.get_task(conn, root).status == "triage"


def test_decompose_scratch_root_keeps_legacy_null_workspace(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="scratch epic", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "A", "body": "plain work"}],
        )
    assert child_ids is not None
    with kb.connect() as conn:
        c = kb.get_task(conn, child_ids[0])
    # Scratch (non-worktree) roots keep the legacy NULL path — the board
    # default behavior, untouched by the worktree anchor fix.
    assert c.workspace_kind == "scratch"
    assert c.workspace_path is None


def test_decompose_explicit_child_workspace_overrides_detection(kanban_home):
    # An explicit per-child workspace_path wins verbatim and is never
    # second-guessed — a negated (or any) repo mention in its body cannot
    # re-anchor it or fail the decompose.
    parent = kanban_home.parent / "repos"
    repo_a = _make_git_repo(parent / "recipe-base",
                            remote="https://github.com/crabby-apps/recipe-base.git")
    _make_git_repo(parent / "iq-kip-v2",
                   remote="git@github.com:jamespakele/iq-kip-v2.git")
    explicit = parent / "pinned-checkout"
    explicit.mkdir(parents=True)
    with kb.connect() as conn:
        root = _create_worktree_triage(conn, repo_a)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{
                "title": "pinned",
                "body": "this is NOT jamespakele/iq-kip-v2 — keep it here",
                "workspace_path": str(explicit),
            }],
        )
    assert child_ids is not None
    with kb.connect() as conn:
        c = kb.get_task(conn, child_ids[0])
    assert c.workspace_path == str(explicit)
    assert c.workspace_kind == "worktree"


def test_decompose_negated_repo_mention_is_not_evidence(kanban_home):
    # The incident's contrast shape: a card saying "this is NOT <repo>"
    # (unresolvable here) must not re-anchor and must not fail loudly.
    repo = _make_git_repo(kanban_home.parent / "repos" / "recipe-base",
                          remote="https://github.com/crabby-apps/recipe-base.git")
    with kb.connect() as conn:
        root = _create_worktree_triage(conn, repo)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{
                "title": "guard",
                "body": "this is NOT jamespakele/iq-kip-v2 — we own the guard",
            }],
        )
    assert child_ids is not None
    with kb.connect() as conn:
        c = kb.get_task(conn, child_ids[0])
    assert c.workspace_path == str(repo)


def test_decompose_unresolvable_repo_mention_fails_loudly(kanban_home):
    # A positively asserted owner/name repo with no local match fails the
    # whole decompose (the incident-inverse policy): ValueError naming the
    # token, zero children persisted, root still triage.
    repo = _make_git_repo(kanban_home.parent / "repos" / "recipe-base",
                          remote="https://github.com/crabby-apps/recipe-base.git")
    with kb.connect() as conn:
        root = _create_worktree_triage(conn, repo)
        x = kb.create_task(conn, title="pre-existing X", parents=[root], triage=False)
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

        with pytest.raises(ValueError, match="jamespakele/iq-kip-v2"):
            kb.decompose_triage_task(
                conn,
                root,
                root_assignee="orchestrator",
                children=[{
                    "title": "guard",
                    "body": "the code lives in jamespakele/iq-kip-v2 — extend it",
                }],
            )

        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
        assert set(kb.child_ids(conn, root)) == {x}
        assert kb.get_task(conn, root).status == "triage"


def test_decompose_prose_noise_tokens_do_not_fail(kanban_home):
    # Issue refs (epic/61), task refs (wt/t_<id>), multi-segment paths
    # (server/api/src/...) and GitHub issue references (owner/name#digits)
    # are prose noise — they must not brick decompose. The last one is the
    # sharp edge: an unresolvable repo token would otherwise trip the
    # fail-loud arm.
    repo = _make_git_repo(kanban_home.parent / "repos" / "recipe-base",
                          remote="https://github.com/crabby-apps/recipe-base.git")
    with kb.connect() as conn:
        root = _create_worktree_triage(conn, repo)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{
                "title": "impl",
                "body": "see epic/61 and wt/t_65ca3bd5 plus "
                        "server/api/src/middleware/auth.rs and "
                        "jamespakele/iq-kip-v2#42 — implement",
            }],
        )
    assert child_ids is not None
    with kb.connect() as conn:
        c = kb.get_task(conn, child_ids[0])
    assert c.workspace_path == str(repo)


def test_decompose_bare_cross_repo_mention_inherits_root(kanban_home):
    # A bare cross-repo safety assertion ("verify <repo> is unaffected")
    # with a resolvable sibling and NO workspace-locator signal is not
    # anchoring evidence: the child inherits the root anchor (the
    # false-misroute guard — the original bug recreated in reverse).
    parent = kanban_home.parent / "repos"
    repo_a = _make_git_repo(parent / "recipe-base",
                            remote="https://github.com/crabby-apps/recipe-base.git")
    repo_b = _make_git_repo(parent / "iq-kip-v2",
                            remote="git@github.com:jamespakele/iq-kip-v2.git")
    with kb.connect() as conn:
        root = _create_worktree_triage(conn, repo_a)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{
                "title": "check",
                "body": "verify jamespakele/iq-kip-v2 is unaffected by this change",
            }],
        )
    assert child_ids is not None
    with kb.connect() as conn:
        c = kb.get_task(conn, child_ids[0])
    assert c.workspace_path == str(repo_a)
    assert c.workspace_path != str(repo_b)
