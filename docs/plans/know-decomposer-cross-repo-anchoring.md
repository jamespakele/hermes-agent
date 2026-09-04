# Working Plan: decomposer cross-repo anchoring fix (kanban t_610fe930)

## Objective

Eliminate the silent cross-repo misroute of decomposed child cards. Incident: epic
t_b25b8c38 (error-sanitization, target `crabby-apps/recipe-base` server/api) had its
regression-guard child (t_65ca3bd5) anchored into `jamespakele/iq-kip-v2` — a different
application — so the guard landed where it protects nothing.

## Root cause (verified, file:line)

1. `hermes_cli/kanban_db.py` `decompose_triage_task` (~7465): worktree children are
   deliberately inserted with `workspace_path = NULL` (comment block 7719-7727) so each
   sibling gets its own worktree directory.
2. At claim time, `_resolve_worktree_workspace` (8090-8176) resolves a NULL
   `workspace_path` against the board's single GLOBAL `default_workdir` (8104-8131).
   That value is mutable at runtime and points at iq-kip-v2 today. UNCONFIRMED
   historical detail: whether it flipped mid-incident (board.json mtime 09:01 sits
   inside the claim window 08:57-09:18, consistent with a flip, but the historical
   value used for each claim is not recoverable). The fix does not depend on
   settling this: anchoring at insert time removes the mutable-global dependency
   entirely.
3. Consequence: any decomposition whose children target a repo other than the board
   default silently misroutes. The resolver HONORS a pre-set `workspace_path`
   (8133-8176): repo-root paths materialize fresh `<repo>/.worktrees/<child-id>` per
   child (8163-8167), and an inherited path occupied by a sibling's branch falls back
   to a fresh worktree under the same repo (8141-8157). Therefore pinning the anchor
   at decompose-insert time fixes the defect without touching the resolver.

Honest scoping: the primary fix for THIS incident is Change 1 (inherit the root's
anchor). The incident child's body contained no repo name at all, so Change 2's
detection would not have fired even with a perfect candidate catalog — Change 2 is
defense-in-depth for children that positively name a different, locally-resolvable
repo, plus the loud-fail net for ambiguous or unresolvable mentions.

## Change 1 — inherit the root's repo anchor (kanban_db.py, decompose_triage_task)

In the child-insert loop (~7716-7731):

