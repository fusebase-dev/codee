---
name: story-security-reviewer
description: Run a security review of the pull request for one subtask in an Issue Tracker story.
disable-model-invocation: true
x-codee-trigger: issue
x-codee-issue-status: ['AI Ready for security review']
x-codee-issue-type: story
argument-hint: <STORY_ID>
---

# Story Security Reviewer

A security-only pass over work that already passed code review. In a general review, security findings compete with naming, tests, and performance and usually lose. This run looks at nothing else.

## Workflow

1. Read the story, acceptance criteria, attachments, comments, and linked subtasks in the Issue Tracker.
2. Read `story-spec/{STORY_ID}/README.md` when it exists.
3. Select one subtask whose pull request has passed code review.
4. Read the project instructions, complete diff, affected files, and the trust boundaries the change touches.
5. Review authentication, authorization, input validation, injection, secrets and credential handling, sensitive data exposure, unsafe deserialization, path and command handling, dependency risk, and access to third-party services.
6. For each finding, record the vulnerable location, the attack it enables, and the concrete fix.
7. Post line comments for specific vulnerabilities and a findings-first pull request review.
8. Add a concise Issue Tracker comment with the verdict, blocking findings, and what was inspected.
9. Move the story to `AI Ready for QA` when the review passes, or back to `AI Ready for development` when it does not.

## Status Transitions

- No exploitable findings: move the story to `AI Ready for QA`.
- Any exploitable finding: move the story to `AI Ready for development` with the location and the fix.
- Subtasks still awaiting a security pass: leave the story in `AI Ready for security review` so the next run reviews the next one.

## Guidelines

- Review one subtask per invocation.
- Report only findings reachable in this codebase, and name the path that reaches them.
- Separate exploitable vulnerabilities from hardening suggestions.
- Do not modify the implementation or merge the pull request.
