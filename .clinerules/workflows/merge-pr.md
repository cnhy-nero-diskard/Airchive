# Merge PR

Merge a GitHub pull request into `master`, after making sure its associated OpenSpec
change is synced and archived. Also handles semver bumps.

## Overview

Two independent operations:

- **Merge a PR** — resolve the PR, gate on OpenSpec sync + archive (skippable when the
  unarchive was deliberate), then merge into `master`.
- **Bump a version** — check out `master`, run the bump-tag script for a
  major/minor/patch bump.

## Mode A — Merge a PR

### 1. Resolve the PR

- If the user gave a PR number or URL, use it.
- Otherwise run:
  ```bash
  gh pr list --state open --json number,title,headRefName,baseRefName
  ```
  and present the open PRs (number, title, branch) for the user to choose.
  **Do not guess or auto-select a PR.**

### 2. Resolve the associated OpenSpec change

- Derive the change name from the PR's head branch: strip the leading `type/` prefix
  (e.g. `fix/auditfix-day-attribution` → `auditfix-day-attribution`).
- Confirm it exists: `openspec list --json` for active changes, or look under
  `openspec/changes/archive/` for an already-archived change.
- If no change can be matched, report it and ask whether to merge anyway or stop.

### 3. Sync + archive gate

**Deliberate-unarchive skip:** If the prompt explicitly says the unarchive was
deliberate (e.g. "the unarchive was deliberate", "skip sync/archive", "merge without
archiving"), skip this gate entirely and go straight to merge.

Otherwise:

1. Check out the PR's head branch so the sync/archive edits land on the branch being
   merged. If that branch is already checked out in a separate worktree, switch to
   that worktree directory instead:
   ```bash
   git checkout <head-branch>
   # or, when using a worktree:
   cd <path-to-worktree>
   ```
2. Run `openspec status --change "<name>" --json` to inspect delta specs.
3. If the change is still active and has delta specs → run `openspec-sync-specs`
   (agent-driven) to sync them into `openspec/specs/`, then commit and push; then run
   `openspec-archive-change` to archive the change, then commit and push again.
4. If the change is still active but has no delta specs → archive it directly, then
   commit and push.
5. If the change is already archived → nothing to do.

Commit and push after **each** sync and **each** archive occurrence — they are separate
operations and each must be recorded on the branch:
```bash
git add openspec/
git commit -m "chore: sync specs for <name>"
git push
# ...then, after archiving:
git commit -m "chore: archive <name> change"
git push
```

### 4. Merge

```bash
gh pr merge <number> --merge --delete-branch
```

- Use `--squash` or `--rebase` if the user asks.
- The base branch is `master` (this repo's default branch).
- Report the merge result.

## Mode B — Bump a version

1. Ensure a clean working tree, then:
   ```bash
   git checkout master
   git pull --ff-only
   ```
2. Run the bump script (Windows PowerShell or WSL/bash):
   ```powershell
   ./scripts/bump-tag.ps1 -Major   # or -Minor, -Patch
   ```
   ```bash
   ./scripts/bump-tag.sh --major   # or --minor, --patch
   ```
   Use `-DryRun` / `--dry-run` first to preview the next tag.
3. Report the new tag and that it was pushed to origin (this triggers the release
   workflow).

## Guardrails

- Never merge without resolving which PR (prompt when ambiguous).
- Do not skip the sync + archive gate unless the prompt explicitly marks the unarchive
  as deliberate.
- Always target `master`; never merge into a feature branch.
- Do not force-push, delete remote branches, or rewrite history.
- If the working tree is dirty, stop and ask before checking out `master`.
- Report clearly what was synced, archived, merged, or tagged.
