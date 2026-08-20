---
name: auto-commit-agent
description: Guarded autonomous commit-and-push workflow for Git feature branches. Use when the user invokes `$auto-commit-agent on`, `$auto-commit-agent checkpoint`, `$auto-commit-agent off`, or `$auto-commit-agent status`, or asks Cline to make incremental local commits and normal pushes while working in a non-protected branch.
allowed-tools: Bash(git:*), Bash(powershell:*)
license: MIT
compatibility: Requires Git and PowerShell.
metadata:
  author: auto-commit-agent
  version: "1.0"
---

# Auto Commit Agent

Guarded autonomous commit-and-push workflow for Git feature branches. This skill helps maintain clean, incremental commits on feature branches with safety checks and conventional commit messages.

## Command Modes

- `on`: Enable guarded checkpoint behavior for the current task. Continue making coherent local commits and normal pushes at natural checkpoints until the user turns it off or the task ends.
- `checkpoint`: Evaluate the current repo state once, then commit and push only if the changes are coherent and safe.
- `off`: Disable autonomous checkpoint behavior. Do not make new commits or pushes unless the user explicitly requests them later.
- `status`: Report repo root, branch, upstream, protection result, working tree state, staged state, and whether auto mode is on.

**Important:** Keep auto mode as conversational state only. Do not write persistent config unless the user explicitly asks for it.

## State Management

Track auto mode state in conversation only. When the user invokes commands:

- **`on`**: Set internal flag `auto-commit-enabled = true`. Announce: "Auto-commit mode enabled. I'll make checkpoint commits at natural stopping points."
- **`off`**: Set internal flag `auto-commit-enabled = false`. Announce: "Auto-commit mode disabled."
- **`status`**: Report current state without changing it.
- **`checkpoint`**: Perform one-time checkpoint without changing auto mode state.


## Safety Gate

Before every commit or push, run the following checks:

### 1. Detect Repository Root

```bash
git rev-parse --show-toplevel
```

If this fails, refuse to proceed. Report: "Not in a Git repository."

### 2. Detect Current Branch

```bash
git branch --show-current
```

**Refuse on detached HEAD.** If empty, report: "Cannot commit in detached HEAD state. Check out a branch first."

### 3. Branch Protection Check

**Refuse commits or pushes to protected branches:**
- `master`
- `main`
- `release/*`
- `hotfix/*`
- `prod/*`

If current branch matches any protected pattern, report: "Refusing to commit to protected branch '<branch>'. Switch to a feature branch."

### 4. Inspect Repository State

```bash
git status --short --branch
```

Parse the output to understand:
- Current branch and upstream tracking
- Staged changes (lines starting with `A`, `M`, `D`, `R`, `C`)
- Unstaged changes (lines starting with ` M`, ` D`, `??`)

### 5. Inspect Changes

Check unstaged and staged diffs:

```bash
# Unstaged changes
git diff --stat --
git diff --check --

# Staged changes
git diff --cached --stat --
git diff --cached --check --
```

Review the file paths and content to determine:
- Are changes related to the active task?
- Are there whitespace or merge conflict issues? (`git diff --check`)
- Is the diff coherent and reviewable?
## Staging Rules

**Stage only files clearly related to the active task.**

### Preferred Approach: Path-Specific Staging

```bash
git add -- path/to/file1 path/to/file2 path/to/file3
```

### Never Stage:
- Unrelated user changes
- Ignored files
- Likely secrets, credentials, or keystores
- Local machine config
- Build outputs or dependency caches
- IDE workspace state
- Files matching these patterns:
  - `.env`, `.env.*`
  - `*.jks`, `*.keystore`, `*.p12`, `*.pem`, `*.key`
## Commit Workflow

**Commit only when the staged diff is a coherent checkpoint.**

### 1. Run Practical Checks

Before committing, run checks appropriate to the touched code and repo conventions:
- Targeted unit tests
- Build checks
- Linting
- OpenSpec validation
- Source checks

If checks are impractical, state why before committing.

### 2. Re-verify Before Commit

Immediately before `git commit`, re-run:

```bash
git status --short --branch
git diff --cached --stat --
git diff --cached --check --
```

Confirm:
- Only intended files are staged
- No whitespace or merge issues
- Diff is coherent

## Push Workflow

**Push automatically only after a safe local checkpoint commit.**

### Push Conditions (ALL must be true)

- ✅ The branch passed the Safety Gate
- ✅ The branch is the intended task branch (not main/master)
- ✅ The push target is `origin/<current-branch>`
- ✅ The push is a normal non-force push
- ❌ No tags are pushed
- ❌ No local or remote branches are deleted

### Push Commands

**If no upstream exists:**
```bash
git push -u origin <current-branch>
```

**If upstream exists and matches `origin/<current-branch>`:**
```bash
git push
```

