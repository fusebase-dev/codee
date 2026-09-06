---
name: task-developer
description: Implement a standalone Issue Tracker task and submit a pull request.
disable-model-invocation: true
x-codee-trigger: issue
x-codee-issue-status: ['AI Ready for development', 'AI In Progress']
x-codee-issue-type: task
argument-hint: <TASK_ID> [continue]
---

# Task Developer

## Workflow

1. Read the task, acceptance criteria, attachments, and comments in the Issue Tracker. With `continue`, also read new pull request feedback.
2. Read the affected projects' `CLAUDE.md`, `AGENTS.md`, and relevant local skills.
3. Move the task to `AI In Progress` before doing any work, so a task already being worked on is not picked up as new.
4. For bugs and regressions, reproduce the problem and identify the root cause before editing code. Report evidence when the issue cannot be reproduced or the root cause remains unclear.
5. Create or reuse a dedicated worktree and branch according to repository conventions.
6. Install dependencies and implement the smallest complete change that satisfies the acceptance criteria.
7. Run focused tests, the project build, and browser validation for UI work.
8. Review the complete diff for correctness, security, regressions, and missing tests.
9. Commit, push, and create or update the pull request. Do not merge it unless explicitly requested.
10. Add a concise Issue Tracker comment describing the change, validation, pull request, and any remaining risk.
11. Move the task to `AI Ready for CR` once the change is complete and its pull request is open.

## Status Transitions

- Work started: move the task to `AI In Progress`.
- Change complete and pushed: move the task to `AI Ready for CR`.
- Work unfinished, or the problem could not be reproduced: leave the task in `AI In Progress` and comment with what is missing.

For UI work, include screenshots or video evidence when appropriate. On continued work, always refresh the Issue Tracker item and pull request before acting.
