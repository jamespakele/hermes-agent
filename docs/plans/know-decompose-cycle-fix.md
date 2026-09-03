# Working Plan: Fix auto-decomposer circular dependency in decomposed test epics

Card: t_1ea36bfb — Fix auto-decomposer circular dependency in decomposed test epics (2nd occurrence)
Repo: /home/pakele/.hermes/hermes-agent (branch main, HEAD 9bdc58749)
Workdir: /home/pakele/.hermes/hermes-agent

## Objective

Eliminate the semantic deadlock the auto-decomposer produces for test-plan epics
(impl story gated behind the epic, epic gated behind the test-executor, test-executor
needs the impl). This is the SECOND occurrence (s01 t_495e5d6f, s02 t_535273e8 both
blocked with kind=needs_input "DEADLOCK in task graph"). The fix must make the
decomposer produce a graph where the test-executor has the impl story among its
parents and the epic/terminal node never gates the impl story it depends on, plus a
cycle check before committing the graph.

## Root cause (identified)

Two layers conspire:

1. **LLM layer (`hermes_cli/kanban_decompose.py`)** — `_USER_TEMPLATE` (lines 112-121)
   passes only `task_id`, `title`, `body`, `roster`, `default_assignee`. It does NOT
   pass the root task's existing children. The dispatch script (`kanban-dispatch.sh`
   step 3) creates the impl stories (e.g. t_7f76791c "s01: Widget API endpoint") as
   children of the epic via `--parent` BEFORE decompose runs. The decomposer never
   sees them, so it cannot express "test-executor depends on impl story" as a parent.
   Its `parents` field only accepts int indices into the new children list.

2. **DB layer (`hermes_cli/kanban_db.py` `decompose_triage_task`)** — lines 7643-7652
   link the ROOT as a child of EVERY new child (`INSERT ... (cid, task_id)`). The impl
   story remains a child of root (impl → root, from dispatch). The test-executor is a
   child of root (root waits for it). So: test-executor's WORK needs impl, but impl is
   gated behind root, which is gated behind test-executor. **Semantic deadlock** — the
   graph is a DAG (no graph cycle), so the existing Kahn's sibling check (lines
   7524-7545) passes, but the work can never dispatch. The test-executor blocks itself
   (kind=needs_input) because it cannot run without impl, and impl can never dispatch
   because root gates it.

The validated manual recovery (applied both times) is exactly the fix the code should
do automatically:
1. `unlink <epic> <impl-story>` — release impl from root's gating
2. `link <impl-story> <test-executor>` — test now waits on impl
3. `unblock <test-executor>` — recompute_ready promotes it once impl is done

## Constraints

- Backward compatible: existing decompose calls (int-index `parents`) must keep working.
- Atomic: the whole decompose (create children + link + unlink + root flip) stays in a
  single write_txn; a malformed graph aborts cleanly with no orphan children.
- The fix lives in the Hermes source repo (`hermes_cli/kanban_decompose.py` +
  `hermes_cli/kanban_db.py`), not the dispatch script — the deadlock is a decompose
  graph-construction bug, and the card's ACs target the decompose code.
- Do NOT touch the live board DB. All testing uses the isolated temp-DB fixture pattern
  already in `tests/hermes_cli/test_kanban_decompose_db.py`.

## File Structure

- `hermes_cli/kanban_decompose.py` — LLM layer: pass root's existing children to the
  LLM; allow `parents` to reference pre-existing root children by task id.
- `hermes_cli/kanban_db.py` — DB layer: extend `decompose_triage_task` to accept
  pre-existing-root-child parents, auto-repair the deadlock (unlink impl from root),
  and add a full-graph cycle check before commit.
- `tests/hermes_cli/test_kanban_decompose_db.py` — regression test: decompose a
  two-sprint epic with impl + test children; assert acyclic + impl precedes tests.
- `docs/plans/know-decompose-cycle-fix.md` — this plan.

## Implementation Notes

### 1. LLM layer — `hermes_cli/kanban_decompose.py`

**1a. `_USER_TEMPLATE`** — add a section listing the root's existing children so the
decomposer can reference them as parents:

