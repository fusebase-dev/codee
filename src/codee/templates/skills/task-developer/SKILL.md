---
name: task-developer
description: Implement a standalone Issue Tracker task and submit a merge request.
disable-model-invocation: true
x-codee-trigger: issue
x-codee-issue-status: ['[AI] Ready for development', '[AI] In Progress', Reopened]
x-codee-issue-type: task
argument-hint: <TASK_ID> [continue]
---

# Task Developer

## Workflow

1. Read the task, acceptance criteria, attachments, and comments in the Issue Tracker. With `continue`, also read new merge request feedback.
2. Read the affected projects' `CLAUDE.md`, `AGENTS.md`, and relevant local skills.
3. For bugs and regressions, reproduce the problem and identify the root cause before editing code. Report evidence when the issue cannot be reproduced or the root cause remains unclear.
4. Create or reuse a dedicated worktree and branch according to repository conventions.
5. Install dependencies and implement the smallest complete change that satisfies the acceptance criteria.
6. Run focused tests, the project build, and browser validation for UI work.
7. Review the complete diff for correctness, security, regressions, and missing tests.
8. Commit, push, and create or update the merge request. Do not merge it unless explicitly requested.
9. Add a concise Issue Tracker comment describing the change, validation, merge request, and any remaining risk.

For UI work, include screenshots or video evidence when appropriate. On continued work, always refresh the Issue Tracker item and merge request before acting.