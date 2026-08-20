---
name: autocmt
description: Guarded autonomous commit-and-push workflow for Git feature branches. Use when the user invokes `$autocmt on`, `$autocmt checkpoint`, `$autocmt off`, or `$autocmt status`, or asks Cline to make incremental local commits and normal pushes while working in a non-protected branch.
allowed-tools: Bash(git:*)
license: MIT
metadata:
  author: cline
  version: "1.0"
---

Autonomous commit-and-push agent that groups uncommitted/unstaged changes into meaningful, coherent increments and pushes them to the current branch.

## Commands

| Command | Action |
|---|---|
| `$autocmt on` | Enable auto-commit mode. From now on, after each completed work unit, automatically commit and push changes in meaningful chunks. |
| `$autocmt checkpoint` | Run a one-time commit-and-push of all current uncommitted changes, grouped into meaningful chunks. |
| `$autocmt off` | Disable auto-commit mode. Stop automatically committing/pushing. |
| `$autocmt status` | Show current mode, branch, and pending uncommitted changes summary. |

## Core Principles

1. **Never commit everything at once.** Group changes into coherent, self-contained increments.
2. **One-liner commit messages** in the format: `committype: (message here)`
3. **Only push to non-protected branches.** Never push to `main`, `master`, or any branch listed in the repo's protected branches.
4. **Never commit secrets, credentials, or generated artifacts** (build outputs, local configs, etc.) — respect `.gitignore`.
5. **Never stage or commit files unrelated to the current chunk.**

## Commit Types

Use the most specific type that fits the chunk:

| Type | When to use |
|---|---|
| `feat` | New feature or user-facing capability |
| `fix` | Bug fix |
| `refactor` | Code restructuring without behavior change |
| `docs` | Documentation-only changes |
| `style` | Formatting, whitespace, lint fixes (no logic change) |
| `test` | Adding or updating tests |
| `chore` | Maintenance, tooling, dependency updates |
| `build` | Build system or CI configuration changes |
| `perf` | Performance improvements |
| `revert` | Reverting a previous commit |

## Workflow

### 1. Preflight Checks

Before doing anything, run:

```bash
git status --porcelain
git branch --show-current
git remote -v
```

- **If on a protected branch** (`main`, `master`, or a branch the user has designated as protected): STOP. Report that auto-commit is blocked on protected branch `<branch>`. Suggest creating/checking out a feature branch first.
- **If no remote is configured**: commit locally only, do not attempt to push. Report that no remote exists.
- **If there are no changes**: report "No uncommitted changes to commit." and stop.

### 2. Analyze Changes

Run `git status --porcelain` and `git diff --stat` to understand the scope of changes.

Group changes into **coherent chunks** by asking:

- **What is the logical unit of work here?** (e.g., a feature, a bug fix, a refactor)
- **Which files belong together?** Files changed for the same purpose belong in the same commit.
- **Are there independent changes?** If a file or group of files addresses a different concern, split them into separate commits.

**Chunking heuristics:**

- **New feature files** (e.g., a new screen, new module, new API endpoint) → one `feat` commit
- **Bug fix + its test** → one `fix` commit (or `fix` + `test` if the test is substantial)
- **Renames/moves** → one `refactor` commit
- **Dependency version bumps** → one `chore` or `build` commit
- **Docs changes** → one `docs` commit
- **Formatting-only changes** → one `style` commit
- **Multiple unrelated files** → separate commits per concern

**Order commits** from most foundational to most dependent (e.g., refactor before feature, chore before feat).

### 3. Commit Each Chunk

For each chunk, in order:

1. **Stage only the files in this chunk:**
   ```bash
   git add <file1> <file2> ...
   ```
   Never use `git add -A` or `git add .` unless ALL changes belong to a single coherent chunk.

2. **Verify what's staged:**
   ```bash
   git diff --cached --stat
   ```

3. **Create the commit** with a one-line message:
   ```bash
   git commit -m "feat: (add collection detail screen)"
   ```
   Message format: `committype: (message here)` — lowercase type, colon, space, parenthesized message. The message should be a concise imperative phrase (e.g., "add", "fix", "update").

4. **Confirm the commit succeeded** before moving to the next chunk.

### 4. Push

After all chunks are committed:

```bash
git push
```

- Push to the current branch's upstream. If no upstream is set, set it:
  ```bash
  git push -u origin <current-branch>
  ```
- If the push is rejected (e.g., remote has new commits), do NOT force-push. Report the conflict and ask the user how to proceed (pull/rebase/merge).

### 5. Report

After pushing, report:

```
## Auto-commit complete

**Branch:** <branch>
**Commits created:** N

- `feat: (add collection detail screen)` — 3 files
- `fix: (fix live status detection)` — 2 files
- `chore: (bump gradle dependencies)` — 1 file

**Pushed:** ✓ / ✗ (reason)
```

## Auto-commit Mode (`on` / `off`)

When auto-commit mode is **on**:

- After completing each meaningful work unit (e.g., finishing a task, implementing a feature, fixing a bug), automatically run the checkpoint workflow.
- Do NOT interrupt the user mid-task to commit. Wait for a natural stopping point.
- If the user is mid-edit on a file (e.g., a file is partially modified and clearly incomplete), do NOT commit that file. Skip it and note it in the report.
- Keep the mode state in the conversation context. If the conversation is reset, default to `off` and inform the user.

## Guardrails

- **Protected branches:** Never commit or push to `main`, `master`, or user-designated protected branches. If on one, stop and suggest a feature branch.
- **No force-push:** Never use `git push --force` or `--force-with-lease` unless explicitly requested by the user.
- **No secrets:** Never commit files containing credentials, API keys, tokens, or `.env` files. If detected, exclude them and warn the user.
- **No generated artifacts:** Never commit build outputs (`build/`, `.gradle/`, `node_modules/`, etc.), IDE configs (`.idea/`, `.vscode/`), or local properties. Respect `.gitignore`.
- **No unrelated files:** Each commit must contain only files belonging to its chunk.
- **No empty commits:** Never create a commit with no staged changes.
- **No amend:** Never use `git commit --amend` on pushed commits.
- **No merge commits:** Do not create merge commits. If a push is rejected, stop and ask.
- **Verify before proceeding:** After each `git add` and `git commit`, confirm the command succeeded before continuing. If a command fails, stop and investigate.
- **Partial/incomplete work:** If a file appears to be mid-edit (syntax errors, obvious incomplete code), exclude it from commits and report it.

## Edge Cases

| Situation | Handling |
|---|---|
| No changes to commit | Report "No uncommitted changes." and stop. |
| Only untracked files | Treat them as a chunk (or chunks) and commit them. |
| Deleted files | Stage deletions with `git add -u <path>` or `git rm <path>`. |
| Renamed files | `git add -A <old-path> <new-path>` to stage the rename. |
| Large number of files | Group by directory/concern; don't create one giant commit. |
| Merge conflicts in progress | STOP. Do not commit. Report that a merge/rebase is in progress. |
| Detached HEAD | STOP. Report that HEAD is detached; suggest checking out a branch. |
| Push rejected | STOP. Report and ask user how to proceed. Never force-push. |
| No remote configured | Commit locally, skip push, report. |
| Protected branch | STOP. Report and suggest a feature branch. |
| File mid-edit / incomplete | Exclude from commits, report it. |
| Binary files | Commit them if they are intentional (e.g., assets), but note them in the report. |
