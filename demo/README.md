# Codee demo

A static, click-through copy of the Codee admin UI — plain HTML, CSS and
JavaScript, no build step and no backend. It exists to show the product
without running Codee, a tasks provider or a coding agent.

Live at <https://fusebase-dev.github.io/codee/demo/> — the Pages workflow
(`.github/workflows/static.yml`) copies this folder into the Docusaurus build
output next to the docs, so a push to `master` publishes both.

## Run it

```bash
open demo/index.html          # works straight off the filesystem
# or, for a normal http:// origin:
python3 -m http.server 8777 --directory demo
```

## What is real and what is not

Every page of the admin UI is here and every menu item is clickable; the data
behind them is fixture data from `js/data.js`.

| Page | In the demo |
| --- | --- |
| Dashboard | Four live sessions across Claude Code, Codex and Github Copilot, with elapsed timers that tick. Three connected Claude accounts with their session and weekly allowance. Total / last-24h / running-now tiles and the runs-per-hour chart. |
| Skills | The 14 skills as cards, with search and type filter. Creating, editing and deleting are **disabled**; every skill opens read-only, frontmatter and body included. |
| Workflow | The story and task status graphs, drawn from the same rules the real page uses. Drag to pan, scroll or use the buttons to zoom, hover an arrow for the transition reason, click one for the skill behind it, and **Regenerate** replays a generation. |
| Memory | The memory index, read-only. |
| Repositories | The cloned repositories; adding one is disabled. |
| Runs | 25 recent runs with status, trigger, full message and paging. |
| Sessions | The session-viewer link. |
| Settings | Prefilled for Jira — agent, rotation with three accounts, provider credentials, work item mapping, custom JQL, MCP. Everything is **read-only**; *Save settings* says so, and *Verify connection* runs an animated check that changes nothing. |

Light and dark mode both work; the toggle sits next to the logo and the choice
is remembered in `localStorage`.

## Files

```
demo/
  index.html        the shell; everything else is loaded from here
  css/styles.css    the admin UI's design tokens and every component
  js/icons.js       the lucide icons the UI uses, as inline SVG
  js/data.js        all fixture data — the only file to edit to change content
  js/workflow.js    workflow layout, SVG edges and the pan/zoom canvas
  js/app.js         hash router, pages and interactions
```

The colour tokens in `css/styles.css` are copied name for name from the
`shell()` style block in `src/codee/admin.py`, so a colour changed there can be
found here.
