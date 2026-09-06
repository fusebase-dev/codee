---
name: story-code-reviewer
description: Review a pull request for one subtask in an Issue Tracker story.
disable-model-invocation: true
x-codee-trigger: issue
x-codee-issue-status: ['AI Ready for CR']
x-codee-issue-type: story
argument-hint: <STORY_ID>
---

# Story Code Reviewer

## Workflow

1. Read the story, acceptance criteria, attachments, comments, and linked subtasks in the Issue Tracker.
2. Read `story-spec/{STORY_ID}/README.md` when it exists.
3. Select one subtask with a pull request awaiting review.
4. Read the project instructions, complete diff, affected files, relevant callers, tests, pipeline results, and unresolved review comments.
5. Review security, correctness, requirement coverage, performance, tests, error handling, and maintainability.
6. Post line comments for specific defects and a findings-first pull request review.
7. Add a concise Issue Tracker comment with the verdict, blocking findings, and verification performed.
8. Move the story to `AI Ready for security review` when the review passes, or back to `AI Ready for development` when it does not.

## Status Transitions

- Review passed with no blocking findings: move the story to `AI Ready for security review`.
- Blocking findings, or a relevant failing pipeline: move the story to `AI Ready for development` so the developer skill addresses them.
- Subtasks still awaiting review: leave the story in `AI Ready for CR` so the next run reviews the next one.

## Guidelines

- Review one subtask per invocation.
- Distinguish blocking defects from optional suggestions.
- Do not approve changes with unresolved blocking findings.
- Do not approve a failing pipeline unless the failure is clearly unrelated.
- Make feedback actionable by naming the observed behavior, expected behavior, and affected location.
