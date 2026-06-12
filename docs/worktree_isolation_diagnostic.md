# Claude Code worktree-isolation defect on Windows

**Status:** platform bug, not an AEGIS bug. This doc is a repro + workaround
note so we stop re-deriving the same mitigations every time we dispatch
parallel agents.

On Windows (`win32`, Windows 11 Home, PowerShell — the AEGIS development
host), the Claude Code `Agent` tool's `isolation: "worktree"` parameter
sets the sub-agent's cwd-override but does NOT run `git worktree add`. The
sub-agent ends up operating on the parent repo's working tree, defeating
the parallel-write safety the flag is supposed to provide.

## Observed symptoms

- Sub-agents launched with `isolation: "worktree"` modify files in the
  parent repo's working tree, not in a sibling worktree directory.
- `git status` in the parent repo after the agent run shows the agent's
  edits as uncommitted changes on the parent branch.
- No side-worktree path is printed in the agent's return value, and no
  `.git/worktrees/<name>` entry is created.
- Reproduced on two separate occasions:
  - **2026-06-05** — parallel build wave (see `docs/REMAINING_WORK.md`,
    "Infra / systemic > Worktree isolation is broken").
  - **2026-06-10** — second hit during a fan-out of investigation agents.

## Reproduction

Approximate steps. The Claude Code surface evolves; treat this as the
shape of the test, not a literal script.

1. From the parent repo on a clean working tree, dispatch any `Agent`
   call with `isolation: "worktree"` and a prompt that creates or
   modifies a tracked file (e.g. "append a line to `README.md`").
2. Wait for the agent to report done.
3. Run `git status` in the parent repo.

**Observed:** the agent's edit appears as an uncommitted change in the
parent repo's working tree.

**Expected (per the tool's documented behavior):** the parent tree
should be unchanged. The edit should live in a side worktree at a
temporary path. If the agent made no changes, the side worktree should
be cleaned up; otherwise the path should be surfaced in the agent's
return value so the orchestrator can decide whether to merge it.

## Implication for AEGIS parallel-agent work

**Trust file-disjointness, not the flag.** The `isolation: "worktree"`
parameter is currently cosmetic on Windows — passing it does not make
parallel writes safe. When dispatching multiple agents in parallel, the
orchestrator must scope each agent to a strictly disjoint set of files
at the WRITE level. Two agents both touching `docs/REMAINING_WORK.md`
are unsafe regardless of whether the flag is set, because the flag
isn't actually creating worktrees.

Cross-reference: `docs/REMAINING_WORK.md` "Infra / systemic > Worktree
isolation is broken" — this doc is the canonical write-up that the
tracker entry points at.

## Mitigation patterns already in use

The following patterns work today and should remain the default until
the platform fix lands:

- **Single-file writers per agent.** Each parallel agent owns a
  non-overlapping set of paths. The orchestrator computes the
  partition before fan-out.
- **Orchestrator does shared-file edits.** Files that every agent's
  work would touch (e.g. `docs/REMAINING_WORK.md`, `CORPUS_FINDINGS.md`,
  the top-level tracker) are edited by the parent agent AFTER
  sub-agents return. Sub-agents report what they did; the parent
  applies the tracker update in one place.
- **Read-only investigation agents are always safe.** Multiple agents
  reading from disjoint or overlapping paths cannot conflict. Use
  fan-out freely for multi-angle investigations (code + logs +
  external API + git history in parallel).

## What to watch for

The platform fix will likely surface as one of:

- A note in the Claude Code release notes that worktree isolation now
  works on Windows.
- An updated `Agent` tool docstring that no longer warns about the
  Windows defect (or that explicitly confirms the flag is honored
  cross-platform).

When that lands, the file-disjointness workaround can relax — parallel
agents can touch overlapping files (e.g. each contributing a section to
the same doc) because the merge will happen in the side worktree and
surface as a path the orchestrator chooses to apply or discard.

## References

- `docs/REMAINING_WORK.md` — "Infra / systemic > Worktree isolation is
  broken" entry (still open as of 2026-06-10).
- Filip's cross-project memory note
  `feedback_claude_code_worktree_isolation_broken_windows.md` (lives in
  `~/.claude/projects/...`, not in this repo — referenced here only so
  a future agent knows the same finding is logged at the user-global
  level).
- Internal Claude Code `Agent` tool description mentions the
  `isolation` parameter but does not currently document the Windows
  defect.
