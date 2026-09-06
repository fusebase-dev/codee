---
name: task-code-reviewer
description: Review the pull request for a standalone Issue Tracker task.
disable-model-invocation: true
x-codee-trigger: issue
x-codee-issue-status: ['AI Ready for CR']
x-codee-issue-type: task
argument-hint: <TASK_ID>
---

# Task Code Reviewer

## Workflow

1. Read the task, acceptance criteria, attachments, and comments in the Issue Tracker.
2. Locate the pull request awaiting review.
3. Read the project instructions, complete diff, affected files, relevant callers, tests, pipeline results, and unresolved review comments.
4. Review security, correctness, requirement coverage, performance, tests, error handling, and maintainability.
5. Post line comments for specific defects and a findings-first pull request review.
6. Add a concise Issue Tracker comment with the verdict, blocking findings, and verification performed.
7. Move the task to `AI Ready for security review` when the review passes, or back to `AI Ready for development` when it does not.

## Status Transitions

- Review passed with no blocking findings: move the task to `AI Ready for security review`.
- Blocking findings, or a relevant failing pipeline: move the task to `AI Ready for development` so the developer skill addresses them.

## Guidelines

- Distinguish blocking defects from optional suggestions.
- Do not approve changes with unresolved blocking findings.
- Do not approve a failing pipeline unless the failure is clearly unrelated.
- Make feedback actionable by naming the observed behavior, expected behavior, and affected location.
