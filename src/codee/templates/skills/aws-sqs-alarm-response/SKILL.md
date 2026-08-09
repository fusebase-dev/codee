---
name: aws-sqs-alarm-response
description: Investigate a production alarm and record the findings in the Issue Tracker.
disable-model-invocation: true
x-codee-trigger: aws-sqs
x-codee-aws-sqs-queue: codee-alarms
---

# Alarm Response

An alarm was triggered with this content:

{CONTENT}

## Workflow

1. Determine which service or user flow is affected.
2. Gather relevant logs, metrics, traces, request data, and screenshots.
3. Identify the likely cause, impact, and any immediate mitigation.
4. Search the Issue Tracker for an existing open issue about the same problem.
5. Update the existing issue, or create a new issue with the evidence, impact, and recommended next steps.

For a canary failure, include screenshots from the failed step and inspect the available network trace. If the alarm reports recovery, add that information to the related issue. Ignore confirmed visual false positives.