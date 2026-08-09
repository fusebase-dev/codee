---
name: task-qa
description: Validate a standalone Issue Tracker task against its acceptance criteria.
disable-model-invocation: true
x-codee-trigger: issue
x-codee-issue-status: ['[AI] Ready for QA']
x-codee-issue-type: task
argument-hint: <TASK_ID>
---

# Task QA Engineer

QA is read-only: do not modify the implementation or merge its merge request.

## Workflow

1. Read the task, acceptance criteria, attachments, and comments in the Issue Tracker.
2. Locate the merge request and test environment, then read relevant project instructions and implementation details.
3. Create a QA plan mapping every acceptance criterion to a scenario, environment, method, and expected result.
4. Execute each scenario and record the actual result.
5. For UI work, test important paths, edge cases, responsive layout, keyboard behavior, and browser console output. Capture screenshots for static evidence and video for dynamic interactions.
6. For service work, test success, validation, authorization, and edge cases. Record requests, responses, HTTP codes, and relevant side effects.
7. Mark each criterion pass or fail and separate blocking defects from non-blocking observations.
8. Post a concise Issue Tracker report with the results, evidence, environment, and actionable reproduction steps for failures.

Create a separate Issue Tracker item for unrelated defects instead of expanding the task.