```
Existing children of this task (may be referenced as parents by id):
{existing_children}
```

Where `existing_children` is a formatted list of `"<id>: <title>"` lines, or
`(none)` when the root has no children. Build it in `decompose_task` by querying
`kb.child_ids(conn, task_id)` and fetching each child's title.

**1b. `_SYSTEM_PROMPT`** — update the `parents` rule to allow referencing existing
children by id:

```
- "parents" is a list of INDICES (0-based) into this same "tasks" list, OR the
  task ids of the "Existing children" listed in the prompt. Tasks with no parents
  run in PARALLEL. Tasks with parents wait until every parent completes.
- When a new task's work depends on an existing child (e.g. a test-executor that
  must run against an implementation story already created), list that existing
  child's id in "parents" so the new task waits for it.
```

**1c. `decompose_task` parsing** — in the children loop (lines 390-430), accept
`parents` entries that are either int indices OR string ids of pre-existing root
children. Validate string ids against the actual pre-existing child id set; drop
unknown/out-of-range entries (log a warning). Pass the resolved parents through to
`kb.decompose_triage_task` unchanged (it will handle both forms).

### 2. DB layer — `hermes_cli/kanban_db.py` `decompose_triage_task`

**2a. Pre-validate `parents`** (extend the existing loop at lines 7507-7522): a parent
entry may be an int index (0 <= p < len(children), p != idx) OR a string id that is a
pre-existing child of the root. Load the root's existing child ids once at the top
(`SELECT child_id FROM task_links WHERE parent_id = ?`). Reject a string id that is not
a pre-existing root child.

**2b. Full-graph cycle check + auto-repair (BEFORE the write_txn).** Build the graph:
nodes = {root} ∪ {pre-existing root children} ∪ {new children}; edges = existing
task_links among these + the sibling parent links to add (int indices) + the
pre-existing-root-child parent links to add (string ids) + the root-under-child links
to add (root is child of every new child). Run Kahn's topological sort. If a cycle is
detected:

- **Auto-repair (the deadlock pattern):** for each new child that lists a pre-existing
  root child X as a parent, if X is currently a child of root (X → root edge exists),
  mark X for unlink from root. This is the "epic must not gate the impl story it
  depends on" rule. Re-run the cycle check on the repaired graph.
- If still cyclic after repair, **reject**: raise `ValueError("cyclic dependency
  detected in decomposed children list (including root links)")` so the caller
  returns `ok=False` and nothing is committed.

**2c. Inside the write_txn**, after creating children and linking sibling parents:

- For each pre-existing root child X marked for unlink: `DELETE FROM task_links WHERE
  parent_id = X AND child_id = <root>` and append an `unlinked` event on the root.
- Link pre-existing-root-child parents: for each new child with a string-id parent X,
  `INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (X, <new_child>)` and
  append a `linked` event on the new child.
- Link the root under every new child (unchanged, lines 7647-7652) — after the unlink,
  this is cycle-free.

**2d. `recompute_ready`** after the txn (already at line 7693-7694) will promote the
impl story (now unlinked from root) to `ready` and keep the test-executor in `todo`
until impl is done — exactly the validated recovery outcome.

### 3. Regression test — `tests/hermes_cli/test_kanban_decompose_db.py`

Add `test_decompose_test_epic_impl_precedes_tests`:

- Create a triage root task.
- Pre-create an impl story as a child of the root (simulating the dispatch script's
  `--parent`): `kb.create_task(..., parents=[root_id], triage=False)`.
- Call `kb.decompose_triage_task` with children:
  - `[0]` test-plan (no parents)
  - `[1]` test-executor (parents=[0, <impl_id>]) — depends on the impl story by id
- Assert:
  - `child_ids` is not None (decompose succeeded, no rejection).
  - The graph is acyclic (walk parent→child edges from every node; assert no node is
    reachable from itself).
  - The impl story is NO LONGER a child of root (`parent_ids(root)` does not contain
    impl_id) — the epic no longer gates the impl.
  - The test-executor has the impl story among its parents (`parent_ids(test_executor)`
    contains impl_id) — impl precedes tests.
  - The test-executor is a child of root (root waits for it).
  - `recompute_ready` promotes the impl story to `ready` and leaves the test-executor
    in `todo` (impl must complete first).

