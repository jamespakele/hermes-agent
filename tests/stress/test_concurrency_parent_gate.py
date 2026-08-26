"""Stress test for parent-completion invariant at the claim gate.

Simulates the create-then-link race described in RCA t_a6acd07d:

  Thread A: repeatedly inserts a child row with status='ready' (racy
            writer) and a split-second-later inserts the parent link,
            emulating the pre-fix _kanban_create path.
  Thread B: repeatedly runs claim_task against every ready task.

Pass criteria: no task is ever 'claimed' while any of its parents is
not 'done'. The claim_task gate added in hermes_cli/kanban_db.py must
demote such tasks back to 'todo' and emit a 'claim_rejected' event
instead of spawning.

Run as a script (`python tests/stress/test_concurrency_parent_gate.py`)
or via `pytest --run-stress`. The default pytest collection in
tests/stress/conftest.py ignores *.py globs, so this is a script.
"""
from __future__ import annotations

import os
import random
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

WT = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, WT)

NUM_CREATE_ROUNDS = 200
WORKERS_RUN_DURATION_S = 8


def run() -> int:
    home = tempfile.mkdtemp(prefix="hermes_parent_gate_stress_")
    os.environ["HERMES_HOME"] = home
    os.environ["HOME"] = home
    # HARD ISOLATION: pin the kanban root to the temp dir AND assert it took.
    # kanban_db_path() honors HERMES_KANBAN_HOME first, then
    # get_default_hermes_root(), which is MEMOIZED and can resolve to the LIVE
    # ~/.hermes even when HERMES_HOME points at a temp dir — if the memo was
    # populated before this env change, this stress test silently writes its
    # parent-N / child fixtures onto the real board. Pinning HERMES_KANBAN_HOME
    # (and asserting the resolved path) makes the leak impossible.
    os.environ["HERMES_KANBAN_HOME"] = home

    from hermes_cli import kanban_db as kb

    # Refuse to run unless the DB provably resolves inside the temp dir.
    _resolved = str(kb.kanban_db_path()).rstrip("/")
    if not _resolved.startswith(home):
        raise SystemExit(
            "STRESS REFUSED TO RUN: kanban DB resolves outside the temp home "
            f"({_resolved!r}); expected under {home!r}. The parent/child "
            "fixtures would have polluted the live board. Fix the isolation pin."
        )

    try:
        kb.init_db()
        _body(kb)
    finally:
        # Clean up after itself: never leave fixtures behind, isolated or not.
        shutil.rmtree(home, ignore_errors=True)
    # _body sets the exit status; run() maps it.
    return _EXIT


_EXIT = 0


def _body(kb) -> None:
    global _EXIT
    # Seed N parents in 'ready' state. They stay ready for the whole run
    # (never 'done'), so every child linked to one of them must remain
    # unclaimable.
    parent_ids: list[str] = []
    conn = kb.connect()
    try:
        for i in range(10):
            parent_ids.append(
                kb.create_task(conn, title=f"parent-{i}", assignee="a")
            )
    finally:
        conn.close()

    created_children: list[str] = []
    created_lock = threading.Lock()
    stop = threading.Event()
    violations: list[str] = []

    def racy_creator() -> None:
        """Inserts child rows with status='ready' and links them after.

        This is the pre-fix _kanban_create behavior — the very race
        the gate in claim_task must catch.
        """
        conn = kb.connect()
        try:
            for _ in range(NUM_CREATE_ROUNDS):
                if stop.is_set():
                    return
                parents = random.sample(parent_ids, k=2)
                # Step 1: insert child WITHOUT parents (ends up ready).
                child = kb.create_task(
                    conn, title="child", assignee="a", parents=[],
                )
                # Tiny delay so worker threads get a chance to see the
                # ready row before the links are inserted.
                time.sleep(random.uniform(0.0001, 0.002))
                # Step 2: add the parent links after the fact.
                for p in parents:
                    try:
                        kb.link_tasks(conn, parent_id=p, child_id=child)
                    except Exception:
                        pass
                with created_lock:
                    created_children.append(child)
        finally:
            conn.close()

    def worker_loop() -> None:
        conn = kb.connect()
        try:
            end = time.monotonic() + WORKERS_RUN_DURATION_S
            while time.monotonic() < end and not stop.is_set():
                row = conn.execute(
                    "SELECT id FROM tasks WHERE status='ready' "
                    "AND claim_lock IS NULL ORDER BY RANDOM() LIMIT 1"
                ).fetchone()
                if row is None:
                    time.sleep(0.002)
                    continue
                tid = row["id"]
                try:
                    claimed = kb.claim_task(conn, tid, claimer="w")
                except Exception:
                    continue
                if claimed is None:
                    continue
                # Invariant: a successful claim on `tid` must mean all
                # parents are 'done'. Check in the same connection txn
                # so we see the post-claim state.
                undone = conn.execute(
                    "SELECT l.parent_id, p.status FROM task_links l "
                    "JOIN tasks p ON p.id = l.parent_id "
                    "WHERE l.child_id = ? AND p.status != 'done'",
                    (tid,),
                ).fetchall()
                if undone:
                    violations.append(
                        f"claimed {tid} while parents not done: "
                        + ",".join(f"{r['parent_id']}={r['status']}" for r in undone)
                    )
                # Release so the run doesn't leak and the next round sees ready.
                kb.complete_task(conn, tid, result="stress-ok")
        finally:
            conn.close()

    creator = threading.Thread(target=racy_creator, daemon=True)
    workers = [threading.Thread(target=worker_loop, daemon=True)
               for _ in range(4)]
    creator.start()
    for w in workers:
        w.start()
    creator.join()
    # Give the workers a chance to fully drain ready rows before we stop.
    time.sleep(0.5)
    stop.set()
    for w in workers:
        w.join(timeout=WORKERS_RUN_DURATION_S + 2)

    # Post-run audit: the DB event log must show no 'claimed' event on any
    # task whose parents were not 'done' at the time of the claim.
    conn = kb.connect()
    try:
        bad = conn.execute(
            """
            WITH claims AS (
              SELECT task_id, created_at AS t
              FROM task_events WHERE kind='claimed'
            )
            SELECT c.task_id, l.parent_id, p.status, p.completed_at
            FROM claims c
            JOIN task_links l ON l.child_id = c.task_id
            JOIN tasks p ON p.id = l.parent_id
            WHERE p.completed_at IS NULL OR p.completed_at > c.t
            """
        ).fetchall()
        rejections = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='claim_rejected'"
        ).fetchone()[0]
    finally:
        conn.close()

    print(f"children created:  {len(created_children)}")
    print(f"violations:        {len(violations)}")
    print(f"event-log bad:     {len(bad)}")
    print(f"claim_rejected:    {rejections}")

    global _EXIT
    if violations or bad:
        for v in violations[:10]:
            print("  VIOLATION:", v)
        for row in list(bad)[:10]:
            print("  EVENT-LOG BAD:", dict(row))
        _EXIT = 1
        return
    print("PARENT-GATE INVARIANT HELD UNDER RACE")


if __name__ == "__main__":
    sys.exit(run())
