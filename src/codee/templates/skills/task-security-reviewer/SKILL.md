---
name: task-security-reviewer
description: Run a security review of the pull request for a standalone Issue Tracker task.
disable-model-invocation: true
x-codee-trigger: issue
x-codee-issue-status: ['AI Ready for security review']
x-codee-issue-type: task
argument-hint: <TASK_ID>
---

# Task Security Reviewer

A security-only pass over work that already passed code review. In a general review, security findings compete with naming, tests, and performance and usually lose. This run looks at nothing else.

## Workflow

1. Read the task, acceptance criteria, attachments, and comments in the Issue Tracker.
2. Locate the pull request that has passed code review.
3. Read the project instructions, complete diff, affected files, and the trust boundaries the change touches.
4. Review authentication, authorization, input validation, injection, secrets and credential handling, sensitive data exposure, unsafe deserialization, path and command handling, dependency risk, and access to third-party services.
5. For each finding, record the vulnerable location, the attack it enables, and the concrete fix.
6. Post line comments for specific vulnerabilities and a findings-first pull request review.
7. Add a concise Issue Tracker comment with the verdict, blocking findings, and what was inspected.
8. Move the task to `AI Ready for QA` when the review passes, or back to `AI Ready for development` when it does not.

## Status Transitions

- No exploitable findings: move the task to `AI Ready for QA`.
- Any exploitable finding: move the task to `AI Ready for development` with the location and the fix.

## Guidelines

- Report only findings reachable in this codebase, and name the path that reaches them.
- Separate exploitable vulnerabilities from hardening suggestions.
- Do not modify the implementation or merge the pull request.
