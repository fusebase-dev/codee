---
name: story-qa
description: Validate one subtask from an Issue Tracker story against its acceptance criteria.
disable-model-invocation: true
x-codee-trigger: issue
x-codee-issue-status: ['[AI] Ready for QA']
x-codee-issue-type: story
argument-hint: <STORY_ID>
---

# Story QA

Validate one subtask per invocation. QA is read-only: do not modify the implementation or merge its merge request.

## Workflow

1. Read the story, acceptance criteria, attachments, comments, subtasks, and `story-spec/{STORY_ID}/README.md` when present.
2. Select one delivered subtask that needs verification and locate its merge request and test environment.
3. Read project instructions and relevant implementation details.
4. Create a QA plan mapping every applicable acceptance criterion to a scenario, environment, method, and expected result.
5. Execute each scenario and record the actual result.
6. For UI work, test important paths, edge cases, responsive layout, keyboard behavior, and browser console output. Capture screenshots for static evidence and video for dynamic interactions.
7. For service work, test success, validation, authorization, and edge cases. Record requests, responses, HTTP codes, and relevant side effects.
8. Mark each criterion pass or fail and separate blocking defects from non-blocking observations.
9. Post a concise Issue Tracker report with the results, evidence, environment, and actionable reproduction steps for failures.
10. Update the story specification with a short QA summary when useful.

Create a separate Issue Tracker item for unrelated defects instead of expanding the subtask.