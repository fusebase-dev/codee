"""``codee-agent``: the entry point a fresh machine starts from.

Named after the distribution rather than after what it does, so that
``uvx codee-agent init`` works without anything being installed first — uvx
resolves the package and runs the console script that shares its name. The
subcommands are where the verbs live.
"""
import argparse
import sys
from pathlib import Path

from codee.admin_service import DEFAULT_ADMIN_PORT
from codee.init_cli import confirm_overwrite, scaffold
from codee import setup_wizard


def _parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codee-agent",
        description="Set up and run a Codee project.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init = subcommands.add_parser(
        "init",
        help="create a Codee project here and configure it",
        description="Create a Codee project in the current directory, then "
                    "ask which coding agent and tasks provider to use.")
    init.add_argument(
        "--port", type=int, default=DEFAULT_ADMIN_PORT,
        help="port the admin UI is started on when a provider has to finish "
             f"its setup in the browser (default: {DEFAULT_ADMIN_PORT})")
    init.add_argument(
        "--scaffold-only", action="store_true",
        help="write the project files and skip every question")
    return parser.parse_args(argv)


def init(arguments: argparse.Namespace) -> int:
    destination = Path.cwd()
    if not confirm_overwrite(destination):
        print("codee-agent init: cancelled")
        return 1

    setup_wizard.heading("Codee project")
    setup_wizard.wrapped(str(destination))
    setup_wizard.line()
    setup_wizard.wrapped("Created " + ", ".join(scaffold(destination)))

    # Every question below needs someone to answer it. Piped into or run from a
    # script, the scaffolding is still worth doing on its own, so that half
    # succeeds and the user is told where the other half lives.
    if arguments.scaffold_only or not sys.stdin.isatty():
        setup_wizard.describe_manual_setup(arguments.port)
        return 0

    return setup_wizard.run(destination, arguments.port)


def main() -> int:
    arguments = _parse_arguments(sys.argv[1:])
    if arguments.command == "init":
        try:
            return init(arguments)
        except (KeyboardInterrupt, EOFError):
            print("\ncodee-agent init: cancelled")
            return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())
