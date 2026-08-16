import shutil
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
# writes provider API tokens into it (see codee.lib.mcp_config).
GITIGNORE_ENTRIES = ("/repositories", "/temp", ".mcp.json")


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


def main() -> int:
    destination = Path.cwd()
    conflicts = [path for path in CONFLICT_PATHS if (
        destination / path).exists()]
    if conflicts:
        joined = ", ".join(str(path) for path in conflicts)
        answer = input(
            f"Existing paths will be updated ({joined}). Continue? [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            print("codee-init: cancelled")
            return 1

    with as_file(TEMPLATE_ROOT) as template_root:
        for source_name, target_path in TARGETS.items():
            source = template_root / source_name
            target = destination / target_path
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target)

    ensure_working_directories(destination)

    print("Created AGENTS.md, CLAUDE.md, .claude/skills, "
          "repositories/, temp/ and memory/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
