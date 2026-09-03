# Working Plan: Assign-Time Guard for Non-Existent Profile in Kanban Assign/Reassign

**Card:** t_fe4f52a4
**Status:** Planning — awaiting approval before implementation

---

## Objective

Add a pre-flight guard to `hermes kanban assign` and `hermes kanban reassign` that rejects assignment to a profile not present on disk under `~/.hermes/profiles/` (the default Hermes root's `profiles/` directory). When the profile is missing, print a loud warning to stderr stating the profile does not exist and that review cards assigned to it will not auto-dispatch, then exit non-zero. An explicit `--force` flag overrides the guard (warning still printed, assignment proceeds). Unassigning via `none`/`-`/`null` must remain unaffected. Valid on-disk profiles (ai-coder, ai-code-review, ai-knowledge, etc.) must pass through silently with no behavior change.

---

## Constraints

- **Language:** Python 3 (codebase uses `from __future__ import annotations`).
- **Framework:** argparse-based CLI (`hermes_cli/kanban.py`), SQLite-backed kanban DB (`hermes_cli/kanban_db.py`).
- **No new dependencies.** The helper `kb.list_profiles_on_disk()` already exists at `kanban_db.py:12127` and is reachable via the existing import `from hermes_cli import kanban_db as kb` (kanban.py:27). It reads `get_default_hermes_root() / "profiles" / <name> / config.yaml` directly with no heavy import chain.
- **No DB/schema/event changes.** The guard is a pure pre-flight check before the DB write; no migration, no new event kind, no hook implications.
- **`sys` is already imported** at kanban.py:22.
- **`run_slash` (kanban.py:3409)** uses the full argparse tree via `build_parser`, so `--force` will be threaded through to the namespace automatically. No special handling needed in `run_slash`.

---

## File Structure

### Files to modify (2)

| File | Lines (approx) | Change |
|------|-----------------|--------|
| `hermes_cli/kanban.py` | ~495 (p_assign parser) | Add `--force` flag to `assign` subparser |
| `hermes_cli/kanban.py` | ~527 (p_reassign parser) | Add `--force` flag to `reassign` subparser |
| `hermes_cli/kanban.py` | ~1889 (_cmd_assign) | Insert profile-existence guard after `profile` normalization, before DB call |
| `hermes_cli/kanban.py` | ~1944 (_cmd_reassign) | Insert identical guard after `profile` normalization, before DB call |
| `tests/hermes_cli/test_kanban_cli.py` | append | Add 5–6 test functions covering phantom reject, phantom+force, valid, unassign, reassign mirror |

### Files NOT modified

- `hermes_cli/kanban_db.py` — `list_profiles_on_disk()`, `assign_task()`, `reassign_task()` are used as-is.
- `hermes_constants.py` — `get_default_hermes_root()` is used as-is.
- No new files created.

---

## Implementation Notes

### Design decision: reject by default, `--force` to override

Consistent across all source agents ([GLM], [DEEPSEEK], [QWEN], [NEMOTRON]). Rejecting by default is fail-safe (no silent phantom acceptance, satisfying AC3). The `--force` escape hatch supports legitimate "assign before profile dir exists" workflows without requiring the user to create a dummy profile.

**Exit code:** return `1` (not `2`), matching the existing convention in `_cmd_assign` which already returns `1` for "no such task".

### Guard insertion point

The guard must be placed **after** the `profile` normalization line:
```python
profile = None if args.profile.lower() in {"none", "-", "null"} else args.profile
```
and **before** the `kb.connect_closing()` / `kb.assign_task()` or `kb.reassign_task()` call. This ensures:
- Unassign sentinels (`none`/`-`/`null`) → `profile=None` → guard skipped entirely → unassign path untouched.
- The guard runs before any DB write, so a rejected assignment leaves the board untouched (no clobbering of existing assignee).

### Helper: `_check_profile_exists`

Rather than duplicating the guard block in both commands, extract a shared helper:

```python
def _check_profile_exists(profile: str | None, force: bool, task_id: str, action: str) -> int:
    """Return 1 to reject (caller returns immediately), 0 to proceed."""
    if profile is None:
        return 0  # unassign — no check
    if profile in kb.list_profiles_on_disk():
        return 0  # valid profile — proceed silently
    print(
        f"WARNING: profile '{profile}' does not exist under "
        f"~/.hermes/profiles/ — review cards assigned to it will "
        f"NOT auto-dispatch.",
        file=sys.stderr,
    )
    if not force:
        print(
            f"refusing to {action} {task_id} to missing profile "
            f"'{profile}' (pass --force to override)",
            file=sys.stderr,
        )
        return 1
    return 0
```

Then in both `_cmd_assign` and `_cmd_reassign`, insert after the normalization line:
```python
rc = _check_profile_exists(profile, getattr(args, "force", False), args.task_id, "assign")
if rc:
    return rc
```

### `--force` flag definition

For both `p_assign` (~line 497, after `profile` arg) and `p_reassign` (~line 537, after `--reason`):
```python
p_assign.add_argument(
    "--force", action="store_true",
    help="Allow assignment to a profile that does not exist on disk "
         "(review cards assigned to it will not auto-dispatch)",
)
```

Neither subparser currently defines `--force` (confirmed by reading both blocks — no collision).

### Defensive `getattr(args, "force", False)`

The `getattr` fallback ensures the guard is inert (treats force as False → rejects) if `--force` is absent from the namespace, which protects older callers or code paths that construct the namespace manually. This is fail-safe.

### `list_profiles_on_disk()` behavior

- Returns `sorted(names)` as `list[str]`, including implicit `"default"` when the root exists.
- Returns `[]` on any exception (e.g., `hermes_constants` import failure). In that case, the guard would reject ALL named assignments — this is fail-safe per AC3 but could surprise users in a broken install. The `--force` escape hatch mitigates this.
- In the test fixture `kanban_home`, the fixture creates `tmp_path/.hermes` with no `profiles/` subdirectory, so `list_profiles_on_disk()` returns `["default"]` (root exists, no profiles dir). Tests for valid profiles must create real profile dirs (e.g., `home / "profiles" / "ai-coder" / "config.yaml"`); tests for phantom profiles can use any name not in the returned set.

### Edge cases

1. **Case sensitivity:** `list_profiles_on_disk()` returns exact directory names. A user typing `AI-Coder` would be rejected. This matches existing on-disk naming conventions and is acceptable.
2. **`profile=None` (unassign):** Guard is gated on `profile is not None`, so unassign never triggers the check.
3. **`reassign` with `--reclaim`:** The guard runs before the `reclaim_first` logic in `reassign_task`. If the guard rejects, no reclaim happens, which is correct — there's no point reclaiming a running worker if the reassignment target is invalid.
4. **Delegation policy:** `_DELEGATED_CHILD_DENIED_ACTIONS` (kanban.py:1209) already includes `assign` and `reassign` — the guard is orthogonal to delegation policy; no change needed.

---

## Verification Criteria

All checks are runnable via `pytest tests/hermes_cli/test_kanban_cli.py -v` from the repo root. The `kanban_home` fixture (line 18) provides an isolated `HERMES_HOME` with `monkeypatch.setattr(Path, "home", ...)`.

### Test helper functions (add to test file)

```python
def _make_profile(home, name):
    p = home / "profiles" / name
    p.mkdir(parents=True)
    (p / "config.yaml").write_text(f"name: {name}\n")
    return p

def _create_task(home):
    out = kc.run_slash("create 'gt' --assignee none")
    import re
    m = re.search(r"(t_[a-f0-9]+)", out)
    assert m, out
    return m.group(1)
```

### AC1: Assigning to a non-existent profile produces a visible warning

```python
def test_assign_phantom_profile_rejected(kanban_home, capsys):
    tid = _create_task(kanban_home)
    args = kc.build_parser(_dummy_sub()).parse_args(["kanban", "assign", tid, "ghost"])
    # Use top-level parser; see test_board_override for pattern
    rc = kc.kanban_command(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "ghost" in err and "does not exist" in err
    assert "NOT auto-dispatch" in err
```

**Assertion:** exit code `1`, stderr contains profile name + "does not exist" + "NOT auto-dispatch". Task `assignee` remains unset.

### AC2: Assigning to a valid profile works as before

```python
def test_assign_valid_profile_unaffected(kanban_home, capsys):
    _make_profile(kanban_home, "ai-coder")
    tid = _create_task(kanban_home)
    args = _parse(["kanban", "assign", tid, "ai-coder"])
    rc = kc.kanban_command(args)
    assert rc == 0
    assert capsys.readouterr().err == ""  # NO warning for valid profile
```

**Assertion:** exit code `0`, stderr empty (no warning), task assignee set to `ai-coder`.

### AC3: No silent acceptance of phantom profiles

Two sub-checks:

```python
def test_assign_phantom_profile_forced_warns_but_assigns(kanban_home, capsys):
    tid = _create_task(kanban_home)
    args = _parse(["kanban", "assign", tid, "ghost", "--force"])
    rc = kc.kanban_command(args)
    assert rc == 0
    err = capsys.readouterr().err
    assert "ghost" in err and "does not exist" in err  # warning still printed
    # Assignment recorded despite phantom
    with kb.connect() as conn:
        row = conn.execute("SELECT assignee FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["assignee"] == "ghost"
```

```python
def test_reassign_phantom_rejected(kanban_home, capsys):
    _make_profile(kanban_home, "ai-coder")
    tid = _create_task(kanban_home)
    kc.kanban_command(_parse(["kanban", "assign", tid, "ai-coder"]))
    capsys.readouterr()
    args = _parse(["kanban", "reassign", tid, "ghost"])
    rc = kc.kanban_command(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "ghost" in err and "does not exist" in err
    # Still assigned to ai-coder — not clobbered
    with kb.connect() as conn:
        row = conn.execute("SELECT assignee FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["assignee"] == "ai-coder"
```

### Unassign regression guard

```python
def test_assign_none_still_unassigns(kanban_home, capsys):
    tid = _create_task(kanban_home)
    args = _parse(["kanban", "assign", tid, "none"])
    rc = kc.kanban_command(args)
    assert rc == 0
    assert capsys.readouterr().err == ""  # unassign must NOT trigger guard
```

**Note on parser construction in tests:** The existing test `test_board_override_is_isolated_per_concurrent_call` (line 73) constructs the parser via `argparse.ArgumentParser(...) → sub → kc.build_parser(sub)` and then `parser.parse_args(["kanban", "--board", board, "create", title])`. Tests should follow the same pattern. Define a module-level helper `_parse(tokens)` that builds the parser once and parses.

### Runnable command summary

```bash
cd /home/pakele/.hermes/hermes-agent
pytest tests/hermes_cli/test_kanban_cli.py -v -k "assign or reassign or phantom or valid_profile or unassign"
```

---

## Logical Consequences

### Second-order review

1. **Find every reference site for the changed concept (profile assignment guard / profile validation):**

   | Site | Location | Decision | Rationale |
   |------|----------|----------|-----------|
   | `_cmd_assign` | kanban.py:1889 | **MODIFY** — insert guard | Primary target per spec |
   | `_cmd_reassign` | kanban.py:1944 | **MODIFY** — insert guard | Primary target per spec |
   | `p_assign` parser | kanban.py:495 | **MODIFY** — add `--force` | Override mechanism |
   | `p_reassign` parser | kanban.py:527 | **MODIFY** — add `--force` | Override mechanism |
   | `assign_task` | kanban_db.py:3710 | **KEEP** — no change | Guard is pre-flight; DB layer unchanged |
   | `reassign_task` | kanban_db.py:5238 | **KEEP** — no change | Guard is pre-flight; DB layer unchanged |
   | `list_profiles_on_disk` | kanban_db.py:12127 | **KEEP** — used as-is | Helper already does what we need |
   | `known_assignees` | kanban_db.py:12167 | **KEEP** — no change | Different code path (listing, not assigning) |
   | `_DELEGATED_CHILD_DENIED_ACTIONS` | kanban.py:1209 | **KEEP** — already denies assign/reassign in delegated children | Guard is orthogonal to delegation policy |
   | `run_slash` | kanban.py:3409 | **KEEP** — no change | Uses full argparse tree, so `--force` threads through automatically |
   | `create` with `--assignee` | kanban.py:341 (parser), 1568 (handler) | **KEEP** — no guard | Spec scopes guard to assign/reassign only; `create --assignee` is a different flow. A task created with a phantom assignee will not auto-dispatch but this is pre-existing behavior. Out of scope for this card. |
   | `_cmd_set_model` | kanban.py:1912 | **KEEP** — no change | Model override, not profile assignment |

2. **Trace data/logic flow end-to-end:**

   ```
   User: hermes kanban assign <tid> ghost
     → argparse parses: args.profile="ghost", args.force=False
     → _cmd_assign: profile = "ghost" (not in sentinel set)
     → _check_profile_exists("ghost", False, tid, "assign")
       → kb.list_profiles_on_disk() → e.g. ["default"] (in test) or full list (production)
       → "ghost" not in list → print WARNING to stderr → return 1
     → _cmd_assign returns 1
     → kanban_command returns 1
     → CLI exits 1, task board untouched
   ```

   No breaks: the DB write never executes, so `assign_task`'s `write_txn` is never opened, no events are appended, no `task_updated` hook fires.

3. **"And then what?" trace (×2):**

   - **Consequence A: User sees rejection → creates the profile dir → retries.**
     - And then? Assignment succeeds, worker dispatches normally.
     - And then? No further intervention needed — guard is transparent for valid profiles.

   - **Consequence B: User sees rejection → uses `--force` → phantom profile recorded.**
     - And then? Task is assigned to a profile with no `config.yaml`. The dispatch system will fail to spawn a worker for this profile (no model/provider config).
     - And then? The task sits idle or the dispatch loop logs an error. The user was warned, so this is expected/intentional behavior.

   - **Consequence C: `list_profiles_on_disk()` returns `[]` due to import failure.**
     - And then? ALL named profiles are rejected unless `--force`. This is fail-safe.
     - And then? Users in a broken install see warnings on every assignment. The `--force` escape hatch lets them proceed. This is a degraded but safe state.

4. **Time horizons:**

   | Consequence | Horizon | Decision |
   |------------|---------|----------|
   | Guard rejects phantom profiles in assign/reassign | **Immediate** | Intended — amplify via clear stderr message |
   | `--force` escape hatch for pre-profile workflows | **Immediate** | Intended — documented in `--help` |
   | `create --assignee <phantom>` still has no guard | **Next sprint** | Keep (out of scope); potential future card to add guard to `create` |
   | `list_profiles_on_disk()` returns `[]` on broken install → all assignments rejected | **Next quarter** | Mitigate by logging a diagnostic when the helper returns empty AND profiles_dir doesn't exist (distinguish "no profiles configured" from "helper crashed") |
   | Case-sensitivity mismatches (e.g., `AI-Coder` vs `ai-coder`) | **Next sprint** | Keep (matches existing conventions); document in `--help` if user confusion reported |

5. **Stakeholders affected:**

   - **Operators using `hermes kanban assign/reassign` interactively:** Will see clear warnings for typos/phantom profiles. Positive — catches mistakes early.
   - **Automated scripts/pipelines that assign tasks:** Scripts passing phantom profiles (likely bugs) will now fail loudly. Positive — surfaces latent misconfigurations. Scripts passing `--force` explicitly opt in.
   - **Developers extending the kanban CLI:** The `_check_profile_exists` helper is reusable; future commands that accept profile names can call it.

6. **Dead UI/docs/data cleanup:**
   - No existing docs reference a `--force` flag for assign/reassign (it doesn't exist yet), so no docs to update.
   - The `assignees` command (kanban.py:~1555) already shows `ON DISK` column from `known_assignees` — this remains accurate and now aligns with the guard logic (both use `list_profiles_on_disk()`).
   - No deprecated behavior to remove; the guard is purely additive.

---

## Consensus & Divergence

### Consensus (all 4 agents agree)
- **Behavior:** Reject by default (exit 1), allow with `--force`. [GLM], [DEEPSEEK], [QWEN], [NEMOTRON] all converge here.
- **Guard placement:** After `profile` normalization, before DB call. All 4 agree.
- **No new imports needed:** `kb` (kanban_db) and `sys` already imported. All confirmed.
- **Unassign (`none`/`-`/`null`) bypasses guard:** All 4 agree — guard gated on `profile is not None`.
- **Warning to stderr:** All 4 agree on `file=sys.stderr`.
- **Exit code 1:** [GLM] explicitly notes matching existing convention; others use `return 1` without comment.

### Divergence
- **QWEN** initially proposed "no `--force` flag needed" but then immediately contradicted itself by including `--force` in its own code. Disregard the inconsistency; the `--force` design is unanimous in the actual code proposals.
- **Warning message wording:** Minor textual differences across agents. The plan standardizes on: `WARNING: profile '{profile}' does not exist under ~/.hermes/profiles/ — review cards assigned to it will NOT auto-dispatch.` This satisfies the spec's requirement that the message "clearly states the profile is missing and that review cards assigned to it will not auto-dispatch."
- **Shared helper vs inline:** [GLM], [DEEPSEEK], [NEMOTRON] propose inline guard blocks in each command; the plan prefers a shared `_check_profile_exists` helper to avoid duplication. This is a minor structural choice that doesn't affect behavior.
- **Test approach:** [GLM] provides the most detailed test code with concrete fixture helpers (`_make_profile`, `_create_task`). [DEEPSEEK] provides manual CLI scenarios. The plan adopts [GLM]'s test-oriented approach as the verification basis.

### Source status
- All 4 sources completed successfully (`status: done`). No failed or missing sources.