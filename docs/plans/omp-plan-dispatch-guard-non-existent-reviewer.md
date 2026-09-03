# Working Plan: Dispatch-Time Guard for Non-Existent Reviewer Profile in Kanban Dispatcher

## Objective

Eliminate the silent stall in the kanban dispatcher when a card is assigned to
a reviewer/assignee profile that does not exist under `~/.hermes/profiles/`.
Currently both the ready-loop and review-loop inside `_dispatch_once_locked`
silently append such cards to `skipped_nonspawnable` (documented as "NOT an
operator-actionable failure") and `continue` — making operator mis-assignments
indistinguishable from expected control-plane lanes (e.g. `orion-cc` terminals
that pull via `claim_task`).

The fix must:
1. **Log a loud warning** (`logger.error`) with the card ID and missing profile name.
2. **Post a comment on the card** explaining the mis-assignment and that it will not auto-dispatch.
3. **Block the card** (`block_task` with `kind="needs_input"`) so a human can intervene.
4. **Distinguish** genuine operator errors from expected control-plane lanes via a quiet-lane allowlist.
5. **Preserve existing behavior** for known quiet lanes (`orion-cc`, `orion-research`, `ai-code-review`, `ai-knowledge-review`) and valid profiles — they continue to dispatch/skip as before.

## Constraints

- **Language**: Python 3.11+ (type hints, `list[tuple[str, str]]` syntax).
- **Framework**: Hermes internal kanban engine (`hermes_cli/kanban_db.py`), SQLite WAL.
- **Dependencies**: `hermes_cli/profiles.py` (`profile_exists`, `normalize_profile_name`); no new external dependencies.
- **Block kind must be in `VALID_BLOCK_KINDS`** = `{"dependency", "needs_input", "capability", "transient"}` (line 125). Use `kind="needs_input"` — semantically correct (human intervention required). Do NOT use `"dispatch_error"` (not in the set, would raise `ValueError`).
- **`block_task` has FIVE `status IN ('running', 'ready')` clauses** (lines 6478, 6536, 6575, 6590, 6768). Only the first FOUR belong to `block_task` and must be widened. The fifth at line 6768 belongs to `request_review` (running/ready → review) and MUST NOT be touched.
- **`source_status` derivation** in `block_task` (lines 6451–6454) currently maps any non-`running` status to `"ready"`. Must add a `"review"` branch so blocked review cards unblock back to `review`, not `ready`.
- **`profile_exists("default")` always returns `True`** (profiles.py:386) — the default profile is always spawnable. The guard only fires when `profile_exists` returns `False`.
- **Dry-run safety**: `logger.error` fires always (even in dry-run); `add_comment`/`block_task` only when `not dry_run`.
- **Idempotency**: marker-comment check prevents comment spam on repeated ticks.
- **`all_assignees_spawnable` conftest fixture** (conftest.py:7, NOT autouse) monkeypatches `profile_exists → lambda name: True`. New loud-path tests must NOT request this fixture.

## File Structure

### Files to MODIFY

| # | File | Location (verified) | Change |
|---|------|---------------------|--------|
| 1 | `hermes_cli/kanban_db.py` | after `VALID_BLOCK_KINDS` (line 125) | Add `DEFAULT_QUIET_NONSPAWNABLE_LANES` constant: `frozenset({"orion-cc", "orion-research", "ai-code-review", "ai-knowledge-review"})` |
| 2 | `hermes_cli/kanban_db.py` | `_fire_dispatch_tick_hook` idle gate (lines 345–355) | Add `result.skipped_misassigned` to the `any(...)` tuple so a tick that only flagged mis-assignments is not classified `"idle"` |
| 3 | `hermes_cli/kanban_db.py` | `DispatchResult` dataclass (~line 8206) | Add `skipped_misassigned: list[tuple[str, str]] = field(default_factory=list)` field with docstring ("IS operator-actionable — logged loudly, commented, blocked") |
| 4 | `hermes_cli/kanban_db.py` | `block_task` docstring (line 6412) | Update "Transition `running`/`ready` → `blocked`" to "Transition `running`/`ready`/`review` → `blocked` (or route to `todo`/`triage`)" |
| 5 | `hermes_cli/kanban_db.py` | `block_task` `source_status` derivation (lines 6451–6454) | Add `"review"` branch: `else ("review" if cur_row["status"] == "review" else "ready")` |
| 6 | `hermes_cli/kanban_db.py` | `block_task` clause 1 (line 6478, dependency→todo) | `status IN ('running', 'ready')` → `('running', 'ready', 'review')` |
| 7 | `hermes_cli/kanban_db.py` | `block_task` clause 2 (line 6536, triage/recurrence-loop-breaker) | Same widening — **CRITICAL**: without this, a review card re-blocked past `BLOCK_RECURRENCE_LIMIT` never escalates to `triage` |
| 8 | `hermes_cli/kanban_db.py` | `block_task` clause 3 (line 6575, blocked no expected_run_id) | Same widening |
| 9 | `hermes_cli/kanban_db.py` | `block_task` clause 4 (line 6590, blocked with expected_run_id) | Same widening |
| 10 | `hermes_cli/kanban_db.py` | near `dispatch_once` (before line 9977) | Add `_quiet_nonspawnable_lanes() -> frozenset[str]` and `_is_quiet_lane(assignee: str) -> bool` helper functions |
| 11 | `hermes_cli/kanban_db.py` | near `dispatch_once` | Add `_flag_misassigned_card(conn, task_id, assignee, *, lane, dry_run)` helper (idempotent marker-comment check, try/except around add_comment/block_task) |
| 12 | `hermes_cli/kanban_db.py` | ready loop (lines 10345–10351) | Branch: `_is_quiet_lane` → quiet `skipped_nonspawnable` / else loud `skipped_misassigned` + `_flag_misassigned_card(lane="ready")` |
| 13 | `hermes_cli/kanban_db.py` | review loop (lines 10498–10500) | Same pattern, `lane="review"` |
| 14 | `hermes_cli/kanban.py` | JSON output (line 2768) | Add `"skipped_misassigned": [{"task_id": tid, "assignee": who} for tid, who in res.skipped_misassigned]` |
| 15 | `hermes_cli/kanban.py` | human output (after line 2809) | Add print block: `Skipped (MISSING profile — commented + blocked): ...` |
| 16 | `gateway/kanban_watchers.py` | dispatcher summary (lines 1709–1720) | Log `logger.error` per board when `res.skipped_misassigned` non-empty, **even when `any_spawned=False`** |
| 17 | `hermes_cli/plugins.py` | hook payload doc (line 350) | Add `skipped_misassigned` to the `on_kanban_dispatch_tick` payload doc comment |
| 18 | `tests/hermes_cli/test_kanban_review_lifecycle.py` | new tests (end of file) | 8 new tests (see Verification Criteria) |

### Files NOT modified (reference sites)

| File | Location | Reason |
|------|----------|--------|
| `hermes_cli/kanban_db.py` | line 6768 | `request_review` SQL clause `status IN ('running', 'ready')` — MUST NOT touch; widening would let a review card re-request review (self-loop) |
| `tests/hermes_cli/conftest.py` | line 7 | `all_assignees_spawnable` fixture (NOT autouse) — monkeypatches `profile_exists → True`; new loud-path tests must opt out by not requesting it |
| `hermes_cli/plugins.py` | lines 340–359 | `on_kanban_dispatch_tick` hook payload doc — additive update only (line 350), no API break |

## Implementation Notes

### Design Decision: Quiet-Lane Allowlist vs Pattern Matching

A **frozenset allowlist** is chosen over pattern matching (e.g., "anything starting with `orion-`") because:
- Pattern matching risks false positives on future profile names.
- An explicit list is auditable and easy to extend.
- Seeded with `orion-cc`, `orion-research` (control-plane terminals) and `ai-code-review`, `ai-knowledge-review` (skill-name reviewers that are expected to be skipped, not spawned — per existing test `test_review_dispatch_skips_skill_name_assignee` at line 747).

### `_quiet_nonspawnable_lanes()` and `_is_quiet_lane(assignee)`

```python
DEFAULT_QUIET_NONSPAWNABLE_LANES = frozenset({
    "orion-cc",
    "orion-research",
    "ai-code-review",
    "ai-knowledge-review",
})

def _quiet_nonspawnable_lanes() -> frozenset[str]:
    """Return the set of assignee names that are expected non-spawnable
    lanes (control-plane terminals or skill-name reviewers), NOT operator
    errors. Tasks assigned to these lanes are silently skipped."""
    return DEFAULT_QUIET_NONSPAWNABLE_LANES

def _is_quiet_lane(assignee: str) -> bool:
    """True if the assignee is a known quiet lane (control-plane terminal
    or seeded skill-name reviewer), NOT operator error."""
    if not assignee:
        return False
    try:
        from hermes_cli.profiles import normalize_profile_name as _norm
    except Exception:
        _norm = lambda n: (n or "").strip().lower()  # noqa: E731
    return _norm(assignee) in _quiet_nonspawnable_lanes()
```

**Why normalize?** `profile_exists` already normalizes internally (`normalize_profile_name` lowercases, handles `Default` → `default`). If `_is_quiet_lane` doesn't normalize, a case variant like `Orion-CC` would bypass the allowlist and trigger the loud path for a known quiet lane. Normalizing ensures consistency.

### `_flag_misassigned_card(conn, task_id, assignee, *, lane, dry_run)`

Contract:
1. **Always log**: `logger.error("kanban dispatcher: card %s assigned to non-existent profile %r — commented + blocked (lane=%s)", task_id, assignee, lane)` — even in dry-run.
2. **Dry-run guard**: if `dry_run`, return after logging (no DB writes).
3. **Idempotency**: call `list_comments(conn, task_id)`; if any comment body contains the marker string `"auto-dispatch: missing reviewer/assignee profile"`, downgrade to `logger.warning(...)` and skip `add_comment`. Re-blocking is naturally a no-op once blocked (`block_task` SQL won't match `status='blocked'` → returns `False`).
4. **Comment**: `add_comment(conn, task_id, author="kanban-dispatcher", body=<marker + missing profile + lane + "not auto-dispatching; reassign or add to quiet lanes">)` inside `try/except` (comment failure must never crash the tick).
5. **Block**: `block_task(conn, task_id, kind="needs_input", reason=f"missing reviewer/assignee profile {assignee!r}")` inside `try/except`. With all four `block_task` clauses widened (MUST-FIX #1), this matches `status='review'` and `'ready'` and routes to `'blocked'` (not `'todo'`/`'triage'` on first occurrence; `block_recurrences=1 < BLOCK_RECURRENCE_LIMIT=2`).
6. **Does not touch `result`**: the caller appends to `result.skipped_misassigned` before calling this helper.

### Loop Diff (both loops)

```python
# READY loop (line 10345) — current:
if profile_exists is not None and not profile_exists(row_assignee):
    result.skipped_nonspawnable.append(row["id"])
    continue

# READY loop — new:
if profile_exists is not None and not profile_exists(row_assignee):
    if _is_quiet_lane(row_assignee):
        result.skipped_nonspawnable.append(row["id"])
        continue
    # Genuine missing profile — operator error. LOUD path.
    result.skipped_misassigned.append((row["id"], row_assignee))
    _flag_misassigned_card(conn, row["id"], row_assignee,
                           lane="ready", dry_run=dry_run)
    continue

# REVIEW loop (line 10498) — identical, using row["assignee"], lane="review"
```

### `block_task` `source_status` Fix (MUST-FIX #1, part b)

Current (lines 6451–6454):
```python
source_status = (
    _retry_status_for_run(conn, task_id)
    if cur_row["status"] == "running"
    else "ready"
)
```

New:
```python
source_status = (
    _retry_status_for_run(conn, task_id)
    if cur_row["status"] == "running"
    else ("review" if cur_row["status"] == "review" else "ready")
)
```

**Why?** When a review card is blocked, the block event writes `source_status` into the event payload. When `unblock_task` later calls `_resume_status_from_events` (line 4489), it checks `payload.get("source_status") == "review"` to decide whether to return the card to `review` or `ready`. Without this fix, a blocked review card would unblock to `ready` instead of `review`, silently converting a reviewer task into an implementation task.

### `block_task` Four-Clause Widening (MUST-FIX #1, part a)

**All four** `status IN ('running', 'ready')` → `('running', 'ready', 'review')`:

| # | Line | Branch | Target status | Why needed |
|---|------|--------|--------------|------------|
| 1 | 6478 | `kind == "dependency"` | `'todo'` | A review-card dependency block can route to `todo` and let `recompute_ready` promote it |
| 2 | 6536 | `recurrences >= BLOCK_RECURRENCE_LIMIT` | `'triage'` | **CRITICAL**: without this, a mis-assigned review card unblocked-and-re-blocked past the limit never escalates to triage (UPDATE returns rowcount 0 → `block_task` returns `False`) |
| 3 | 6575 | blocked (no `expected_run_id`) | `'blocked'` | Review cards can be blocked for human intervention |
| 4 | 6590 | blocked (with `expected_run_id`) | `'blocked'` | Same, with run guard |

**Do NOT touch line 6768** (`request_review` SQL). Widening it would let a review card re-request review (no-op self-loop) and corrupt the review lifecycle.

### `_fire_dispatch_tick_hook` Idle Gate (line 345–355)

Add `result.skipped_misassigned` to the `any(...)` tuple at line 354 (currently ends with `result.skipped_nonspawnable,`). Without this, a tick that only flagged mis-assignments (no spawned/reclaimed/etc.) would be classified as `"idle"`, hiding the event from hook subscribers.

### Gateway Watcher Logging (lines 1709–1720)

Currently only logs when `res.spawned` is non-empty. Add:
```python
if res is not None and getattr(res, "skipped_misassigned", None):
    logger.error(
        "kanban dispatcher [%s]: %d task(s) BLOCKED due to non-existent "
        "profile assignment: %s",
        slug, len(res.skipped_misassigned), res.skipped_misassigned,
    )
```
This must fire **even when `any_spawned=False`** — the current code only enters the logging block when `res.spawned` is truthy, so mis-assignments on an otherwise-idle board would be silently dropped.

### Edge Cases

1. **Empty assignee**: handled before the `profile_exists` check by `skipped_unassigned` — no change needed.
2. **`default` profile**: `profile_exists("default")` returns `True` unconditionally — the guard never fires.
3. **Profile exists but is a skill name** (`ai-knowledge-review`): in the quiet lane allowlist → silently skipped. Existing test `test_review_dispatch_skips_skill_name_assignee` stays green.
4. **Already-blocked card on re-tick**: `block_task` returns `False` (status not in `('running','ready','review')`), idempotency check skips the comment. Downgraded to `logger.warning`.
5. **Comment/block failure**: wrapped in `try/except`, logged as `logger.warning`, never crashes the dispatch tick.
6. **`profile_exists` import fails** (partial install): `profile_exists` is `None`, the `if profile_exists is not None` guard is skipped, and the card proceeds to spawn. No change to this path.
7. **Config override**: `_quiet_nonspawnable_lanes()` returns the module-level constant. Future enhancement: read from `kanban.quiet_nonspawnable_lanes` config key. For now, the constant is sufficient and documented.

## Verification Criteria

All tests are in `tests/hermes_cli/test_kanban_review_lifecycle.py`. New tests must NOT request the `all_assignees_spawnable` fixture (it forces `profile_exists → True`, bypassing the guard). Tests that need real `profile_exists` behavior create actual profile directories under the `kanban_home` fixture's temp profiles root.

### Test 1: `test_review_misassigned_profile_is_loud`
- **Setup**: review card with assignee `nonexistent-reviewer` (no profile dir), `dry_run=True`.
- **Assert**: `(tid, "nonexistent-reviewer") in res.skipped_misassigned` AND `tid not in res.skipped_nonspawnable`.
- **Covers**: AC#1 (non-existent reviewer does not silently stall).

### Test 2: `test_review_misassigned_posts_comment_and_blocks`
- **Setup**: review card with assignee `ghost-reviewer` (no profile dir), `dry_run=False`.
- **Assert**: marker comment present in `list_comments(conn, tid)`, `task.status in ("blocked", "review")`, `res.skipped_misassigned` non-empty.
- **Covers**: AC#2 (clear warning/block visible on card).

### Test 3: `test_review_misassigned_logs_error`
- **Setup**: review card with assignee `missing-profile`, `caplog.at_level(ERROR)`.
- **Assert**: a log record mentions both the tid and the missing profile name.
- **Covers**: AC#2 (clear warning in logs).

### Test 4: `test_misassigned_idempotent_no_comment_spam`
- **Setup**: two non-dry-run dispatch ticks on the same mis-assigned card.
- **Assert**: exactly one marker comment on the card.
- **Covers**: idempotency.

### Test 5 (regression): `test_review_dispatch_skips_skill_name_assignee` (line 747)
- **Existing test, no change needed**.
- **Assert**: `ai-knowledge-review` → `skipped_nonspawnable`, NOT `skipped_misassigned`.
- **Covers**: AC#3 (valid assignments unaffected) + quiet lane behavior.

### Test 6: `test_review_valid_profile_dispatches_normally`
- **Setup**: create `kanban_home/"profiles"/"test-reviewer"` dir; review card with assignee `test-reviewer`, `dry_run=True`.
- **Assert**: `tid in [s[0] for s in res.spawned]`.
- **Covers**: AC#3 (valid assignments dispatch normally).

### Test 7: `test_ready_control_plane_lane_stays_quiet`
- **Setup**: ready card with assignee `orion-cc`, `dry_run=True`.
- **Assert**: `tid in res.skipped_nonspawnable`, `tid not in [m[0] for m in res.skipped_misassigned]`.
- **Covers**: control-plane lane stays quiet.

### Test 8: `test_misassigned_review_unblocks_back_to_review`
- **Setup**: block a review card (via the loud path), then call `unblock_task`.
- **Assert**: status returns to `review` (not `ready`).
- **Extended**: re-block `BLOCK_RECURRENCE_LIMIT` times; assert card reaches `triage` (validates clause #2 at line 6536).
- **Covers**: MUST-FIX #1 — `source_status` round-trip + triage escalation.

### Runnable command
```bash
cd /home/pakele/.hermes/hermes-agent && python -m pytest tests/hermes_cli/test_kanban_review_lifecycle.py -v
```

## Logical Consequences

| # | Site | Decision | Rationale | Horizon |
|---|------|----------|-----------|---------|
| 1 | `hermes_cli/kanban_db.py:6768` (`request_review` SQL) | **KEEP** (no change) | This `status IN ('running','ready')` clause belongs to `request_review` (running/ready → review). Widening it to include `'review'` would let a review card re-request review (no-op self-loop) and corrupt the review lifecycle. Explicitly out of scope. | Immediate |
| 2 | `tests/hermes_cli/conftest.py:7` (`all_assignees_spawnable` fixture) | **KEEP** (no change to fixture; new tests opt out) | The fixture monkeypatches `profile_exists → True`, so tests using it cannot exercise the new loud path. It is NOT `autouse`, so new tests simply don't request it. Existing tests that use it (asserting spawn behavior with synthetic assignees) continue to work unchanged. | Immediate |
| 3 | `hermes_cli/plugins.py:340–359` (`on_kanban_dispatch_tick` hook payload doc) | **MODIFY** (additive — add `skipped_misassigned` to doc comment at line 350) | Plugin authors need to know the new field exists in the `DispatchResult` payload. Additive only — no API break, no existing plugin breaks. The `_fire_dispatch_tick_hook` idle gate (line 345–355) is also updated so a tick that only flagged mis-assignments fires with `outcome != "idle"`. | Immediate |
| 4 | `hermes_cli/kanban_db.py:345–355` (`_fire_dispatch_tick_hook` idle gate) | **MODIFY** (add `result.skipped_misassigned` to `any(...)` tuple) | Without this, a tick that only flagged mis-assignments (no spawns) would be classified `"idle"`, hiding the event from hook subscribers. Hook subscribers that react to non-idle ticks will now see mis-assignment ticks. | Immediate |
| 5 | `hermes_cli/kanban_db.py:6451–6454` (`source_status` derivation in `block_task`) | **MODIFY** (add `"review"` branch) | When a review card is blocked, the block event writes `source_status`. Without the `"review"` branch, a blocked review card would unblock to `ready` instead of `review`, silently converting a reviewer task into an implementation task. `_resume_status_from_events` (line 4489) already checks `source_status == "review"` — no change needed there. | Immediate |
| 6 | `gateway/kanban_watchers.py:1709–1720` (dispatcher summary) | **MODIFY** (add `logger.error` for `skipped_misassigned`) | Currently only logs when `res.spawned` is non-empty. Mis-assignments on an otherwise-idle board would be silently dropped. The error log must fire even when `any_spawned=False`. Operators monitoring gateway logs will now see mis-assignment warnings. | Immediate |
| 7 | `hermes_cli/kanban.py:2768` (CLI JSON) and `:2806` (CLI human) | **MODIFY** (add `skipped_misassigned` output) | CLI users running `hermes kanban dispatch --dry-run` or `--json` will now see mis-assigned cards in the output. The JSON output adds a structured `skipped_misassigned` array; the human output adds a clearly labeled print block. | Immediate |
| 8 | Existing test `test_review_dispatch_skips_skill_name_assignee` (line 747) | **KEEP** (no change — regression guard) | `ai-knowledge-review` is in the quiet lane allowlist, so it continues to go to `skipped_nonspawnable`. The test stays green and serves as a regression guard against accidentally treating skill-name reviewers as operator errors. | Immediate |
| 9 | `has_spawnable_ready` / `has_spawnable_review` (lines 9709/9741) | **KEEP** (no change) | These functions already use `profile_exists` to filter spawnable work. They do not reference `skipped_nonspawnable` or `skipped_misassigned` — they answer a different question ("is there any spawnable work?"). No change needed. | Next sprint |
| 10 | `skipped_nonspawnable` docstring (line 8206) | **MODIFY** (add cross-ref to `skipped_misassigned`) | The existing docstring says "NOT an operator-actionable failure" — this remains true for `skipped_nonspawnable` (quiet lanes). Add a cross-reference: "For operator-actionable mis-assignments, see `skipped_misassigned`." Prevents future developers from conflating the two buckets. | Immediate |
| 11 | Config extensibility (`kanban.quiet_nonspawnable_lanes`) | **DEFER** (document as future enhancement) | The module-level `DEFAULT_QUIET_NONSPAWNABLE_LANES` constant is sufficient for the seeded lanes. Custom deployments with additional control-plane lanes would need to edit the constant. A config-key override is a natural next step but out of scope for this card. The error message in the comment tells operators exactly what to add. | Next sprint |
| 12 | `block_task` widening impact on all callers | **AUDIT** (additive, no break) | Existing `block_task` callers operate on `running`/`ready` tasks. Widening to include `review` is additive — it only enables blocking review cards, which was previously impossible (returned `False`). No existing caller breaks. The new caller is `_flag_misassigned_card`. | Immediate |

### "And then what?" trace (end-to-end)

1. **Mis-assigned review card enters dispatcher** → `profile_exists` returns `False` → `_is_quiet_lane` returns `False` → `skipped_misassigned.append((tid, assignee))` → `_flag_misassigned_card` logs error, posts comment, calls `block_task(kind="needs_input")`.
2. **And then what?** `block_task` now matches `status='review'` (clause #3, line 6575) → card moves to `'blocked'`, `source_status='review'` written to block event. Card no longer in `review`/`ready` → won't be re-dispatched next tick.
3. **And then what?** Operator sees the error log / comment, fixes the assignment (reassigns or creates profile), calls `unblock_task` → `_resume_status_from_events` reads `source_status='review'` → card returns to `review` → next dispatch tick spawns it normally.
4. **And then what if operator doesn't intervene?** Card stays blocked. If a cron auto-unblocks it and the mis-assignment persists, next tick re-blocks. After `BLOCK_RECURRENCE_LIMIT=2` re-blocks, clause #2 (line 6536) routes to `'triage'` — forcing human-in-the-loop. Without MUST-FIX #1 clause #2, this escalation would silently fail (rowcount 0, `block_task` returns `False`, card stuck in `review` forever).

### Stakeholders

| Stakeholder | Impact |
|-------------|--------|
| **Operators** | Will see loud errors for mis-assignments instead of silent stalls. May need to add custom control-plane lanes to `DEFAULT_QUIET_NONSPAWNABLE_LANES`. |
| **Plugin authors** | New `skipped_misassigned` field in `DispatchResult` / hook payload. Additive, no break. |
| **CLI users** | New output in `hermes kanban dispatch` (JSON + human). |
| **Gateway daemon** | New `logger.error` for mis-assignments even on idle boards. |

## Consensus & Divergence

### Consensus (all 4 agents agreed)
- The silent skip in both ready and review loops must be replaced with a quiet-lane check + loud path.
- `block_task` must be widened to accept `'review'` status.
- A new `DispatchResult` field is needed to track mis-assigned cards.
- The gateway watcher must log mis-assignments even when no spawns occurred.
- Dry-run must not write to the DB.
- Idempotency is needed to prevent comment spam.

### Divergence

| Topic | [GLM] | [QWEN] | [NEMOTRON] | [DEEPSEEK] | Resolution |
|-------|-------|-------|------------|------------|------------|
| Field name & type | `skipped_misassigned: list[tuple[str,str]]` | `skipped_nonexistent_profile: list[str]` | (not specified) | (no plan produced) | **GLM wins** — tuple carries assignee for CLI/gateway display; "misassigned" is clearer |
| Block kind | `"needs_input"` | `"capability"` | `"dispatch_error"` | — | **GLM wins** — `"dispatch_error"` is NOT in `VALID_BLOCK_KINDS` and would crash (`ValueError`); `"needs_input"` is semantically correct (human intervention needed) |
| Quiet lane list | Configurable from config, seeded with 4 lanes | Hardcoded frozenset with 2 lanes + config note | Hardcoded frozenset with 2 lanes | — | **Merged** — module-level constant seeded with 4 lanes (`orion-cc`, `orion-research`, `ai-code-review`, `ai-knowledge-review`); config override deferred to future enhancement |
| `source_status` fix | Identified and specified | Not mentioned | Not mentioned | — | **GLM wins** — critical for review-card unblock round-trip; verified against `_resume_status_from_events` (line 4489) |
| `_is_quiet_lane` definition | Fully defined with `normalize_profile_name` | Defined (direct membership) | Defined (direct membership) | — | **GLM wins** — normalization prevents case-variant false negatives |
| `_fire_dispatch_tick_hook` idle gate | Identified (line 345–355) | Not mentioned | Not mentioned | — | **GLM wins** — without this, mis-assignment-only ticks are classified `"idle"` and hidden from hooks |
| CLI line refs | 2726/2764 (claimed) | 2726/2764 (from 2nd-order review) | (not specified) | — | **Verified as 2768/2806** — both GLM and the 2nd-order correction were slightly off; actual grep confirms 2768 (JSON) and 2806 (human) |
| Idempotency | Marker-comment check | Mentioned in risks, not implemented | Not addressed | — | **GLM wins** — fully specified with `list_comments` check + marker string |
| `normalize_profile_name` in `_is_quiet_lane` | Yes | No | No | — | **GLM wins** — prevents case-variant bypass |

### Failed/missing sources
- **[DEEPSEEK]**: Produced only a single navigation sentence ("Let me read the main dispatcher loop where results are summarized.") — no plan, no analysis, no contribution. Discarded.