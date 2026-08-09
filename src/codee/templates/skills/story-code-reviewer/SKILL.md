---
name: story-code-reviewer
description: Review a merge request for one subtask in an Issue Tracker story.
disable-model-invocation: true
x-codee-trigger: issue
x-codee-issue-status: ['[AI] CR Needed']
x-codee-issue-type: story
argument-hint: <STORY_ID>
---

# Story Code Reviewer

## Workflow

1. Read the story, acceptance criteria, attachments, comments, and linked subtasks in the Issue Tracker.
2. Read `story-spec/{STORY_ID}/README.md` when it exists.
3. Select one subtask with a merge request awaiting review.
4. Read the project instructions, complete diff, affected files, relevant callers, tests, pipeline results, and unresolved review comments.
5. Review security, correctness, requirement coverage, performance, tests, error handling, and maintainability.
6. Post line comments for specific defects and a findings-first merge request review.
7. Add a concise Issue Tracker comment with the verdict, blocking findings, and verification performed.

## Guidelines

- Review one subtask per invocation.
- Distinguish blocking defects from optional suggestions.
- Do not approve changes with unresolved blocking findings.
- Do not approve a failing pipeline unless the failure is clearly unrelated.
- Make feedback actionable by naming the observed behavior, expected behavior, and affected location.