### Refuse to Push If:
- Upstream points anywhere other than `origin/<current-branch>` → Report: "Upstream is <upstream>, not origin/<branch>. Refusing to push."
## Reporting

### For Checkpoint and Auto-Mode Checkpoints

Report the following after each checkpoint:

```
## Auto-Commit Checkpoint

**Branch:** <branch-name>
**Auto Mode:** <enabled/disabled>

### Staged Files
- <file1> - <reason it's related to task>
- <file2> - <reason it's related to task>

### Checks Run
- <test/build/lint command>: ✅/❌ <result>

### Commit
- **Hash:** <commit-hash or "No commit made">
- **Message:** <commit message or reason skipped>
- **Skipped because:** <if applicable>

### Push
- **Command:** `<push command>` or "No push made"
- **Result:** <success/failure> or reason skipped
- **Skipped because:** <if applicable>
```

### For Status Command

Report without committing or pushing:

```
## Auto-Commit Status

**Repository:** <repo-root>
**Branch:** <branch-name>
**Upstream:** <upstream or "none">
**Branch Protection:** ✅ Safe / ❌ Protected (<reason>)

### Working Tree
## Example Workflows

### Example 1: Auto Mode Checkpoint

User: "I just finished implementing the sync retry logic"
Cline (auto mode enabled):
1. Detects changes in `src/sync/retry.ts` and `src/sync/__tests__/retry.test.ts`
2. Runs `npm test -- src/sync/__tests__/retry.test.ts` → ✅ Passed
3. Stages: `git add -- src/sync/retry.ts src/sync/__tests__/retry.test.ts`
4. Commits: `git commit -m "feat: add retry logic to sync operations"`
5. Pushes: `git push` → ✅ Success
6. Reports checkpoint summary

### Example 2: Manual Checkpoint

User: "$auto-commit-agent checkpoint"
Cline:
1. Evaluates current repo state
2. Runs safety gate checks
3. If safe and coherent: commits and pushes
4. If unsafe: reports why and skips
5. Reports results

### Example 3: Status Check

User: "$auto-commit-agent status"
Cline:
1. Runs all safety checks
2. Reports repository state
3. Does NOT commit or push
4. Suggests next allowed action

## Guardrails

- **Never** commit to `master`, `main`, or protected branches
- **Never** force-push or delete branches
- **Never** stage unrelated changes
- **Never** stage secrets, credentials, or build artifacts
- **Never** amend or rewrite history automatically
- **Always** review diff content before staging
- **Always** run relevant checks before committing
- **Always** ask when unsure about staging decisions
- **Always** report clearly what was done and why
- **Keep** auto mode as conversational state only

## Troubleshooting

### "Not in a Git repository"
Ensure you're working in a Git repository. Run `git status` to verify.

### "Detached HEAD state"
Check out a feature branch before using auto-commit: `git checkout -b feature/my-feature`

### "Protected branch"
Switch to a feature branch: `git checkout -b feature/my-feature`

### "Upstream mismatch"
Set upstream correctly: `git branch --set-upstream-to=origin/<branch> <branch>`

### "No changes to commit"
Working tree is clean. Make changes to code before running checkpoint.

### "Changes are ambiguous"
Review changes with `git status` and `git diff`. If changes span multiple unrelated features, consider creating separate commits manually or ask for guidance.

<git status output>

### Staged Changes
<git diff --cached --stat output or "None">

### Auto Mode
<Enabled/Disabled>

### Next Allowed Action
<What can be done next based on current state>
```

- Force-push requested → Refuse
- Push includes tags → Refuse
- Push deletes branches → Refuse
- Target is `master` or `main` → Refuse

### 3. Create Commit

```bash
git commit -m "<conventional-commit-message>"
```

### 4. Commit Message Format

Use concise conventional commit messages in **lowercase imperative form**:

- `feat: add sync diagnostics`
- `fix: handle monitor retry cancellation`
- `test: cover sync status failure`
- `docs: update openspec task notes`
- `refactor: simplify error handling`
- `chore: update dependencies`

**Format:** `<type>: <lowercase imperative description>`

### 5. Post-Commit

**Do NOT amend, squash, or rebase automatically.** Report the commit hash and message.

  - `local.properties`
  - `.gradle/`, `build/`, `out/`, `dist/`, `node_modules/`
  - `.idea/workspace.xml`

### When in Doubt

If changes appear unrelated to the current task or are ambiguous, **ask the user before staging**. Skip committing when changes are mixed, ambiguous, or unsafe.


**Helper Script (Optional):**

If `scripts/Test-AutoCommitSafety.ps1` exists, use it for deterministic checks:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/Test-AutoCommitSafety.ps1 -RepoRoot <repo-root>
```

The helper only reports; you must still review file contents and decide whether changes are related to the active task.
