import re
import shutil
import subprocess
import sys
from importlib.resources import as_file, files
from pathlib import Path


TEMPLATE_ROOT = files("codee").joinpath("templates")
TARGETS = {
    "AGENTS.md": Path("AGENTS.md"),
    "CLAUDE.md": Path("CLAUDE.md"),
    "skills": Path(".claude/skills"),
}
CONFLICT_PATHS = (Path(".claude"), Path("AGENTS.md"), Path("CLAUDE.md"))
# Directories the agents write into: cloned repositories, scratch files, and
# long-term memory.
WORKING_DIRECTORIES = (Path("repositories"), Path("temp"), Path("memory"))
# Of those directories, only the per-checkout state is kept out of git;
# `memory/` is tracked on purpose, since the admin UI commits and pushes memory
# edits (see AdminService). `.mcp.json` joins them because the settings page
# writes provider API tokens into it (see codee.lib.mcp_config), `/.codee` for
# the same reason plus the runs database and the admin UI's node_modules under
# it, and `.venv` because this is a uv project like any other.
GITIGNORE_ENTRIES = ("/repositories", "/temp", ".mcp.json", "/.codee", ".venv")

PYPROJECT = Path("pyproject.toml")
# The minimum that makes the directory a uv project depending on Codee, so
# `uv run codee-start` resolves and installs it without another step. Kept to
# what `uv init` would have written plus the one dependency: anything more is a
# decision belonging to the project, not to its scaffolding.
PYPROJECT_TEMPLATE = """[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["codee-agent"]
"""
FALLBACK_PROJECT_NAME = "codee-project"


def _ignored_patterns(gitignore: Path) -> set[str]:
    """Patterns already listed, normalized so `/temp`, `temp/` and `temp` match."""
    if not gitignore.is_file():
        return set()

    patterns = set()
    for line in gitignore.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            patterns.add(stripped.strip("/"))
    return patterns


def ensure_working_directories(destination: Path) -> None:
    """Create the runtime directories and make sure git ignores them."""
    for directory in WORKING_DIRECTORIES:
        (destination / directory).mkdir(parents=True, exist_ok=True)

    gitignore = destination / ".gitignore"
    ignored = _ignored_patterns(gitignore)
    missing = [entry for entry in GITIGNORE_ENTRIES
               if entry.strip("/") not in ignored]
    if not missing:
        return

    existing = gitignore.read_text() if gitignore.is_file() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    gitignore.write_text(existing + "".join(f"{entry}\n" for entry in missing))


def project_name(destination: Path) -> str:
    """A PEP 508-legal package name derived from the directory being set up.

    The directory name is what the user already chose to call this, so it is
    the least surprising thing to put in `pyproject.toml`. A name that survives
    normalization to nothing (a directory called `.` or `_`) falls back rather
    than writing a file uv would refuse to parse.
    """
    name = re.sub(r"[^a-z0-9]+", "-",
                  destination.resolve().name.lower()).strip("-")
    return name or FALLBACK_PROJECT_NAME


def ensure_pyproject(destination: Path) -> bool:
    """Write `pyproject.toml` unless the directory already has one.

    Never merged into an existing file: a project that already declares itself
    has dependencies and build settings of its own, and the one line it might
    be missing (`codee-agent`) is `uv add codee-agent` away. Returns whether a
    file was written, so the caller can say so.
    """
    path = destination / PYPROJECT
    if path.exists():
        return False
    path.write_text(PYPROJECT_TEMPLATE.format(name=project_name(destination)))
    return True


def ensure_git_repository(destination: Path) -> bool:
    """Run `git init` unless this is already inside a work tree.

    The check is `rev-parse` rather than a test for `.git/`, so a subdirectory
    of an existing repository is left alone instead of becoming a nested one.
    A machine without git is not an error here: nothing else in the setup needs
    it, and the repositories Codee works on are cloned by the admin UI later.
    """
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=destination, capture_output=True, text=True, check=False)
        if inside.returncode == 0 and inside.stdout.strip() == "true":
            return False
        return subprocess.run(["git", "init", "--quiet"], cwd=destination,
                              capture_output=True, check=False).returncode == 0
    except OSError:
        return False


def scaffold(destination: Path) -> list[str]:
    """Lay down every file a Codee project needs, and say what was created.

    Idempotent: run against a project that already has some of this and only
    the missing pieces appear. The templates themselves are always refreshed,
    which is how an existing project picks up new packaged skills.
    """
    with as_file(TEMPLATE_ROOT) as template_root:
        for source_name, target_path in TARGETS.items():
            source = template_root / source_name
            target = destination / target_path
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target)

    ensure_working_directories(destination)

    created = ["AGENTS.md", "CLAUDE.md", ".claude/skills",
               "repositories/", "temp/", "memory/"]
    if ensure_pyproject(destination):
        created.append(str(PYPROJECT))
    if ensure_git_repository(destination):
        created.append("git repository")
    return created


def confirm_overwrite(destination: Path) -> bool:
    """Ask before refreshing templates over files that are already there."""
    conflicts = [path for path in CONFLICT_PATHS if (
        destination / path).exists()]
    if not conflicts:
        return True
    joined = ", ".join(str(path) for path in conflicts)
    answer = input(
        f"Existing paths will be updated ({joined}). Continue? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


def main() -> int:
    destination = Path.cwd()
    if not confirm_overwrite(destination):
        print("codee-init: cancelled")
        return 1

    print("Created " + ", ".join(scaffold(destination)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
