---
name: story-developer
description: Implement one subtask from an Issue Tracker story and submit a pull request.
disable-model-invocation: true
x-codee-trigger: issue
x-codee-issue-status: ['AI Ready for development', 'AI In Progress']
x-codee-issue-type: story
argument-hint: <STORY_ID>
---

# Story Developer

## Workflow

1. Read the story, acceptance criteria, attachments, comments, and subtasks in the Issue Tracker.
2. Read `story-spec/{STORY_ID}/README.md` and the affected projects' `CLAUDE.md`, `AGENTS.md`, and relevant local skills.
3. Move the story to `AI In Progress` before doing any work, so a story already being worked on is not picked up as new.
4. Select one open subtask and inspect any related branches, pull requests, and review comments.
5. For bugs and regressions, reproduce the problem and identify the root cause before editing code.
6. Create or reuse a dedicated worktree and branch according to repository conventions.
7. Install dependencies, implement the smallest complete change, and update the story specification when useful.
8. Run focused tests, the project build, and any required browser checks.
9. Review the complete diff for correctness, security, regressions, and missing tests.
10. Commit, push, and create or update the pull request. Do not merge it unless explicitly requested.
11. Add a concise Issue Tracker comment describing the change, validation, pull request, and any remaining risk.
12. Move the story to `AI Ready for CR` once every open subtask is implemented and its pull request is open.

## Status Transitions

- Work started: move the story to `AI In Progress`.
- All subtasks implemented and pushed: move the story to `AI Ready for CR`.
- Open subtasks remain: leave the story in `AI In Progress` so the next run continues the work.

For UI work, include screenshots or video evidence when appropriate.
