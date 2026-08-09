---
name: cron-research-5xx-errors
description: Investigate frequent 5xx errors from the last 24 hours and report the findings.
disable-model-invocation: true
x-codee-trigger: cron
x-codee-cron: 0 0 * * 2-6
---

# 5xx Error Review

1. Review available gateway, ingress, and application logs for the last 24 hours.
2. Group 5xx responses by root cause or failing endpoint.
3. Investigate the three most frequent groups.
4. For each group, record frequency, impact, evidence, likely cause, and recommended action.
5. Create one Issue Tracker report, or update an existing open issue when it covers the same errors.

Do not make speculative code changes. Clearly separate confirmed findings from hypotheses.