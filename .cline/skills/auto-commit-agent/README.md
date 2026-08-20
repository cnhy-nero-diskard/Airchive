# Auto-Commit-Agent Skill Created

## Files Created

1. `.cline/skills/auto-commit-agent/SKILL.md` (240 lines)
   - Complete skill definition with all workflow rules
   - Formatted for Cline's skill system

2. `scripts/Test-AutoCommitSafety.ps1` (181 lines)
   - PowerShell helper script for deterministic safety checks
   - Validates repo root, branch, protection, dirty state, and upstream

## Skill Features

### Command Modes
- `on` - Enable guarded checkpoint behavior
- `checkpoint` - One-time evaluation and commit/push
- `off` - Disable autonomous behavior
- `status` - Report repository state

### Safety Gate (5 checks)
1. Detect repository root
2. Detect current branch (refuse detached HEAD)
3. Branch protection (master, main, release/*, hotfix/*, prod/*)
4. Inspect repository state
5. Inspect changes (unstaged and staged)

### Key Features
- Path-specific staging only
- Conventional commit messages
- Automatic push with safety conditions
- Detailed reporting
- Example workflows included
- Troubleshooting guide

## Usage

```
$auto-commit-agent on
$auto-commit-agent checkpoint
$auto-commit-agent off
$auto-commit-agent status
```

## Testing

Test the helper script:
```
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/Test-AutoCommitSafety.ps1 -RepoRoot .
```

## Notes

- Auto mode is conversational state only (not persisted)
- Never amends, squashes, or rebases automatically
- Only commits/pushes on feature branches
- Asks user before staging ambiguous changes
