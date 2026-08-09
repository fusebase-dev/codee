---
name: story-planner
description: Decompose an Issue Tracker story into actionable subtasks and supporting documentation.
disable-model-invocation: true
x-codee-trigger: issue
x-codee-issue-status: ['[AI] Decomposition Needed']
x-codee-issue-type: story
argument-hint: <STORY_ID>
---

# Story Planner

Decompose an Issue Tracker story into actionable subtasks with supporting documentation.

## Workflow

1. Read the story, acceptance criteria, attachments, comments, and linked issues in the Issue Tracker.
2. Inspect relevant project instructions and source code to understand the current behavior.
3. Ask focused questions when requirements, constraints, or expected behavior are unclear. Record the questions in the Issue Tracker and stop until they are answered.
4. Create a dependency-ordered plan of small, independently implementable subtasks.
5. Give each subtask a clear title, technical description, acceptance criteria, dependencies, and estimate when useful.
6. Ensure the subtasks cover every story acceptance criterion, including testing and monitoring work where needed.
7. Create or update the subtasks in the Issue Tracker using its supported rich-text format.
8. Write the specification to `story-spec/{STORY_ID}/README.md` using [assets/readme-template.md](assets/readme-template.md). Add `architecture.md` only when architecture or data flow needs explanation.
9. Post a concise Issue Tracker comment summarizing the plan and linking the subtasks and specification.

## Feedback Rounds

When a plan already exists, re-read new comments and edit the existing subtasks and specification. Create new subtasks only for newly identified work, and avoid duplicates.

## Guidelines

- Prefer subtasks that can be implemented and reviewed independently.
- Order subtasks by dependency.
- Keep technical details concrete and acceptance criteria verifiable.