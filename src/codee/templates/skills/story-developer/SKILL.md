---
name: story-developer
description: Implement one subtask from an Issue Tracker story and submit a merge request.
disable-model-invocation: true
x-codee-trigger: issue
x-codee-issue-status: ['[AI] Ready for development', '[AI] In Progress']
x-codee-issue-type: story
argument-hint: <STORY_ID>
---

# Story Developer

## Workflow

1. Read the story, acceptance criteria, attachments, comments, and subtasks in the Issue Tracker.
2. Read `story-spec/{STORY_ID}/README.md` and the affected projects' `CLAUDE.md`, `AGENTS.md`, and relevant local skills.
3. Select one open subtask and inspect any related branches, merge requests, and review comments.
4. For bugs and regressions, reproduce the problem and identify the root cause before editing code.
5. Create or reuse a dedicated worktree and branch according to repository conventions.
6. Install dependencies, implement the smallest complete change, and update the story specification when useful.
7. Run focused tests, the project build, and any required browser checks.
8. Review the complete diff for correctness, security, regressions, and missing tests.
9. Commit, push, and create or update the merge request. Do not merge it unless explicitly requested.
10. Add a concise Issue Tracker comment describing the change, validation, merge request, and any remaining risk.

For UI work, include screenshots or video evidence when appropriate.