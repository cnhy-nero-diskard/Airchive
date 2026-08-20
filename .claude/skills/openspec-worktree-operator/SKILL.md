---
name: openspec-worktree-operator
description: Create and manage isolated Git worktrees for autonomous OpenSpec implementation. Use when the user invokes this skill, starts a branch worktree, applies an OpenSpec proposal in an isolated worktree, asks for worktree status, a handoff prompt, draft PR creation, or cleanup of an OpenSpec implementation worktree.
license: MIT
metadata:
  author: openspec
  version: "1.0"
---

# OpenSpec Worktree Operator Skill

## Overview
This skill manages isolated Git worktrees for applying OpenSpec proposals. All implementation
work is isolated to the worktree, protecting the original repository state.

## Supported Commands

### 1. Create & Apply a Worktree
**Syntax:**
- `start branch <branch> apply proposal <proposal>`
- `start branch <branch> from current apply proposal <proposal>`
- `start branch <branch> from <base>`

**Flow:**
1. Run planning gate (detect repo-root, current branch, worktree list, status)
2. Create isolated worktree at sibling path: `<parent>/<repo-name>-<sanitized-branch>`
3. If proposal specified: apply OpenSpec changes
4. Report worktree path, branch, base clearly

### 2. Status Check
**Syntax:** `status`

**Actions:**
- Detect repo root (`git rev-parse --show-toplevel`)
- Show current branch (`git branch --show-current`)
- List worktrees (`git worktree list --porcelain`)
- Show status (`git status --short --branch`)
- Do NOT mutate anything

### 3. Handoff Prompt
**Syntax:** `handoff branch <branch>`

**Output format:**
```
Open <worktree-path>. Use $openspec-apply-change to apply proposal <proposal>
on branch <branch>. Stay inside this worktree. Use $auto-commit-agent checkpoint
after coherent increments if available. Do not switch branches in the original repo.
```

### 4. Draft PR Creation
**Syntax:**
- `pr branch <branch>`
- `create pr branch <branch>`
- `ready pr branch <branch>`

**Before creating:**
1. Verify branch is NOT master/main
2. Verify branch is pushed to `origin/<branch>`
3. Inspect PR templates in `.github/`
4. Summarize changes from commits and diff
5. Include proposal name if known
6. Include actual validation results only
7. Create draft PR (unless user explicitly says ready-for-review)
8. Report PR URL, title, base, head, draft status

### 5. Cleanup Worktree
**Syntax:**
- `cleanup branch <branch>`
- `cleanup branch <branch> force`

**Flow:**
1. Fetch origin for fresh remote-tracking refs
2. Verify branch is merged into origin/master or origin/main
3. Check `git status --short --branch` in worktree
4. **Refuse** if unmerged or dirty (unless force)
5. Remove worktree: `git worktree remove <worktree-path>`
6. Delete local branch: `git branch -d <branch>`
7. Refresh remote base refs with `git fetch`

## Planning Gate (Before Any Mutation)

Before creating, inspecting, or removing a worktree, always:

1. **Detect repo-root:**
   ```bash
   git rev-parse --show-toplevel
   ```
2. **Detect current branch:**
   ```bash
   git branch --show-current
   ```
3. **List active worktrees:**
   ```bash
   git worktree list --porcelain
   ```
4. **Check repo status:**
   ```bash
   git status --short --branch
   ```
5. **Verify safety:**
   - Is `<branch>` already checked out elsewhere? (REFUSE if yes)
   - Does `<worktree-path>` already exist? (REFUSE if yes)
   - Can base be verified? (REFUSE if ambiguous)

Never switch branches in the original worktree.

## Worktree Creation

**Path derivation:**
```
<parent-of-repo-root>/<repo-name>-<sanitized-branch>
```
Sanitize `<branch>` for folder names: replace `/`, `\`, `:`, whitespace with `-`.

**Fetch before creating:**
```bash
git fetch origin
```

**Creation paths (choose exactly one):**

New local branch:
```bash
git worktree add <worktree-path> -b <branch> <base>
```
Existing local branch (not checked out elsewhere):
```bash
git worktree add <worktree-path> <branch>
```
Existing origin branch (when clearly intended):
```bash
git worktree add <worktree-path> -b <branch> origin/<branch>
```

## Provision Local Environment

After creating the worktree, provision safe local files (e.g., `local.properties`, `android/local.properties`):

1. Copy from original repo only if safe:
   - File must be git-ignored or untracked
   - Never copy `.env`, keystores, credentials, signing files
2. Verify ignored status:
   ```bash
   git -C <worktree-path> status --short --ignored
   ```
3. SDK provisioning (Android projects):
   - If `local.properties` exists in original and repo is Android project, preserve `sdk.dir` value
   - If SDK path unavailable, set `ANDROID_HOME` environment variable
   - Report outcome clearly (copied/created/provided via env/failed)

Report provisioning status before applying changes.

## OpenSpec Apply

Inside `<worktree-path>`:

1. Inspect proposal: `openspec/changes/<proposal>`
2. Use available tools:
   - Prefer the `openspec-apply-change` skill if available
   - Else follow repo-local OpenSpec files directly
   - Only claim validation passed if actually run in the worktree
3. Scope changes:
   - Keep to proposal only
   - Exclude: local machine files, secrets, build outputs, ignored files

## Auto-Commit Coordination

- Invoke the auto-commit agent only inside `<worktree-path>`
- Let it handle commit/push safeguards
- Never push unless preparing a PR
- Forbidden branches: `master`, `main`, `release/*`, `hotfix/*`, `prod/*`

## Hard Safety Rules

- Never mutate another active worktree
- Never switch branches in the original repo for task execution
- Never overwrite an existing folder
- Never delete unmerged work unless explicitly forced
- Never force-push, push tags, auto-rebase, auto-amend, or delete remote branches
- Treat `.gitignore` as ignore policy, not proof a file is safe to commit