Also add `test_decompose_rejects_unresolvable_cycle`:
- Create a triage root with a pre-existing child X that is a child of root.
- Call decompose with a child that lists X as a parent AND X is also a parent of root
  in a way that cannot be repaired (e.g. X → root and root → X both required) — assert
  `decompose_triage_task` raises `ValueError` (reject path) and no children are created.

### 4. Documentation

Add a short section to `kanban-pipeline-operations/SKILL.md` (Pitfall 31) documenting
the correct decomposition pattern for test-plan epics: the test-executor must have the
impl story among its parents, and the epic/terminal node must never gate the impl story
it depends on. Reference the auto-repair behavior now in the decomposer.

## Verification Criteria

- [ ] `pytest tests/hermes_cli/test_kanban_decompose_db.py tests/hermes_cli/test_kanban_decompose.py -q` — all pass (existing 5 + new 2).
- [ ] The new regression test FAILS on the old code (before the fix) and PASSES after.
- [ ] `pytest tests/hermes_cli/ -q` — no regressions in the broader kanban suite.
- [ ] The full-graph cycle check rejects a genuinely unresolvable cycle (no orphan children).
- [ ] Existing int-index `parents` behavior is unchanged (backward compatible).

## Logical Consequences

| Site | Decision | Horizon | Type | Notes |
|------|----------|---------|------|-------|
| `hermes_cli/kanban_decompose.py` `_USER_TEMPLATE` | Add existing-children section | Immediate | Intended | Decomposer must see impl stories to reference them |
| `hermes_cli/kanban_decompose.py` `_SYSTEM_PROMPT` | Allow id-based parents | Immediate | Intended | Enables test-executor → impl dependency |
| `hermes_cli/kanban_decompose.py` `decompose_task` parsing | Accept string-id parents | Immediate | Intended | Validate against pre-existing root children |
| `hermes_cli/kanban_db.py` `decompose_triage_task` pre-validate | Accept string-id parents | Immediate | Intended | Backward compatible with int indices |
| `hermes_cli/kanban_db.py` full-graph cycle check | Add before commit | Immediate | Intended | Reject or auto-repair; AC2 |
| `hermes_cli/kanban_db.py` auto-repair unlink | Unlink impl from root | Immediate | Intended | "Epic must not gate impl" rule; matches validated recovery |
| `hermes_cli/kanban_db.py` root-under-child link | Keep (after unlink, cycle-free) | Immediate | Intended | Root still wakes when graph completes |
| `tests/hermes_cli/test_kanban_decompose_db.py` | Add 2 regression tests | Immediate | Intended | AC3: fails on old, passes on fix |
| `kanban-pipeline-operations/SKILL.md` | Add Pitfall 31 doc | Next sprint | Intended | Document correct test-epic decomposition pattern (AC5) |
| Live board DB (`~/.hermes/kanban.db`) | Do NOT touch | Immediate | Unintended-negative | All testing on isolated temp-DB fixture |
| Dispatch script `kanban-dispatch.sh` | Keep unchanged | Immediate | Keep | Deadlock is a decompose bug, not a dispatch bug; impl stories still created as root children, decompose now repairs |
| `hermes_cli/kanban_db.py` `link_tasks`/`_would_cycle` | Keep unchanged | Immediate | Keep | Decompose uses raw INSERTs in one txn; the new full-graph check covers it |
| Existing int-index `parents` callers | Keep working | Immediate | Intended | Backward compatibility preserved |

## Pre-mortem flip

If this shipped and a user still hit a deadlocked test epic six months from now, what
went wrong? Likely: (a) the decomposer LLM still didn't reference the impl story by id
(needs the prompt to be explicit), or (b) a NEW cycle shape not covered by the
auto-repair (e.g. two impl stories cross-depending). Mitigation: the full-graph cycle
check rejects any unresolvable cycle (no silent deadlock — the decompose fails loudly
instead of producing a stuck graph), and the regression test pins the canonical
test-epic shape. If a new shape appears, the reject path surfaces it as a clear
`ValueError` rather than a blocked card.
