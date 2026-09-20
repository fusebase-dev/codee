/* The demo's stand-in for AdminService: everything the pages read lives here.
   Shapes follow the real state models (SkillSummary, ActiveJob, RunRecord,
   WorkflowSection) so a page written against one reads the other. */
(function (global) {
  const JIRA = "https://demo-company.atlassian.net/browse/";
  const VIEWER = "https://sessions.codee.dev/s/";
  const minutesAgo = (m) => Date.now() - m * 60000;

  const AGENT_NAMES = {
    claude_code: "Claude Code",
    github_copilot: "Github Copilot",
    codex: "Codex",
  };

  /* ---------------------------------------------------------- dashboard */

  const activeJobs = [
    {
      promptPrefix: "/story-developer ",
      taskKey: "DEMO-4821",
      taskUrl: JIRA + "DEMO-4821",
      message: "/story-developer DEMO-4821",
      startedAt: minutesAgo(18.4),
      agent: "Claude Code",
      model: "claude-opus-5",
      viewerUrl: VIEWER + "0d41c7f2",
    },
    {
      promptPrefix: "/task-code-reviewer ",
      taskKey: "DEMO-4807",
      taskUrl: JIRA + "DEMO-4807",
      message: "/task-code-reviewer DEMO-4807",
      startedAt: minutesAgo(6.2),
      agent: "Codex",
      model: "gpt-5-codex",
      viewerUrl: VIEWER + "b71ea930",
    },
    {
      promptPrefix: "/story-qa ",
      taskKey: "DEMO-4793",
      taskUrl: JIRA + "DEMO-4793",
      message: "/story-qa DEMO-4793",
      startedAt: minutesAgo(3.1),
      agent: "Github Copilot",
      model: "claude-sonnet-5",
      viewerUrl: VIEWER + "4f0ab155",
    },
    {
      promptPrefix: "/task-security-reviewer ",
      taskKey: "DEMO-4788",
      taskUrl: JIRA + "DEMO-4788",
      message: "/task-security-reviewer DEMO-4788",
      startedAt: minutesAgo(1.4),
      agent: "Claude Code",
      model: "claude-sonnet-5",
      viewerUrl: VIEWER + "9c25de71",
    },
  ];

  const claudeAccounts = [
    {
      id: 1,
      label: "denis@example.com",
      subscription: "Max 20x",
      inUse: true,
      needsReconnect: false,
      sessionPercent: 63,
      weeklyPercent: 41,
      sessionResets: "in 2h 10m",
      weeklyResets: "Monday 09:00",
    },
    {
      id: 2,
      label: "codee-agent@example.com",
      subscription: "Max 5x",
      inUse: false,
      needsReconnect: false,
      sessionPercent: 92,
      weeklyPercent: 81,
      sessionResets: "in 46m",
      weeklyResets: "Monday 09:00",
    },
    {
      id: 3,
      label: "qa-bot@example.com",
      subscription: "Pro",
      inUse: false,
      needsReconnect: false,
      sessionPercent: 17,
      weeklyPercent: 28,
      sessionResets: "in 4h 05m",
      weeklyResets: "Tuesday 09:00",
    },
  ];

  const hourlyRuns = [
    2, 1, 0, 0, 1, 3, 4, 6, 9, 7, 5, 8,
    11, 6, 4, 7, 9, 12, 8, 5, 3, 2, 4, 1,
  ].map((runs, i) => ({ hour: String((new Date().getHours() + 1 + i) % 24).padStart(2, "0"), runs }));

  const stats = {
    totalRuns: 1284,
    last24h: hourlyRuns.reduce((sum, point) => sum + point.runs, 0),
  };

  /* --------------------------------------------------------------- runs */

  const runs = [
    ["story-developer", "issue", "succeeded", "", 7, "Implemented DEMO-4821 subtask 3 and opened PR #612.",
      "/story-developer DEMO-4821\n\nSubtask 3/5 — retry the AGIC health probe before failing the rollout.\nBranch: feature/DEMO-4821-agic-probe\nPull request: https://github.com/example-org/platform-api/pull/612"],
    ["task-code-reviewer", "issue", "succeeded", "", 24, "Approved PR #609 with two non-blocking notes.",
      "/task-code-reviewer DEMO-4807\n\nReviewed 8 files, 214 added / 37 removed.\nVerdict: approve. Two non-blocking notes on error wrapping."],
    ["cron-research-5xx-errors", "cron", "succeeded", "", 51, "Filed DEMO-4833: 502s on /auth/api/auth after pod rollout.", ""],
    ["story-qa", "issue", "failed", "Acceptance criterion 4 could not be verified: staging returned 503.", 68,
      "QA run stopped: the staging environment was unreachable.", ""],
    ["task-developer", "issue", "succeeded", "", 96, "Fixed DEMO-4799 and opened PR #607.", ""],
    ["story-planner", "issue", "succeeded", "", 122, "Decomposed DEMO-4821 into 5 subtasks and wrote story-spec/DEMO-4821/README.md.", ""],
    ["aws-sqs-alarm-response", "aws-sqs", "succeeded", "", 149, "Investigated SNAT port exhaustion alarm; commented on DEMO-4812.", ""],
    ["task-security-reviewer", "issue", "succeeded", "", 175, "No findings on PR #604.", ""],
    ["story-code-reviewer", "issue", "failed", "The pull request for subtask 2 has merge conflicts.", 201,
      "Review aborted: branch feature/DEMO-4788-token conflicts with master.", ""],
    ["task-qa", "issue", "succeeded", "", 236, "All 6 acceptance criteria passed on PR #601.", ""],
    ["story-developer", "issue", "succeeded", "", 268, "Implemented DEMO-4780 subtask 1 and opened PR #598.", ""],
    ["weekly-dependency-audit", "cron", "succeeded", "", 301, "3 outdated dependencies reported in DEMO-4770.", ""],
    ["email-bug-intake", "email", "succeeded", "", 338, "Created DEMO-4835 from support@ mail thread.", ""],
    ["task-developer", "issue", "succeeded", "", 372, "Fixed DEMO-4762 and opened PR #594.", ""],
    ["story-security-reviewer", "issue", "succeeded", "", 405, "Two medium findings posted on PR #591.", ""],
    ["task-code-reviewer", "issue", "succeeded", "", 441, "Requested changes on PR #588.", ""],
    ["cron-research-5xx-errors", "cron", "succeeded", "", 478, "No new 5xx classes; updated DEMO-4744.", ""],
    ["story-qa", "issue", "succeeded", "", 512, "Subtask 4 of DEMO-4751 verified.", ""],
    ["task-qa", "issue", "succeeded", "", 549, "PR #585 verified on staging.", ""],
    ["story-planner", "issue", "succeeded", "", 583, "Questions posted on DEMO-4758; moved to AI Decomposition review.", ""],
    ["task-developer", "issue", "succeeded", "", 620, "Fixed DEMO-4740 and opened PR #581.", ""],
    ["story-developer", "issue", "succeeded", "", 664, "Implemented DEMO-4733 subtask 2 and opened PR #579.", ""],
    ["aws-sqs-alarm-response", "aws-sqs", "succeeded", "", 702, "Latency alarm traced to a cold cache; no issue filed.", ""],
    ["task-security-reviewer", "issue", "succeeded", "", 741, "One high finding posted on PR #575.", ""],
    ["story-code-reviewer", "issue", "succeeded", "", 780, "Approved PR #573.", ""],
  ].map(([skillName, triggerType, status, error, minutes, preview, message], index) => ({
    skillName,
    triggerType,
    status,
    error,
    preview,
    message,
    startedAt: new Date(minutesAgo(minutes)).toISOString().replace("T", " ").slice(0, 19),
    viewerUrl: index % 3 === 0 ? VIEWER + (10000 + index).toString(16) : "",
  }));

  /* ------------------------------------------------------------- skills */

  const skillBody = (title, lines) =>
    "# " + title + "\n\n## Workflow\n\n" +
    lines.map((line, i) => i + 1 + ". " + line).join("\n") +
    "\n\n## Reporting\n\nRecord the outcome as an Issue Tracker comment: what was done, what was" +
    " verified, and anything the next run has to pick up.\n";

  const skills = [
    {
      slug: "story-planner",
      name: "story-planner",
      description: "Decompose an Issue Tracker story into actionable subtasks and supporting documentation.",
      type: "issue trigger",
      agent: "Claude Code",
      model: "claude-opus-5",
      issueType: "story",
      issueStatus: "AI Decomposition needed",
      frontmatter: {
        "x-codee-trigger": "issue",
        "x-codee-issue-status": "['AI Decomposition needed']",
        "x-codee-issue-type": "story",
        "argument-hint": "<STORY_ID>",
      },
      body: skillBody("Story Planner", [
        "Read the story, acceptance criteria, attachments, comments, and linked issues in the Issue Tracker.",
        "Inspect relevant project instructions and source code to understand the current behavior.",
        "Ask focused questions when requirements are unclear. Record them in the Issue Tracker and move the story to `AI Decomposition review`, then stop.",
        "Create a dependency-ordered plan of small, independently implementable subtasks.",
        "Write `story-spec/{STORY_ID}/README.md` with the plan, affected components, and open risks.",
        "Create the subtasks in the Issue Tracker and move the story to `AI Decomposition review`. A person checks the plan there and moves the story on to `AI Ready for development`; never move it there yourself.",
      ]),
    },
    {
      slug: "story-developer",
      name: "story-developer",
      description: "Implement one subtask from an Issue Tracker story and submit a pull request.",
      type: "issue trigger",
      agent: "Claude Code",
      model: "claude-opus-5",
      issueType: "story",
      issueStatus: "AI Ready for development, AI In Progress",
      frontmatter: {
        "x-codee-trigger": "issue",
        "x-codee-issue-status": "['AI Ready for development', 'AI In Progress']",
        "x-codee-issue-type": "story",
        "argument-hint": "<STORY_ID>",
      },
      body: skillBody("Story Developer", [
        "Read the story, acceptance criteria, attachments, comments, and subtasks in the Issue Tracker.",
        "Read `story-spec/{STORY_ID}/README.md` and the affected projects' `CLAUDE.md`, `AGENTS.md`, and relevant local skills.",
        "Move the story to `AI In Progress` before doing any work, so a story already being worked on is not picked up as new.",
        "Select one open subtask and inspect any related branches, pull requests, and review comments.",
        "For bugs and regressions, reproduce the problem and identify the root cause before editing code.",
        "Create or reuse a dedicated worktree and branch according to repository conventions.",
        "Implement the smallest complete change that satisfies the acceptance criteria, with tests.",
        "Open a pull request and move the story to `AI Ready for CR`.",
      ]),
    },
    {
      slug: "story-code-reviewer",
      name: "story-code-reviewer",
      description: "Review a pull request for one subtask in an Issue Tracker story.",
      type: "issue trigger",
      agent: "Codex",
      model: "gpt-5-codex",
      issueType: "story",
      issueStatus: "AI Ready for CR",
      frontmatter: {
        "x-codee-trigger": "issue",
        "x-codee-issue-status": "['AI Ready for CR']",
        "x-codee-issue-type": "story",
        "x-codee-agent": "codex",
        "argument-hint": "<STORY_ID>",
      },
      body: skillBody("Story Code Reviewer", [
        "Read the story, acceptance criteria, attachments, comments, and linked subtasks in the Issue Tracker.",
        "Read `story-spec/{STORY_ID}/README.md` when it exists.",
        "Select one subtask with a pull request awaiting review.",
        "Read the project instructions, complete diff, affected files, relevant callers, tests, and pipeline results.",
        "Review security, correctness, requirement coverage, performance, tests, error handling, and maintainability.",
        "Post line comments for specific defects and a findings-first pull request review.",
        "On approval move the story to `AI Ready for security review`; on blocking findings move it back to `AI Ready for development`.",
      ]),
    },
    {
      slug: "story-security-reviewer",
      name: "story-security-reviewer",
      description: "Run a security review of the pull request for one subtask in an Issue Tracker story.",
      type: "issue trigger",
      agent: "Claude Code",
      model: "claude-opus-5",
      issueType: "story",
      issueStatus: "AI Ready for security review",
      frontmatter: {
        "x-codee-trigger": "issue",
        "x-codee-issue-status": "['AI Ready for security review']",
        "x-codee-issue-type": "story",
        "argument-hint": "<STORY_ID>",
      },
      body: skillBody("Story Security Reviewer", [
        "Read the story, acceptance criteria, attachments, comments, and linked subtasks in the Issue Tracker.",
        "Select one subtask whose pull request has passed code review.",
        "Read the project instructions, complete diff, affected files, and the trust boundaries the change touches.",
        "Review authentication, authorization, input validation, injection, secrets handling, and dependency risk.",
        "Post findings with severity and a concrete exploit path; approve only when nothing blocking is left.",
        "On approval move the story to `AI Ready for QA`; on findings move it back to `AI Ready for development`.",
      ]),
    },
    {
      slug: "story-qa",
      name: "story-qa",
      description: "Validate one subtask from an Issue Tracker story against its acceptance criteria.",
      type: "issue trigger",
      agent: "Github Copilot",
      model: "claude-sonnet-5",
      issueType: "story",
      issueStatus: "AI Ready for QA",
      frontmatter: {
        "x-codee-trigger": "issue",
        "x-codee-issue-status": "['AI Ready for QA']",
        "x-codee-issue-type": "story",
        "x-codee-agent": "github_copilot",
        "argument-hint": "<STORY_ID>",
      },
      body: skillBody("Story QA", [
        "Read the story, acceptance criteria, attachments, comments, subtasks, and `story-spec/{STORY_ID}/README.md` when present.",
        "Select one delivered subtask that needs verification and locate its pull request and test environment.",
        "Create a QA plan mapping every applicable acceptance criterion to a scenario, environment, method, and expected result.",
        "Execute each scenario and record the actual result with evidence.",
        "QA is read-only: do not modify the implementation or merge its pull request.",
        "On a pass move the story to `AI Done`; on a failure move it back to `AI Ready for development` with a reproduction.",
      ]),
    },
    {
      slug: "task-developer",
      name: "task-developer",
      description: "Implement a standalone Issue Tracker task and submit a pull request.",
      type: "issue trigger",
      agent: "Claude Code",
      model: "claude-sonnet-5",
      issueType: "task",
      issueStatus: "AI Ready for development, AI In Progress",
      frontmatter: {
        "x-codee-trigger": "issue",
        "x-codee-issue-status": "['AI Ready for development', 'AI In Progress']",
        "x-codee-issue-type": "task",
        "argument-hint": "<TASK_ID> [continue]",
      },
      body: skillBody("Task Developer", [
        "Read the task, acceptance criteria, attachments, and comments in the Issue Tracker.",
        "Read the affected projects' `CLAUDE.md`, `AGENTS.md`, and relevant local skills.",
        "Move the task to `AI In Progress` before doing any work.",
        "For bugs and regressions, reproduce the problem and identify the root cause before editing code.",
        "Create or reuse a dedicated worktree and branch according to repository conventions.",
        "Implement the smallest complete change that satisfies the acceptance criteria.",
        "Open a pull request and move the task to `AI Ready for CR`.",
      ]),
    },
    {
      slug: "task-code-reviewer",
      name: "task-code-reviewer",
      description: "Review the pull request for a standalone Issue Tracker task.",
      type: "issue trigger",
      agent: "Codex",
      model: "gpt-5-codex",
      issueType: "task",
      issueStatus: "AI Ready for CR",
      frontmatter: {
        "x-codee-trigger": "issue",
        "x-codee-issue-status": "['AI Ready for CR']",
        "x-codee-issue-type": "task",
        "x-codee-agent": "codex",
        "argument-hint": "<TASK_ID>",
      },
      body: skillBody("Task Code Reviewer", [
        "Read the task, acceptance criteria, attachments, and comments in the Issue Tracker.",
        "Locate the pull request awaiting review.",
        "Read the project instructions, complete diff, affected files, relevant callers, tests, and pipeline results.",
        "Review security, correctness, requirement coverage, performance, tests, error handling, and maintainability.",
        "Post line comments for specific defects and a findings-first pull request review.",
        "On approval move the task to `AI Ready for security review`; on blocking findings move it back to `AI Ready for development`.",
      ]),
    },
    {
      slug: "task-security-reviewer",
      name: "task-security-reviewer",
      description: "Run a security review of the pull request for a standalone Issue Tracker task.",
      type: "issue trigger",
      agent: "Claude Code",
      model: "claude-sonnet-5",
      issueType: "task",
      issueStatus: "AI Ready for security review",
      frontmatter: {
        "x-codee-trigger": "issue",
        "x-codee-issue-status": "['AI Ready for security review']",
        "x-codee-issue-type": "task",
        "argument-hint": "<TASK_ID>",
      },
      body: skillBody("Task Security Reviewer", [
        "Read the task, acceptance criteria, attachments, and comments in the Issue Tracker.",
        "Locate the pull request that has passed code review.",
        "Read the project instructions, complete diff, affected files, and the trust boundaries the change touches.",
        "Review authentication, authorization, input validation, injection, secrets handling, and dependency risk.",
        "On approval move the task to `AI Ready for QA`; on findings move it back to `AI Ready for development`.",
      ]),
    },
    {
      slug: "task-qa",
      name: "task-qa",
      description: "Validate a standalone Issue Tracker task against its acceptance criteria.",
      type: "issue trigger",
      agent: "Github Copilot",
      model: "claude-sonnet-5",
      issueType: "task",
      issueStatus: "AI Ready for QA",
      frontmatter: {
        "x-codee-trigger": "issue",
        "x-codee-issue-status": "['AI Ready for QA']",
        "x-codee-issue-type": "task",
        "x-codee-agent": "github_copilot",
        "argument-hint": "<TASK_ID>",
      },
      body: skillBody("Task QA Engineer", [
        "Read the task, acceptance criteria, attachments, and comments in the Issue Tracker.",
        "Locate the pull request and test environment, then read relevant project instructions.",
        "Create a QA plan mapping every acceptance criterion to a scenario, environment, method, and expected result.",
        "Execute each scenario and record the actual result.",
        "On a pass move the task to `AI Done`; on a failure move it back to `AI Ready for development`.",
      ]),
    },
    {
      slug: "cron-research-5xx-errors",
      name: "cron-research-5xx-errors",
      description: "Investigate frequent 5xx errors from the last 24 hours and report the findings.",
      type: "cron trigger",
      agent: "Claude Code",
      model: "claude-opus-5",
      cron: "0 0 * * 2-6",
      cronDescription: "At 00:00, Tuesday through Saturday",
      frontmatter: { "x-codee-trigger": "cron", "x-codee-cron": "0 0 * * 2-6" },
      body:
        "# 5xx Error Review\n\n1. Review available gateway, ingress, and application logs for the last 24 hours.\n" +
        "2. Group 5xx responses by root cause or failing endpoint.\n3. Investigate the three most frequent groups.\n" +
        "4. For each group, record frequency, impact, evidence, likely cause, and recommended action.\n" +
        "5. Create one Issue Tracker report, or update an existing open issue when it covers the same errors.\n\n" +
        "Do not make speculative code changes. Clearly separate confirmed findings from hypotheses.\n",
    },
    {
      slug: "aws-sqs-alarm-response",
      name: "aws-sqs-alarm-response",
      description: "Investigate a production alarm and record the findings in the Issue Tracker.",
      type: "aws-sqs trigger",
      agent: "Claude Code",
      model: "",
      sqs: "codee-alarms",
      frontmatter: { "x-codee-trigger": "aws-sqs", "x-codee-aws-sqs-queue": "codee-alarms" },
      body:
        "# Alarm Response\n\nAn alarm was triggered with this content:\n\n{CONTENT}\n\n## Workflow\n\n" +
        "1. Determine which service or user flow is affected.\n" +
        "2. Gather relevant logs, metrics, traces, request data, and screenshots.\n" +
        "3. Identify the likely cause, impact, and any immediate mitigation.\n" +
        "4. Search the Issue Tracker for an existing open issue about the same problem.\n" +
        "5. Create or update one issue with the evidence and the recommended action.\n",
    },
    {
      slug: "email-bug-intake",
      name: "email-bug-intake",
      description: "Turn a support mail thread into a triaged Issue Tracker bug.",
      type: "email trigger",
      agent: "Claude Code",
      model: "claude-sonnet-5",
      email: "bugs@codee.example.com",
      frontmatter: { "x-codee-trigger": "email", "x-codee-email": "bugs@codee.example.com" },
      body:
        "# Email Bug Intake\n\n1. Read the whole thread, including quoted replies and attachments.\n" +
        "2. Extract the reported behaviour, the expected behaviour, and the environment.\n" +
        "3. Search the Issue Tracker for a duplicate before creating anything.\n" +
        "4. Create the bug with a reproduction, severity, and the affected component.\n" +
        "5. Reply to the reporter with the issue key.\n",
    },
    {
      slug: "release-notes",
      name: "release-notes",
      description: "Write the release notes for a tagged build from its merged pull requests.",
      type: "slash command",
      agent: "Claude Code",
      model: "claude-sonnet-5",
      frontmatter: { "argument-hint": "<TAG>" },
      body:
        "# Release Notes\n\n1. List every pull request merged since the previous tag.\n" +
        "2. Group them into features, fixes, and internal changes.\n" +
        "3. Write one line per user-visible change, in the user's language, not the commit's.\n" +
        "4. Call out migrations and breaking changes at the top.\n",
    },
    {
      slug: "repository-conventions",
      name: "repository-conventions",
      description: "Branch naming, commit format, and pull request rules every run follows.",
      type: "knowledge",
      agent: "Default agent",
      model: "",
      frontmatter: {},
      body:
        "# Repository Conventions\n\n- Branches: `feature/<ISSUE-KEY>-<slug>`, `fix/<ISSUE-KEY>-<slug>`.\n" +
        "- One worktree per issue, created under `repositories/<name>/`.\n" +
        "- Commits are imperative and name the issue key in the body, never the subject.\n" +
        "- Pull requests open as drafts until the test suite passes.\n" +
        "- Never force-push a branch that already has review comments.\n",
    },
  ];

  /* ----------------------------------------------------------- workflow */

  const workflow = {
    story: {
      title: "Story workflow",
      issueType: "story",
      statuses: [
        "AI Decomposition needed",
        "AI Decomposition review",
        "AI Ready for development",
        "AI In Progress",
        "AI Ready for CR",
        "AI Ready for security review",
        "AI Ready for QA",
        "AI Done",
      ],
      agents: {
        "AI Decomposition needed": { skill: "story-planner", agent: "Claude Code", model: "claude-opus-5" },
        "AI Ready for development": { skill: "story-developer", agent: "Claude Code", model: "claude-opus-5" },
        "AI In Progress": { skill: "story-developer", agent: "Claude Code", model: "claude-opus-5" },
        "AI Ready for CR": { skill: "story-code-reviewer", agent: "Codex", model: "gpt-5-codex" },
        "AI Ready for security review": { skill: "story-security-reviewer", agent: "Claude Code", model: "claude-opus-5" },
        "AI Ready for QA": { skill: "story-qa", agent: "Github Copilot", model: "claude-sonnet-5" },
      },
      humanActions: {
        "AI Decomposition review":
          "Answer the planner's questions and check the subtasks, then move the story to " +
          "AI Ready for development.",
      },
      finalStatuses: ["AI Done"],
      transitions: [
        { source: "AI Decomposition needed", target: "AI Decomposition review", labels: ["story-planner"],
          reasons: [
            "Create the subtasks in the Issue Tracker and move the story to `AI Decomposition review`.",
            "Ask focused questions when requirements, constraints, or expected behavior are unclear. Record the questions in the Issue Tracker, move the story to `AI Decomposition review`, and stop until they are answered.",
          ] },
        { source: "AI Decomposition review", target: "AI Ready for development", labels: [], human: true,
          reasons: ["No issue-trigger skill picks up AI Decomposition review: a person answers the planner's questions and moves the story on to AI Ready for development."] },
        { source: "AI Ready for development", target: "AI In Progress", labels: ["story-developer"],
          reasons: ["Move the story to `AI In Progress` before doing any work, so a story already being worked on is not picked up as new."] },
        { source: "AI In Progress", target: "AI Ready for CR", labels: ["story-developer"],
          reasons: ["Open a pull request and move the story to `AI Ready for CR`."] },
        { source: "AI Ready for CR", target: "AI Ready for security review", labels: ["story-code-reviewer"],
          reasons: ["On approval move the story to `AI Ready for security review`."] },
        { source: "AI Ready for CR", target: "AI Ready for development", labels: ["story-code-reviewer"],
          reasons: ["On blocking findings move it back to `AI Ready for development`."] },
        { source: "AI Ready for security review", target: "AI Ready for QA", labels: ["story-security-reviewer"],
          reasons: ["On approval move the story to `AI Ready for QA`."] },
        { source: "AI Ready for security review", target: "AI Ready for development", labels: ["story-security-reviewer"],
          reasons: ["On findings move it back to `AI Ready for development`."] },
        { source: "AI Ready for QA", target: "AI Done", labels: ["story-qa"],
          reasons: ["On a pass move the story to `AI Done`."] },
        { source: "AI Ready for QA", target: "AI Ready for development", labels: ["story-qa"],
          reasons: ["On a failure move it back to `AI Ready for development` with a reproduction."] },
      ],
      warnings: [],
    },
    task: {
      title: "Task workflow",
      issueType: "task",
      statuses: [
        "AI Ready for development",
        "AI In Progress",
        "AI Ready for CR",
        "AI Ready for security review",
        "AI Ready for QA",
        "AI Done",
      ],
      agents: {
        "AI Ready for development": { skill: "task-developer", agent: "Claude Code", model: "claude-sonnet-5" },
        "AI In Progress": { skill: "task-developer", agent: "Claude Code", model: "claude-sonnet-5" },
        "AI Ready for CR": { skill: "task-code-reviewer", agent: "Codex", model: "gpt-5-codex" },
        "AI Ready for security review": { skill: "task-security-reviewer", agent: "Claude Code", model: "claude-sonnet-5" },
        "AI Ready for QA": { skill: "task-qa", agent: "Github Copilot", model: "claude-sonnet-5" },
      },
      humanActions: {},
      finalStatuses: ["AI Done"],
      transitions: [
        { source: "AI Ready for development", target: "AI In Progress", labels: ["task-developer"],
          reasons: ["Move the task to `AI In Progress` before doing any work."] },
        { source: "AI In Progress", target: "AI Ready for CR", labels: ["task-developer"],
          reasons: ["Open a pull request and move the task to `AI Ready for CR`."] },
        { source: "AI Ready for CR", target: "AI Ready for security review", labels: ["task-code-reviewer"],
          reasons: ["On approval move the task to `AI Ready for security review`."] },
        { source: "AI Ready for CR", target: "AI Ready for development", labels: ["task-code-reviewer"],
          reasons: ["On blocking findings move it back to `AI Ready for development`."] },
        { source: "AI Ready for security review", target: "AI Ready for QA", labels: ["task-security-reviewer"],
          reasons: ["On approval move the task to `AI Ready for QA`."] },
        { source: "AI Ready for security review", target: "AI Ready for development", labels: ["task-security-reviewer"],
          reasons: ["On findings move it back to `AI Ready for development`."] },
        { source: "AI Ready for QA", target: "AI Done", labels: ["task-qa"],
          reasons: ["On a pass move the task to `AI Done`."] },
        { source: "AI Ready for QA", target: "AI Ready for development", labels: ["task-qa"],
          reasons: ["On a failure move it back to `AI Ready for development`."] },
      ],
      warnings: [],
    },
  };

  const workflowProgress = [
    "Reading 14 skills from the skills directory…",
    "Collecting issue-trigger skills for story and task…",
    "Asking Claude Code to infer status transitions…",
    "story: 8 statuses, 11 transitions, 1 human hand-off.",
    "task: 6 statuses, 8 transitions.",
    "Writing the graph to .codee/workflow.json…",
  ];

  /* ------------------------------------------------- memory, repos, etc */

  const memories = [
    { title: "AGIC rollout 502s", file: "agic-rollout-502.md",
      hook: "Every AGW-fronted deploy drops 500-840 real-user requests during the AGIC push." },
    { title: "Issue tracker conventions", file: "issue-tracker-conventions.md",
      hook: "House format for 5xx reviews; API v2 wiki markup, and read comments when deduping." },
    { title: "Staging environment", file: "staging-environment.md",
      hook: "staging.example.com redeploys on every merge to master; QA runs wait for the green check." },
    { title: "Repository conventions", file: "repository-conventions.md",
      hook: "Branch naming, commit format and the draft-PR rule every run follows." },
    { title: "Release train", file: "release-train.md",
      hook: "Production ships Tuesdays and Thursdays at 11:00 CET; nothing merges after 16:00 on a ship day." },
  ];

  const repositories = [
    { name: "platform-api", url: "git@github.com:example-org/platform-api.git", defaultBranch: "master" },
    { name: "web-client", url: "git@github.com:example-org/web-client.git", defaultBranch: "main" },
    { name: "infra-charts", url: "git@github.com:example-org/infra-charts.git", defaultBranch: "main" },
  ];

  const settings = {
    codingAgent: "claude_code",
    maxParallelAgents: 4,
    rotateKeys: true,
    tasksProvider: "jira",
    jira: {
      baseUrl: "https://demo-company.atlassian.net",
      accountEmail: "codee-agent@example.com",
      apiToken: "ATATT3xFfGF0T9k1s2QpLm8vZc7Yb4Nd",
      project: "DEMO",
      taskFilter: 'labels = "codee" AND component != "legacy"',
    },
    workItems: [
      { name: "story", providerTypes: ["Story"], mode: "types", fixed: true, query: "" },
      { name: "task", providerTypes: ["Task", "Bug"], mode: "types", fixed: true, query: "" },
      { name: "spike", providerTypes: [], mode: "query", fixed: false,
        query: 'issuetype = Spike AND labels = "codee"' },
    ],
    mcpConfigured: true,
    sessionViewer: "https://sessions.codee.dev/s/{session_id}",
    agentsFile:
      "# AGENTS.md\n\nThree repositories are cloned under `repositories/`:\n\n" +
      "- `platform-api` — the Python backend and the task API.\n" +
      "- `web-client` — the React front end.\n" +
      "- `infra-charts` — Helm charts and the cluster bootstrap.\n\n" +
      "Always read the repository's own `CLAUDE.md` before editing it.\n",
  };

  global.DEMO = {
    AGENT_NAMES,
    SKILL_TYPES: ["knowledge", "slash command", "issue trigger", "cron trigger", "email trigger", "aws-sqs trigger"],
    AGENT_OPTIONS: ["Default agent", "Claude Code", "Github Copilot", "Codex"],
    activeJobs,
    claudeAccounts,
    hourlyRuns,
    stats,
    runs,
    skills,
    workflow,
    workflowProgress,
    memories,
    repositories,
    settings,
  };
})(window);
