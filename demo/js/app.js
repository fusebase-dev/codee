/* Codee demo — the shell, the router, and every page.

   A demo, so nothing here writes anywhere: the pages read `window.DEMO`, and
   the controls that would change Codee (creating or editing a skill, saving
   settings) are present but disabled, with a line saying why. Everything that
   only changes what is on screen — search, filters, paging, the workflow
   canvas, the connection check — works. */
(function (global) {
  const D = global.DEMO;
  const icon = global.icon;
  const $ = (selector, root) => (root || document).querySelector(selector);
  const $$ = (selector, root) => [...(root || document).querySelectorAll(selector)];

  const esc = (value) =>
    String(value === undefined || value === null ? "" : value).replace(
      /[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );

  const NAV = [
    ["Dashboard", "layout-dashboard", "#/"],
    ["Skills", "blocks", "#/skills"],
    ["Workflow", "git-branch", "#/workflow"],
    ["Memory", "notebook-text", "#/memory"],
    ["Repositories", "folder-git-2", "#/repositories"],
    ["Runs", "history", "#/runs"],
    ["Sessions", "key-round", "#/sessions"],
    ["Settings", "settings", "#/settings"],
  ];

  /* What an outward link says instead of opening: the demo has no Jira and no
     session viewer behind these, and a dead tab explains nothing. */
  const WORK_ITEM_ALERT = "Opens work item in JIRA in new tab";
  const SESSION_ALERT = "Opens agent session in web UI";

  /* State the demo keeps between renders, so a filter survives a redraw. */
  const ui = {
    skillQuery: "",
    skillFilter: "All",
    runsShown: 20,
    checks: null,
    workflowRunning: false,
    workflowProgress: [],
  };

  const timers = [];
  function clearTimers() {
    while (timers.length) clearInterval(timers.pop());
  }

  /* ------------------------------------------------------------- pieces */

  const liveDot = (size) =>
    '<span class="live-dot" style="width:' + (size || "0.6rem") + ";height:" + (size || "0.6rem") + '"><span></span><span></span></span>';

  const pageHeader = (title, description) =>
    '<div class="page-header"><h1>' + esc(title) + "</h1>" +
    (description ? "<p>" + esc(description) + "</p>" : "") + "</div>";

  const emptyState = (name, text) =>
    '<div class="empty-state">' + icon(name, 28) + "<span>" + esc(text) + "</span></div>";

  const demoBanner = (text) =>
    '<div class="demo-banner">' + icon("lock", 13) + "<span>" + esc(text) + "</span></div>";

  const badge = (text, tone, extra) =>
    '<span class="badge' + (tone ? " badge-" + tone : "") + '"' + (extra || "") + ">" + esc(text) + "</span>";

  const callout = (text, tone, iconName) =>
    '<div class="callout' + (tone ? " callout-" + tone : "") + '">' +
    icon(iconName || "info", 16) + "<span>" + text + "</span></div>";

  function toast(message, tone) {
    const host = $("#toasts");
    const node = document.createElement("div");
    node.className = "toast " + (tone || "info");
    node.innerHTML = icon(tone === "success" ? "circle-check" : tone === "warning" ? "triangle-alert" : "info", 16) +
      "<span>" + esc(message) + "</span>";
    host.appendChild(node);
    setTimeout(() => node.remove(), 4000);
  }

  function fmtElapsed(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    return h ? h + "h " + m + "m" : m ? m + "m " + s + "s" : s + "s";
  }

  /* --------------------------------------------------------------- shell */

  function shell(route, content) {
    const theme = document.documentElement.dataset.theme;
    return (
      '<div class="shell">' +
      '<aside class="sidebar">' +
      '<div class="brand"><div class="brand-mark">👨🏻‍💻</div><div class="brand-name">Codee</div>' +
      '<div class="spacer"></div>' +
      '<button class="btn btn-soft btn-icon" id="theme-toggle" aria-label="Toggle color mode">' +
      icon(theme === "dark" ? "sun" : "moon", 15) + "</button></div>" +
      '<nav class="nav">' +
      NAV.map(([label, name, href]) => {
        const active = href === "#/" ? route === "#/" : route.startsWith(href);
        return (
          '<a class="nav-link" href="' + href + '"' + (active ? ' aria-current="page"' : "") + ">" +
          icon(name, 17) + "<span>" + label + "</span></a>"
        );
      }).join("") +
      "</nav></aside>" +
      '<main class="content">' + content + "</main></div>"
    );
  }

  /* ----------------------------------------------------------- dashboard */

  function jobRow(job) {
    const elapsed = fmtElapsed((Date.now() - job.startedAt) / 1000);
    return (
      '<div class="job-row">' + liveDot() +
      '<div class="job-main">' +
      '<div class="job-prompt" title="' + esc(job.message) + '">' + esc(job.promptPrefix) +
      '<a href="' + esc(job.taskUrl) + '" target="_blank" rel="noreferrer" ' +
      'data-demo-alert="' + WORK_ITEM_ALERT + '">' + esc(job.taskKey) + "</a></div>" +
      '<div class="job-meta">' + icon("bot", 13) + "<span>" + esc(job.agent) + "</span>" +
      (job.model ? "<code>" + esc(job.model) + "</code>" : "") + "</div></div>" +
      '<span class="elapsed-pill" data-elapsed="' + job.startedAt + '">' + icon("timer", 14) +
      "<span>" + elapsed + "</span></span>" +
      '<a class="job-link" href="' + esc(job.viewerUrl) + '" target="_blank" rel="noreferrer" ' +
      'data-demo-alert="' + SESSION_ALERT + '" aria-label="View session">' +
      icon("external-link", 16) + "</a></div>"
    );
  }

  function runningPanel() {
    const jobs = D.activeJobs;
    return (
      '<section class="card panel-running">' +
      '<div class="card-head">' + liveDot("0.55rem") +
      '<h2 class="card-title" style="margin:0">Currently running</h2>' +
      '<span class="running-count codee-breathe">' + jobs.length + "</span></div>" +
      '<div class="stack stack-3">' + jobs.map(jobRow).join("") + "</div></section>"
    );
  }

  function meter(label, percent, resets) {
    const tone = percent >= 100 ? "red" : percent >= 80 ? "amber" : "green";
    return (
      "<div>" +
      '<div class="meter-head"><span class="label">' + label + '</span><span class="spacer"></span>' +
      '<span class="value' + (percent >= 100 ? " over" : "") + '">' + percent + "%</span></div>" +
      '<div class="progress progress-' + tone + '"><div style="width:' + Math.min(percent, 100) + '%"></div></div>' +
      (resets && percent >= 80 ? '<div class="meter-reset">resets ' + esc(resets) + "</div>" : "") +
      "</div>"
    );
  }

  function accountUsageRow(account) {
    return (
      '<div class="usage-account' + (account.inUse ? " in-use" : "") + '">' +
      '<div class="usage-head">' + (account.inUse ? liveDot("0.45rem") : "") +
      '<span class="usage-name">' + esc(account.label) + "</span>" +
      (account.subscription ? badge(account.subscription) : "") +
      (account.inUse ? badge("in use", "green") : "") +
      (account.needsReconnect ? badge("sign in again", "amber") : "") +
      "</div>" +
      '<div class="usage-meters">' +
      meter("Session", account.sessionPercent, account.sessionResets) +
      meter("This week", account.weeklyPercent, account.weeklyResets) +
      "</div></div>"
    );
  }

  function accountsPanel() {
    return (
      '<section class="card">' +
      '<div class="card-head"><h2 class="card-title" style="margin:0">Claude accounts</h2></div>' +
      '<div class="stack stack-3">' + D.claudeAccounts.map(accountUsageRow).join("") + "</div></section>"
    );
  }

  function chart() {
    const data = D.hourlyRuns;
    const W = 960, H = 280, padL = 34, padB = 26, padT = 10, padR = 10;
    const max = Math.max(...data.map((d) => d.runs), 1);
    const step = (W - padL - padR) / data.length;
    const barW = Math.max(6, step * 0.62);
    const scale = (value) => (H - padB - padT) * (value / max);
    const ticks = [0, Math.round(max / 2), max].filter((v, i, a) => a.indexOf(v) === i);

    const grid = ticks
      .map((tick) => {
        const y = H - padB - scale(tick);
        return (
          '<line class="grid-line" x1="' + padL + '" y1="' + y + '" x2="' + (W - padR) + '" y2="' + y + '"/>' +
          '<text class="axis-text" x="' + (padL - 8) + '" y="' + (y + 4) + '" text-anchor="end">' + tick + "</text>"
        );
      })
      .join("");

    const bars = data
      .map((point, index) => {
        const h = scale(point.runs);
        const x = padL + index * step + (step - barW) / 2;
        const y = H - padB - h;
        return (
          '<g class="bar-col" data-hour="' + point.hour + '" data-runs="' + point.runs + '">' +
          '<rect class="bar" x="' + x + '" y="' + y + '" width="' + barW + '" height="' + Math.max(h, 1) +
          '" rx="3"/>' +
          '<rect class="bar-hit" x="' + (padL + index * step) + '" y="' + padT + '" width="' + step +
          '" height="' + (H - padB - padT) + '" fill="transparent"/>' +
          '<text class="axis-text" x="' + (x + barW / 2) + '" y="' + (H - padB + 16) +
          '" text-anchor="middle">' + point.hour + "</text></g>"
        );
      })
      .join("");

    return (
      '<section class="card">' +
      '<h2 class="card-title">Last 24 hours by hour</h2>' +
      '<svg class="chart" viewBox="0 0 ' + W + " " + H + '" height="280" width="100%" ' +
      'preserveAspectRatio="none" role="img" aria-label="Runs per hour over the last 24 hours">' +
      grid + bars + "</svg></section>"
    );
  }

  function dashboardPage() {
    return (
      pageHeader("Dashboard", "Run activity and live coding-agent sessions.") +
      '<div class="stack stack-5">' +
      runningPanel() +
      accountsPanel() +
      '<div class="grid grid-3">' +
      '<div class="card stat-tile"><div class="stat-label">Total runs</div>' +
      '<div class="stat-value">' + D.stats.totalRuns.toLocaleString("en-US") + "</div></div>" +
      '<div class="card stat-tile"><div class="stat-label">Last 24 hours</div>' +
      '<div class="stat-value">' + D.stats.last24h + "</div></div>" +
      '<div class="card stat-tile is-running"><div class="stat-label">Running now' + liveDot("0.5rem") + "</div>" +
      '<div class="stat-value">' + D.activeJobs.length + "</div></div>" +
      "</div>" +
      chart() +
      "</div>"
    );
  }

  function dashboardMounted() {
    timers.push(
      setInterval(() => {
        $$("[data-elapsed]").forEach((pill) => {
          const started = Number(pill.dataset.elapsed);
          pill.lastElementChild.textContent = fmtElapsed((Date.now() - started) / 1000);
        });
      }, 1000)
    );

    const tip = document.createElement("div");
    tip.className = "chart-tip";
    tip.hidden = true;
    document.body.appendChild(tip);
    $$(".bar-col").forEach((column) => {
      const hit = $(".bar-hit", column);
      hit.addEventListener("mouseenter", () => {
        $(".bar", column).classList.add("is-hovered");
        tip.hidden = false;
        tip.innerHTML =
          "<strong>" + column.dataset.hour + ":00</strong> · " + column.dataset.runs + " runs";
      });
      hit.addEventListener("mousemove", (event) => {
        tip.style.left = event.clientX + 12 + "px";
        tip.style.top = event.clientY - 34 + "px";
      });
      hit.addEventListener("mouseleave", () => {
        $(".bar", column).classList.remove("is-hovered");
        tip.hidden = true;
      });
    });
    cleanups.push(() => tip.remove());
  }

  /* -------------------------------------------------------------- skills */

  function skillCard(skill) {
    return (
      '<article class="skill-card">' +
      '<div class="head"><h3>' + esc(skill.name) + "</h3>" + badge(skill.type, "blue") + "</div>" +
      '<p class="desc" title="' + esc(skill.description) + '">' + esc(skill.description) + "</p>" +
      '<div class="agent-line">' + icon("bot", 14) + "<span>" + esc(skill.agent) + "</span>" +
      (skill.model ? "<code>" + esc(skill.model) + "</code>" : "") + "</div>" +
      (skill.issueStatus
        ? '<div class="trigger-line">' + badge(skill.issueType, "outline-blue") +
          icon("circle-dot", 14) + "<span>" + esc(skill.issueStatus) + "</span></div>"
        : skill.cron
        ? '<div class="trigger-line">' + icon("clock-3", 14) + "<span>" + esc(skill.cronDescription) + "</span></div>"
        : skill.email
        ? '<div class="trigger-line">' + icon("notebook-text", 14) + "<span>" + esc(skill.email) + "</span></div>"
        : skill.sqs
        ? '<div class="trigger-line">' + icon("bot", 14) + "<span>" + esc(skill.sqs) + "</span></div>"
        : '<div class="spacer-fill"></div>') +
      '<a class="btn btn-soft btn-block skill-card-action" href="#/skills/' + esc(skill.slug) + '">' +
      icon("eye", 15) + "Open</a></article>"
    );
  }

  function agentsCard() {
    return (
      '<article class="skill-card">' +
      '<div class="head"><h3>AGENTS.md</h3>' + badge("always on") + "</div>" +
      '<p class="desc">Plain-text instructions every coding-agent run loads. Cannot be deleted.</p>' +
      '<div class="spacer-fill"></div>' +
      '<a class="btn btn-soft btn-block skill-card-action" href="#/skills/AGENTS.md">' + icon("eye", 15) + "Open</a></article>"
    );
  }

  function filteredSkills() {
    const query = ui.skillQuery.trim().toLowerCase();
    return D.skills.filter((skill) => {
      const matchesType = ui.skillFilter === "All" || skill.type === ui.skillFilter;
      const matchesQuery =
        !query ||
        skill.name.toLowerCase().includes(query) ||
        skill.description.toLowerCase().includes(query);
      return matchesType && matchesQuery;
    });
  }

  function skillsPage() {
    const found = filteredSkills();
    const agentsVisible = ui.skillFilter === "All" && !ui.skillQuery.trim();
    const cards =
      agentsVisible || found.length
        ? '<div class="grid grid-cards">' +
          (agentsVisible ? agentsCard() : "") +
          found.map(skillCard).join("") +
          "</div>"
        : emptyState("search-x", "No skills match this view.");

    return (
      pageHeader("Skills", "Create and configure agent capabilities.") +
      demoBanner("Demo: skills open read-only. Creating, editing and deleting are disabled.") +
      '<div class="stack stack-5">' +
      '<div style="display:flex;gap:0.75rem;width:100%">' +
      '<input class="input" placeholder="New skill name" disabled style="flex:1">' +
      '<button class="btn" disabled title="Disabled in the demo">' + icon("plus", 16) + "Create</button></div>" +
      '<div class="grid" style="grid-template-columns:3fr 1fr;gap:0.75rem">' +
      '<input class="input" id="skill-query" placeholder="Search skills" value="' + esc(ui.skillQuery) + '">' +
      '<select class="select" id="skill-filter">' +
      ["All", ...D.SKILL_TYPES]
        .map((type) => '<option' + (type === ui.skillFilter ? " selected" : "") + ">" + type + "</option>")
        .join("") +
      "</select></div>" +
      cards +
      "</div>"
    );
  }

  function skillsMounted() {
    const query = $("#skill-query");
    if (query) {
      query.addEventListener("input", (event) => {
        ui.skillQuery = event.target.value;
        const at = event.target.selectionStart;
        render();
        const next = $("#skill-query");
        next.focus();
        next.setSelectionRange(at, at);
      });
    }
    const filter = $("#skill-filter");
    if (filter) {
      filter.addEventListener("change", (event) => {
        ui.skillFilter = event.target.value;
        render();
      });
    }
  }

  function frontmatterText(skill) {
    const lines = ["name: " + skill.name, "description: " + skill.description, "disable-model-invocation: true"];
    Object.entries(skill.frontmatter || {}).forEach(([key, value]) => lines.push(key + ": " + value));
    if (skill.model) lines.push("model: " + skill.model);
    return "---\n" + lines.join("\n") + "\n---";
  }

  function skillPage(slug) {
    if (slug === "AGENTS.md") {
      return (
        pageHeader("Skills", "Create and configure agent capabilities.") +
        demoBanner("Demo: AGENTS.md is shown read-only.") +
        '<div class="stack stack-4">' +
        '<div class="editor-bar"><a class="btn btn-ghost" href="#/skills">' + icon("arrow-left", 16) +
        "Back</a><h2 class=\"card-title\" style=\"margin:0\">AGENTS.md</h2>" +
        '<div class="spacer"></div>' +
        '<button class="btn" disabled>' + icon("save", 16) + "Save AGENTS.md</button></div>" +
        '<p class="muted small">Edited as plain text, without skill frontmatter.</p>' +
        '<textarea class="textarea mono" readonly rows="20">' + esc(D.settings.agentsFile) + "</textarea></div>"
      );
    }

    const skill = D.skills.find((item) => item.slug === slug);
    if (!skill) return pageHeader("Skills", "") + emptyState("search-x", "No skill named " + slug + ".");

    const typeFields = [];
    if (skill.type === "issue trigger") {
      typeFields.push(
        '<div class="grid grid-2">' +
        field("Issue type", select([skill.issueType], skill.issueType)) +
        field("Issue statuses", input(skill.issueStatus)) +
        "</div>"
      );
    }
    if (skill.type === "cron trigger") {
      typeFields.push(
        field("Cron expression", input(skill.cron),
          '<span class="hint" style="display:inline-flex;gap:0.35rem;align-items:center">' +
          icon("clock-3", 14) + esc(skill.cronDescription) + "</span>")
      );
    }
    if (skill.type === "email trigger") typeFields.push(field("Email address", input(skill.email)));
    if (skill.type === "aws-sqs trigger") typeFields.push(field("AWS SQS queue", input(skill.sqs)));

    return (
      pageHeader("Skills", "Create and configure agent capabilities.") +
      demoBanner("Demo: this skill is read-only. Saving, deleting and running are disabled.") +
      '<div class="stack stack-5">' +
      '<div class="editor-bar"><a class="btn btn-ghost" href="#/skills">' + icon("arrow-left", 16) +
      "Back</a><div class=\"spacer\"></div>" +
      '<button class="btn btn-red" disabled>' + icon("trash-2", 16) + "Delete skill</button>" +
      '<button class="btn" disabled>' + icon("save", 16) + "Save skill</button></div>" +
      '<div class="grid grid-2">' +
      field("Name", input(skill.name)) +
      field("Skill type", select(D.SKILL_TYPES, skill.type)) +
      "</div>" +
      field("Description", '<textarea class="textarea" readonly rows="2">' + esc(skill.description) + "</textarea>") +
      '<div class="grid grid-2">' +
      field("Agent", select(D.AGENT_OPTIONS, skill.agent),
        '<span class="hint">' + (skill.agent === "Default agent"
          ? "Runs on the default agent from Settings."
          : "Always runs on " + esc(skill.agent) + ", whatever Settings says.") + "</span>") +
      field("Model", input(skill.model || "Agent default"),
        '<span class="hint">' + (skill.model
          ? "Saved as <code>" + esc(skill.model) + "</code> in the skill frontmatter."
          : "Runs on whatever that agent defaults to.") + "</span>") +
      "</div>" +
      typeFields.join("") +
      field("Frontmatter", '<textarea class="textarea mono" readonly rows="' +
        (frontmatterText(skill).split("\n").length + 1) + '">' + esc(frontmatterText(skill)) + "</textarea>") +
      field("Skill body", '<textarea class="textarea mono" readonly rows="18">' + esc(skill.body) + "</textarea>") +
      "</div>"
    );
  }

  const input = (value) => '<input class="input" readonly value="' + esc(value) + '">';
  const select = (options, value) =>
    '<select class="select" disabled>' +
    options.map((option) => '<option' + (option === value ? " selected" : "") + ">" + esc(option) + "</option>").join("") +
    "</select>";
  const field = (label, control, hint) =>
    '<div class="field"><label>' + esc(label) + "</label>" + control + (hint || "") + "</div>";

  /* ------------------------------------------------------------ workflow */

  function workflowPage() {
    const sections = Object.values(D.workflow)
      .map(
        (section) =>
          '<section class="stack stack-4">' +
          '<h2 style="font-size:1.25rem;font-weight:700">' + esc(section.title) + "</h2>" +
          (section.warnings || [])
            .map((warning) => callout(esc(warning), "orange", "triangle-alert"))
            .join("") +
          '<div class="workflow-canvas" data-workflow="' + esc(section.issueType) + '"></div>' +
          '<div class="workflow-legend">' +
          '<span class="legend-item" style="color:#167d5a"><span class="legend-swatch"></span>' +
          '<span class="muted">agent moves it forward</span></span>' +
          '<span class="legend-item" style="color:#d1a207"><span class="legend-swatch"></span>' +
          '<span class="muted">a person moves it forward</span></span>' +
          '<span class="legend-item" style="color:#d97706"><span class="legend-swatch dashed"></span>' +
          '<span class="muted">sent back</span></span>' +
          '<span class="legend-item" style="color:#d1a207"><span class="legend-box"></span>' +
          '<span class="muted">waiting on a person</span></span>' +
          '<span class="muted">Drag to pan, scroll to zoom. Hover an arrow for the reason, click it for its skill.</span>' +
          "</div></section>"
      )
      .join("");

    return (
      '<div class="page-header-row">' +
      pageHeader("Workflow", "Status transitions per work item, inferred from issue-trigger skills.") +
      '<div class="spacer"></div>' +
      '<button class="btn btn-outline" id="workflow-regenerate"' + (ui.workflowRunning ? " disabled" : "") + ">" +
      (ui.workflowRunning ? '<span class="spinner"></span>' : icon("refresh-cw", 16)) +
      (ui.workflowRunning ? "Generating…" : "Regenerate") + "</button></div>" +
      '<div class="stack stack-8">' +
      (ui.workflowRunning || ui.workflowProgress.length
        ? '<div class="card" style="display:flex;gap:0.75rem;align-items:flex-start">' +
          (ui.workflowRunning ? '<span class="spinner"></span>' : icon("circle-check", 16)) +
          '<div><div style="font-weight:600;font-size:0.9rem">' +
          (ui.workflowRunning ? "Generating the workflow..." : "Workflow regenerated.") + "</div>" +
          ui.workflowProgress.map((line) => '<div class="muted small">' + esc(line) + "</div>").join("") +
          "</div></div>"
        : "") +
      sections +
      "</div>"
    );
  }

  function closeOverlays() {
    $("#overlays").innerHTML = "";
  }

  function showEdgeTooltip(edge, x, y) {
    const host = $("#overlays");
    let tip = $("#edge-tooltip");
    if (!tip) {
      tip = document.createElement("div");
      tip.id = "edge-tooltip";
      tip.className = "floating-card tooltip";
      host.appendChild(tip);
    }
    tip.innerHTML =
      '<div class="tooltip-title">Transition reason</div>' +
      edge.reasons.map((reason) => "<div>" + esc(reason) + "</div>").join("");
    tip.style.left = Math.min(x + 14, window.innerWidth - 430) + "px";
    tip.style.top = y + 14 + "px";
  }

  function hideEdgeTooltip() {
    const tip = $("#edge-tooltip");
    if (tip) tip.remove();
  }

  function showEdgeMenu(edge, x, y) {
    closeOverlays();
    const host = $("#overlays");
    const scrim = document.createElement("div");
    scrim.className = "scrim";
    scrim.addEventListener("click", closeOverlays);
    const menu = document.createElement("div");
    menu.className = "floating-card";
    menu.style.left = Math.min(x, window.innerWidth - 240) + "px";
    menu.style.top = Math.min(y, window.innerHeight - 100) + "px";
    menu.innerHTML = edge.labels
      .map(
        (label) =>
          '<button class="menu-item" data-skill="' + esc(label) + '">' + icon("eye", 15) +
          '<span style="font-weight:500">Open skill</span>' +
          '<code style="margin-left:auto">' + esc(label) + "</code></button>"
      )
      .join("");
    host.appendChild(scrim);
    host.appendChild(menu);
    $$("[data-skill]", menu).forEach((button) => {
      button.addEventListener("click", () => {
        closeOverlays();
        location.hash = "#/skills/" + button.dataset.skill;
      });
    });
  }

  function workflowMounted() {
    $$("[data-workflow]").forEach((canvas) => {
      const section = D.workflow[canvas.dataset.workflow];
      global.Workflow.render(canvas, section, {
        onEdgeHover: showEdgeTooltip,
        onEdgeLeave: hideEdgeTooltip,
        onEdgeClick: showEdgeMenu,
        onPaneClick: closeOverlays,
      });
    });

    const button = $("#workflow-regenerate");
    if (button) {
      button.addEventListener("click", () => {
        ui.workflowRunning = true;
        ui.workflowProgress = [];
        render();
        D.workflowProgress.forEach((line, index) => {
          setTimeout(() => {
            ui.workflowProgress.push(line);
            if (index === D.workflowProgress.length - 1) ui.workflowRunning = false;
            if (location.hash.startsWith("#/workflow")) render();
            if (index === D.workflowProgress.length - 1) {
              toast("Workflow regenerated from 14 skills.", "success");
            }
          }, 550 * (index + 1));
        });
      });
    }
  }

  /* -------------------------------------------------- memory, repos, runs */

  function memoryPage() {
    return (
      pageHeader("Memory", "Manage agent provider's memory.") +
      demoBanner("Demo: memory entries are shown read-only.") +
      '<div class="stack stack-3">' +
      D.memories
        .map(
          (entry) =>
            '<div class="row-card"><div class="grow">' +
            '<div style="font-weight:600">' + esc(entry.title) + "</div>" +
            '<div class="muted small">' + esc(entry.hook) + "</div></div>" +
            '<span class="muted mono small">' + esc(entry.file) + "</span>" +
            '<button class="btn btn-ghost btn-icon" disabled aria-label="Edit memory">' + icon("pencil", 15) + "</button>" +
            '<button class="btn btn-ghost btn-icon" disabled aria-label="Delete memory">' + icon("trash-2", 15) + "</button>" +
            "</div>"
        )
        .join("") +
      "</div>"
    );
  }

  function repositoriesPage() {
    return (
      pageHeader("Repositories", "Repositories the coding agents work in.") +
      '<div class="stack stack-5">' +
      '<div class="card">' +
      '<div style="display:flex;gap:0.75rem;width:100%">' +
      '<input class="input" placeholder="git@github.com:org/repo.git" disabled style="flex:1">' +
      '<button class="btn" disabled>' + icon("plus", 16) + "Add repository</button></div>" +
      '<p class="muted small" style="margin-top:0.75rem">Clones a bare repository into ' +
      "repositories/&lt;name&gt;/.bare and checks out its default branch as a worktree beside it. " +
      "A first clone can take a few minutes.</p></div>" +
      callout(
        "The agents work better when they know what each repository is for: describe them in " +
        '<a href="#/skills/AGENTS.md" style="color:var(--codee-accent);text-decoration:underline">AGENTS.md</a>.'
      ) +
      '<div class="stack stack-3">' +
      D.repositories
        .map(
          (repo) =>
            '<div class="row-card">' + icon("folder-git-2", 18) +
            '<div class="grow"><div style="display:flex;gap:0.5rem;align-items:center">' +
            '<span style="font-weight:600">' + esc(repo.name) + "</span>" +
            badge(repo.defaultBranch, "blue", ' title="Default branch"') + "</div>" +
            '<div class="muted mono small">' + esc(repo.url) + "</div></div></div>"
        )
        .join("") +
      "</div></div>"
    );
  }

  function runsPage() {
    const shown = D.runs.slice(0, ui.runsShown);
    return (
      pageHeader("Runs", "Recent trigger executions and outcomes.") +
      '<div class="stack stack-3">' +
      shown
        .map(
          (run) =>
            '<div class="run-card"><div class="run-top"><div class="grow" style="flex:1">' +
            '<div class="run-title">' + esc(run.skillName) +
            badge(run.status, run.status === "succeeded" ? "green" : "red") + "</div>" +
            '<div class="muted mono small">' + esc(run.startedAt) + "</div>" +
            '<div class="muted">' + esc(run.preview) + "</div>" +
            (run.error ? '<div style="color:var(--red-11);font-size:0.85rem">' + esc(run.error) + "</div>" : "") +
            "</div>" +
            badge(run.triggerType, "outline") +
            (run.viewerUrl
              ? '<a href="' + esc(run.viewerUrl) + '" target="_blank" rel="noreferrer" ' +
                'data-demo-alert="' + SESSION_ALERT + '" ' +
                'style="color:var(--codee-accent)" aria-label="View session">' + icon("external-link", 16) + "</a>"
              : "") +
            "</div>" +
            (run.message
              ? '<details class="accordion"><summary>Full message</summary><pre>' + esc(run.message) + "</pre></details>"
              : "") +
            "</div>"
        )
        .join("") +
      (ui.runsShown < D.runs.length
        ? '<button class="btn btn-soft btn-block" id="load-more-runs">Load 20 more</button>'
        : "") +
      "</div>"
    );
  }

  function runsMounted() {
    const more = $("#load-more-runs");
    if (more) {
      more.addEventListener("click", () => {
        ui.runsShown += 20;
        render();
      });
    }
  }

  function sessionsPage() {
    return (
      pageHeader("Sessions", "Open the configured coding agent session viewer.") +
      '<a class="btn" href="https://sessions.codee.dev" target="_blank" rel="noreferrer" ' +
      'data-demo-alert="Opens agent session viewer in web UI">' +
      icon("external-link", 16) + "Open session viewer</a>"
    );
  }

  /* ------------------------------------------------------------ settings */

  function workItemRow(item) {
    return (
      '<div style="display:flex;gap:0.5rem;align-items:center;width:100%">' +
      '<div style="flex:1;min-width:0">' + input(item.name) + "</div>" +
      '<span class="muted">' + icon("arrow-right", 16) + "</span>" +
      '<div style="flex:2;min-width:0;display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap">' +
      '<span class="badge badge-outline">' + (item.mode === "types" ? "Types" : "JQL (Advanced)") + "</span>" +
      (item.mode === "types"
        ? item.providerTypes.map((type) => badge(type)).join("") +
          '<span class="muted small">' + (item.fixed ? "built-in work item" : "") + "</span>"
        : '<div style="flex:1;min-width:12rem">' + input(item.query) + "</div>") +
      "</div>" +
      '<div style="width:2rem;display:flex;justify-content:center">' +
      (item.fixed ? "" : '<button class="btn btn-ghost btn-icon" disabled aria-label="Remove work item">' +
        icon("trash-2", 14) + "</button>") +
      "</div></div>"
    );
  }

  function checkRow(check) {
    const mark =
      check.status === "running"
        ? '<span class="spinner"></span>'
        : check.status === "ok"
        ? '<span style="color:var(--green-9)">' + icon("circle-check", 16) + "</span>"
        : check.status === "failed"
        ? '<span style="color:var(--red-9)">' + icon("circle-alert", 16) + "</span>"
        : '<span class="muted">' + icon("circle-dashed", 16) + "</span>";
    return (
      '<div style="display:flex;gap:0.5rem;align-items:flex-start;width:100%">' + mark +
      '<div><div style="font-size:0.85rem;font-weight:500' +
      (check.status === "waiting" ? ";color:var(--codee-muted)" : "") + '">' + esc(check.name) + "</div>" +
      (check.message ? '<div class="muted small">' + esc(check.message) + "</div>" : "") +
      "</div></div>"
    );
  }

  function settingsPage() {
    const s = D.settings;
    const checks = ui.checks || [];
    return (
      pageHeader("Settings", "") +
      demoBanner("Demo: settings are prefilled and read-only — nothing here can be changed or saved.") +
      '<div class="stack stack-5">' +

      '<section class="card"><h2 class="card-title">Coding agent</h2>' +
      '<div class="stack stack-4">' +
      field("Default agent", select(["claude_code", "github_copilot", "codex"], s.codingAgent)) +
      field("Max parallel tasks", input(s.maxParallelAgents),
        '<span class="hint">How many task agents may run at once.</span>') +
      "</div></section>" +

      '<section class="card"><h2 class="card-title">Claude Code</h2>' +
      '<div class="stack stack-3">' +
      '<label class="checkbox"><input type="checkbox" checked disabled>' +
      "<span>Use specified accounts - Auto accounts rotate</span></label>" +
      D.claudeAccounts
        .map(
          (account) =>
            '<div style="display:flex;gap:0.75rem;align-items:center;width:100%;padding:0.45rem 0.7rem;' +
            'border:1px solid var(--codee-border);background:var(--codee-page-background)">' +
            '<span class="muted">' + icon("circle-user-round", 16) + "</span>" +
            '<span style="font-size:0.85rem">' + esc(account.label) + "</span>" +
            badge(account.subscription) +
            (account.inUse ? badge("in use", "green") : "") +
            '<span style="flex:1"></span>' +
            '<button class="btn btn-ghost btn-icon" disabled aria-label="Disconnect ' + esc(account.label) + '">' +
            icon("trash-2", 14) + "</button></div>"
        )
        .join("") +
      '<button class="btn btn-outline" disabled>' + icon("plus", 16) + "Connect a Claude account</button>" +
      "</div></section>" +

      '<section class="card"><h2 class="card-title">Tasks provider</h2>' +
      field("Provider", select(["jira", "azure_devops"], s.tasksProvider)) +
      '<div style="margin-top:1rem" class="stack stack-4">' +
      field("Base URL", input(s.jira.baseUrl),
        '<span class="hint">Your Jira site, for instance https://your-company.atlassian.net.</span>') +
      field("API Token Owner Email", input(s.jira.accountEmail),
        '<span class="hint">The Atlassian account the API token belongs to.</span>') +
      field("API token", '<input class="input" type="password" readonly value="' + esc(s.jira.apiToken) + '">') +
      field("Project key", input(s.jira.project),
        '<span class="hint">The project Codee polls for work items.</span>') +
      "</div>" +

      '<div style="margin-top:1.25rem" class="stack stack-3">' +
      '<div style="font-weight:500;font-size:0.85rem">Work items</div>' +
      s.workItems.map(workItemRow).join("") +
      '<div style="display:flex;gap:0.75rem;align-items:center">' +
      '<button class="btn btn-outline" disabled>' + icon("plus", 16) + "Add work item</button>" +
      '<button class="btn btn-outline" disabled>' + icon("refresh-cw", 16) + "Reload types</button>" +
      '<span class="muted small">7 work item types read from Jira.</span></div></div>' +

      '<div style="margin-top:1.25rem">' +
      field("Custom JQL",
        '<textarea class="textarea" readonly rows="2">' + esc(s.jira.taskFilter) + "</textarea>",
        '<span class="hint">Optional. Added to every task query as one more AND condition, on top of ' +
        "the work items above.</span>") + "</div>" +

      '<div style="margin-top:1.25rem;display:flex;gap:0.75rem;align-items:center">' +
      '<span style="color:var(--green-9)">' + icon("circle-check", 16) + "</span>" +
      '<span style="font-size:0.85rem">MCP config already set up.</span>' +
      '<button class="btn btn-outline" disabled>' + icon("file-cog", 16) + "Setup Jira MCP again</button></div>" +

      '<div style="margin-top:1.25rem" class="stack stack-3">' +
      '<div style="display:flex;gap:0.75rem;align-items:center">' +
      '<button class="btn btn-outline" id="verify-tasks">' + icon("plug-zap", 16) + "Verify connection</button>" +
      '<span class="muted small">Pull/modification tasks check.</span></div>' +
      (checks.length
        ? '<div class="stack stack-3" style="padding:0.85rem;border:1px solid var(--codee-border);' +
          'background:var(--codee-page-background)">' + checks.map(checkRow).join("") + "</div>"
        : "") +
      "</div></section>" +

      '<div><button class="btn" id="save-settings">' + icon("save", 16) + "Save settings</button></div>" +
      "</div>"
    );
  }

  const CHECK_NAMES = ["Tasks can be pulled", "The coding agent can work through the MCP server"];

  function settingsMounted() {
    const save = $("#save-settings");
    if (save) {
      save.addEventListener("click", () =>
        toast("Demo mode: settings are read-only and nothing was saved.", "warning"));
    }
    const verify = $("#verify-tasks");
    if (verify) {
      verify.addEventListener("click", () => {
        ui.checks = CHECK_NAMES.map((name, index) => ({
          name,
          status: index === 0 ? "running" : "waiting",
          message: "",
        }));
        render();
        setTimeout(() => {
          ui.checks = [
            { name: CHECK_NAMES[0], status: "ok", message: "14 work items pulled from DEMO (3 story, 11 task)." },
            { name: CHECK_NAMES[1], status: "running", message: "" },
          ];
          if (location.hash.startsWith("#/settings")) render();
        }, 1100);
        setTimeout(() => {
          ui.checks = [
            { name: CHECK_NAMES[0], status: "ok", message: "14 work items pulled from DEMO (3 story, 11 task)." },
            { name: CHECK_NAMES[1], status: "ok",
              message: "Claude Code created, commented on and closed DEMO-4840 through the Jira MCP server." },
          ];
          if (location.hash.startsWith("#/settings")) render();
        }, 2600);
      });
    }
  }

  /* -------------------------------------------------------------- router */

  const cleanups = [];

  const TITLES = {
    "#/": "Dashboard",
    "#/skills": "Skills",
    "#/workflow": "Workflow",
    "#/memory": "Memory",
    "#/repositories": "Repositories",
    "#/runs": "Runs",
    "#/sessions": "Sessions",
    "#/settings": "Settings",
  };

  function route() {
    const hash = location.hash || "#/";
    if (hash.startsWith("#/skills/")) return { key: "#/skills", slug: decodeURIComponent(hash.slice(9)) };
    return { key: TITLES[hash] ? hash : "#/", slug: null };
  }

  function render() {
    clearTimers();
    while (cleanups.length) cleanups.pop()();
    closeOverlays();
    hideEdgeTooltip();

    const current = route();
    // The regeneration banner belongs to one visit to the workflow page: a
    // finished run's progress lines are stale the moment the page is left.
    if (current.key !== "#/workflow" && !ui.workflowRunning) ui.workflowProgress = [];

    let content;
    if (current.slug !== null) content = skillPage(current.slug);
    else if (current.key === "#/") content = dashboardPage();
    else if (current.key === "#/skills") content = skillsPage();
    else if (current.key === "#/workflow") content = workflowPage();
    else if (current.key === "#/memory") content = memoryPage();
    else if (current.key === "#/repositories") content = repositoriesPage();
    else if (current.key === "#/runs") content = runsPage();
    else if (current.key === "#/sessions") content = sessionsPage();
    else content = settingsPage();

    document.title = TITLES[current.key] + " | Codee";
    $("#app").innerHTML = shell(current.key, content);

    $("#theme-toggle").addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("codee-demo-theme", next); } catch (error) { /* private window */ }
      render();
    });

    if (current.slug === null) {
      if (current.key === "#/") dashboardMounted();
      if (current.key === "#/skills") skillsMounted();
      if (current.key === "#/workflow") workflowMounted();
      if (current.key === "#/runs") runsMounted();
      if (current.key === "#/settings") settingsMounted();
    }
  }

  function boot() {
    document.addEventListener("click", (event) => {
      const link = event.target.closest("[data-demo-alert]");
      if (!link) return;
      event.preventDefault();
      toast(link.dataset.demoAlert);
    });

    let stored = null;
    try { stored = localStorage.getItem("codee-demo-theme"); } catch (error) { /* private window */ }
    document.documentElement.dataset.theme =
      stored || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    window.addEventListener("hashchange", () => {
      render();
      window.scrollTo(0, 0);
    });
    render();
  }

  boot();
})(window);
