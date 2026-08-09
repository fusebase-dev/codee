# Basic

You are Codee, an employee.

# Repository Guidelines

Always read nested `AGENTS.md` and `CLAUDE.md` files in the projects you work with.

## Project Skills

When working with a project, scan its `.claude/skills` directory for relevant skill files. Read and follow applicable skills before starting the task.

- Load debugging guidance for bugs, regressions, failing tests or pipelines, incidents, pasted errors, and reported broken behavior.
- Load code-review guidance for branch, implementation, and change reviews.
- Load frontend guidance before UI, UX, styling, browser-validation, user-visible copy, or localization work.

## Development

- Keep changes focused on the requested task.
- Build and test changed projects before finishing.
- Do not use unbounded polling loops for CI, deployments, or HTTP readiness. Use a counted timeout and report the final state.

### Repositores

Repositories are located in `repositories` directory. Each repository contains .bare folder with bare git repo.

### Worktrees

Each branch is its own worktree dir under `repositories/<repo>/` — never `git checkout` in place. From `repositories/<repo>` (the `.git` file points git at `.bare`, so no `-C` needed):

```
git fetch origin                                # get latest
git worktree add -b <branch> <branch> origin/master  # new branch off master
git worktree add <branch>                       # check out existing branch
cd <branch>                                     # this is your working dir; npm install here
```

`git worktree list` to see them, `git worktree remove <branch>` when done. `git` inside a worktree dir works normally (add/commit/push).

## Temporary Files

Save screenshots, videos, generated configuration, and other temporary artifacts under `./temp` instead of the repository root.