- When `child_ws_kind == "worktree"` and the child sets no explicit `workspace_path`:
  set `child_ws_path = root_ws_path` **only when `root_ws_path` is a non-empty string
  AND the root path is an actual git checkout** — verify ONCE per decompose in the
  pre-insert phase (e.g. `<path>/.git` exists, or `git -C <path> rev-parse --git-dir`
  succeeds) and pass the verdict into the insert loop. A non-git root path inherits
  NOTHING: fall back to the legacy NULL path (board-default behavior). Inheriting a
  non-git path would hard-fail at claim time (resolver ValueError) for epics that
  currently work via the board-default fallback — a latent regression, so the guard
  is mandatory.
  (resolver then materializes a fresh per-child worktree — see root-cause #3).
- Keep the legacy NULL fallback when the root has no `workspace_path` (board-default
  behavior preserved; scratch/`dir` roots are unaffected because this branch only
  fires for worktree children).
- Edge case (expected, not a regression): when the root's own `workspace_path` is
  itself a `.worktrees/<...>` checkout path, children inherit it and the resolver's
  occupied-checkout branch (kanban_db.py:8141-8157) materializes a fresh
  `.worktrees/<child-id>` under the same repo — exactly how siblings avoid sharing
  one checkout today.
- Extend the root SELECTs (~7550 and ~7685) to also fetch `project_id`, and add
  `project_id` to the INSERT column list (~7732-7748) with the root's value copied
  verbatim (NULL stays NULL — no fabrication).
- Leave `branch_name` unset → default `wt/<child-id>`.

## Change 2 — deterministic alternate-repo detection, loud failure (same file)

Add a module-level helper near `decompose_triage_task`, e.g.
`_detect_alternate_repo_anchor(root_ws_path, children) -> dict[int, str]`, called in
the pre-insert validation phase (after the full-graph cycle check ~7659-7673, before
the write_txn at ~7684). Runs ONLY when the root is worktree-kind with a non-empty
`workspace_path`. ANY failure aborts with zero rows written (no orphan children —
same guarantee as the cycle rejection).

- Evidence text per child: `title` + `body`.
- Evidence = RESOLVED candidate, never a parsed token. Tokens: owner/name slugs
  (regex like `[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+`) and absolute paths found in the
  text. A token counts as repo evidence ONLY if it resolves to an actual candidate
  checkout (directory-name match, or normalized `git config remote.origin.url`
  owner/name match). `server/api`, `src/api`, `api/v1` etc. never resolve to a
  sibling repo named `server`/`src`/`api`, so they self-filter — no hardcoded
  ignore-list. A token immediately followed by `#<digits>` is a GitHub issue
  reference, not repo evidence — skip it.
- Negation context: a token preceded within ~24 chars by `not`, `no`, `n't`,
  `never`, `rather than`, or `instead of` is IGNORED as evidence (a card saying
  "this is NOT iq-kip-v2" must not re-anchor or fail). Fixed-token regex window,
  not NLP. Documented limitation: a negated mention of the child's REAL target
  would also be ignored — acceptable; loud-fail still covers genuinely ambiguous
  cases.
- Candidate checkouts — DETERMINISTIC set, no broad filesystem walk:
  (a) the root's repo; (b) explicit absolute paths named in the child text;
  (c) projects registered in projects_db (HERMES_HOME-scoped), matched by slug,
  folder basename, or normalized remote owner/name; (d) bounded depth-1 sibling
  scan: only immediate child directories of `root_ws_path.parent` that are git
  repos. Identity = directory-name match, or normalized `git config
  remote.origin.url` owner/name match when present. Never choose the first
  filesystem match silently.
- Re-anchor conservatism: a resolvable non-root repo re-anchors the child ONLY when
  a workspace-locator signal sits near the token — a path fragment containing a
  forward slash beyond the owner/name (e.g. "jamespakele/iq-kip-v2 (src/api: mod.rs)"
  or "crabby-apps/recipe-base server/api"), or locator words (worktree, workspace,
  checkout, "lives in", "anchor to") in the vicinity. A bare cross-repo mention
  WITHOUT a locator signal — e.g. a cross-repo safety assertion like "verify
  jamespakele/iq-kip-v2 is unaffected" — is NOT anchoring evidence: the child
  inherits the root anchor. Without this rule, exactly-one-resolvable-match would
  misroute such a child to the mentioned repo — the original bug recreated in
  reverse.
- Per child: zero locator-signalled repos (or every match == the root's own repo)
  → inherit root anchor (Change 1 default); exactly one locator-signalled repo
  different from the root anchor → re-anchor that child's `workspace_path` to that
  repo root; two or more distinct locator-signalled repos OTHER than the root
  anchor → raise `ValueError`
  naming the child and the conflicting tokens (fail loudly, no partial commit).
  A repo-shaped token that resolves to NO candidate fails loudly ONLY when it is
  positively asserted in an explicit workspace-anchoring context — within ~30 chars
  of an anchoring signal: repo, repository, checkout, clone, fork, anchor, or the
  phrases "lives in", "anchor to", "in repo", "checkout of", "worktree",
  "workspace" — or written as an explicit absolute path that does not exist.
  Bare "project", "remote", "origin", "target" are NOT signal words (they occur in
  ordinary prose — "the apache/arrow project", "our target is", "the origin of the
  bug") and must never trigger a fail-loud on their own.
  Bare owner/name tokens with no signal context (`epic/61`, `wt/t_<id>`,
  multi-segment paths like `server/api/src/middleware/auth.rs`) are prose noise
  and are IGNORED — a decompose must not brick on issue/branch references.
  Negated mentions are not assertions and do not trigger this.
- Explicit per-child `workspace_path` still wins (existing behavior) and is not
  second-guessed by detection.

## Change 3 — externalize the loud failure at the CLI layer (kanban_decompose.py)

`decompose_task`'s `except ValueError` (lines 494-495) currently only returns
`DecomposeOutcome(task_id, False, f"DB rejected graph: {exc}")` — silent on the
board. `block_task` (kanban_db.py:6420+) cannot represent this: it only transitions
running/ready/review, and the epic is in **triage**. Implement instead:

- In the `except ValueError` branch, post a comment on the ROOT task
  (`kanban_db.add_comment` over a fresh connection) with the rejection reason and
  the required human action, append a `decompose_rejected` event if a helper exists
  (otherwise the comment carries the signal), and return the failed
  DecomposeOutcome with a reason that says the epic remains in triage awaiting
  input (needs_input-equivalent at the triage stage).
- The atomic txn rollback in `decompose_triage_task` already guarantees zero
  partial children; add a test asserting exactly that persisted state.

## Change 4 — regression tests (tests/hermes_cli/test_kanban_decompose_db.py,
plus the CLI-helper test in tests/hermes_cli/test_kanban_decompose.py)

Mirror the cyclic-graph test style (existing `kanban_home` fixture,
`decompose_triage_task` calls, ValueError + unchanged-row assertions). Add a helper
that creates tmp git repos (`git -c user.name=test -c user.email=test@test commit`,
plus `git remote add origin <url>` and `git config remote.origin.url`, all offline):

1. `test_decompose_worktree_children_inherit_root_repo_anchor` — root triage task
   created with `workspace_kind="worktree"`, `workspace_path=<tmp repoA>`; two
   children with no repo evidence → each child gets `workspace_path == repoA`,
   `workspace_kind == "worktree"`, `project_id == root's project_id`.
2. `test_decompose_reanchors_child_with_alternate_repo_evidence` — repoB as a
   depth-1 sibling of repoA (both under the same tmp parent) with remote
   `jamespakele/iq-kip-v2.git`; child body positively mentions
   `jamespakele/iq-kip-v2` → that child anchors to repoB; its sibling stays on
   repoA.
3. `test_decompose_rejects_conflicting_repo_evidence` — child body mentions TWO
   resolvable non-root repos → `pytest.raises(ValueError)`; task count and root
   links unchanged after the failure (no orphan children); root still `triage`.
4. `test_decompose_scratch_root_keeps_legacy_null_workspace` — scratch root →
   children keep `workspace_path is None` (legacy board-default behavior intact).
5. `test_decompose_explicit_child_workspace_overrides_detection` — child sets its
   own absolute `workspace_path` → honored verbatim; detection does not
   second-guess it (and a negated repo mention in its body does not fail it).
6. `test_decompose_negated_repo_mention_is_not_evidence` — child body says "this is
   NOT jamespakele/iq-kip-v2" (unresolvable, negated) → inherits root anchor, no
   ValueError (the incident's contrast-mention shape must not false-fail).
7. `test_decompose_unresolvable_repo_mention_fails_loudly` — child body names an
   owner/name repo WITH repo-evidence context (e.g. "the code lives in
   jamespakele/iq-kip-v2") and no local/registered match → ValueError naming the
   token; zero children persisted; root still `triage`. (THE incident-inverse:
   the card's fail-loud policy.)
8. `test_decompose_prose_noise_tokens_do_not_fail` — child body mentions
   `epic/61`, `wt/t_65ca3bd5`, and `server/api/src/middleware/auth.rs` (no
   repo-signal context) → inherits root anchor, NO ValueError (prose-noise
   classes must not brick decompose).
9. CLI layer: test the rejection-comment helper (`_record_decompose_rejection` or
   equivalent) posts a comment on the root and returns cleanly when the DB helper
   raises ValueError from `decompose_triage_task` (mock the LLM payload path or
   call the helper directly).
10. `test_decompose_bare_cross_repo_mention_inherits_root` — child body makes a
    cross-repo safety assertion ("verify jamespakele/iq-kip-v2 is unaffected";
    repoB resolvable as a depth-1 sibling, NO workspace-locator signal) → child
    inherits the root anchor; NO ValueError, NO re-anchor (the false-misroute
    guard).

Tests must not read the real user board (fixture monkeypatches HERMES_HOME/Path.home),
and must NOT depend on the real /srv/data checkout forest: any projects-registry
candidate data is created inside the tmp Hermes home, and sibling-scan scenarios use
tmp directories only. The negation-context rule is a fixed-token regex window, not an
NLP heuristic, and gets its own test case.

## Verification

- Baseline FIRST (already recorded): focused suite green before any edit.
- `venv/bin/python -m pytest tests/hermes_cli/test_kanban_decompose.py
  tests/hermes_cli/test_kanban_decompose_db.py -q` → all pass, including the new
  tests.
- Broader: `venv/bin/python -m pytest tests/hermes_cli/test_kanban_*.py -q`
  (record counts; pre-existing failures noted, e.g. historical httpx ones).
- Read the final diff for accidental damage beyond the four changes.

## Constraints

- omp does NOT commit. The ORCHESTRATOR reviews the diff, runs the suite, and
  commits code + tests + plan doc together (`fix(kanban): …` style, mirroring
  the t_1ea36bfb precedent) BEFORE any board mutation — the card demands a
  commit before completion.
- Do NOT touch any kanban board state from omp; board mutations belong to the
  orchestrator.
- `project_id` inheritance copies the root's value verbatim (NULL stays NULL —
  no fabrication).
- Canonical single tree: /srv/data/hermes/hermes-agent (symlinked from
  /home/pakele/.hermes/hermes-agent — the live editable install; no second-copy sync).

## Logical Consequences

1. **[Immediate] Every future decompose of a worktree-kind epic pins children to the
   epic's own repo** — change: child rows carry the root's workspace_path instead of
   NULL. Keep: resolver, cycle check, assignee plumbing untouched. Rationale: the
   mutable board-level default_workdir can no longer silently re-anchor siblings of a
   multi-repo epic mid-flight (the t_65ca3bd5/t_d928029a failure mode). Risk accepted:
   boards that RELIED on default_workdir for decomposed children (root without
   workspace_path) keep the legacy NULL path — behavior unchanged for them.
2. **[Immediate] Ambiguous or unresolvable repo evidence fails the whole decompose
   loudly** — change: a child whose text names 2+ distinct resolvable repos, or
   positively names an unresolvable repo, raises ValueError before any insert; the
   epic stays in triage with a rejection comment (zero children). Keep: the
   no-orphan-children guarantee the cycle check established. Amplify: makes misroute
   impossible to do silently; a human resolves the anchor and re-runs decompose.
   Mitigate: negation context, issue-ref exclusion (`#digits`), and
   resolution-not-exclusion keep prose mentions and path fragments from
   false-failing.
3. **[Next epic] The corrected guard card (D2) is NOT protected by this fix** —
   change: it is a standalone card, so it must carry an explicit workspace_path
   anchor at creation (board default is iq-kip-v2 right now). Keep: t_65ca3bd5 stays
   unarchived while t_d928029a references it. Remove: nothing. Rationale: the D1 fix
   covers decompose-insert only; standalone cards always needed explicit anchors.
4. **[Next sprint] t_d928029a's re-verdict has no automatic trigger** — change: this
   worker cannot unblock it (hard card constraint); the unblock + re-verdict handoff
   must be explicit in this card's completion summary for the parent dispatcher.
   Keep: reviewer (run 1470 verdict) authority over the epic's close. Rationale:
   forcing the review card open would bypass the SDLC gate the card protects.
5. **[Next quarter] Unresolvable positive repo mentions now fail loudly instead of
   inheriting** — change: a decomposed child naming a repo that is neither a local
   sibling nor a registered project blocks the decompose with a needs_input-style
   rejection comment. Mitigate: negation window + issue-ref exclusion keep contrast
   mentions and issue links flowing. Risk accepted: higher human-touch rate for
   epics whose children legitimately reference uncloned repos — the accepted
   trade per the card's "fail loudly instead of silently misrouting"; operators
   clone the repo (making detection resolvable) or rephrase the body.