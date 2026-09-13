"""Reading and writing the account in Claude Code's own credentials file.

The ``claude`` CLI authenticates a subscription from
``~/.claude/.credentials.json``, and nothing on its command line can point one
run at a different account. So rotating between accounts means writing that
file — which is why this module exists rather than the agent passing a token
per run.

The whole ``claudeAiOauth`` section is written, not just the access token: a
session token is good for hours while an agent run can take two, so the CLI has
to be left holding the refresh token and the real expiry that let it renew the
token mid-run. Writing an access token alone — under an expiry invented to stop
the CLI trying to renew it — would strand a long run the moment it aged out.
Anything else in the file is left exactly as it was found.
"""
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from codee_main_context.logging import get_logger

log = get_logger(__name__)

# Where the CLI keeps them. ``CLAUDE_CONFIG_DIR`` is the CLI's own override, so
# an installation that moved its config is followed rather than having a second
# credentials file written next to a directory nothing reads.
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
CREDENTIALS_NAME = ".credentials.json"
OAUTH_SECTION = "claudeAiOauth"

# Credentials are readable by their owner and nobody else. The CLI writes them
# that way and a rewrite has to keep it, including when this module is what
# creates the file.
_OWNER_ONLY = stat.S_IRUSR | stat.S_IWUSR


@dataclass(frozen=True)
class StoredCredentials:
    """The ``claudeAiOauth`` section, as far as Codee reads it."""

    access_token: str = ""
    refresh_token: str = ""
    # Milliseconds since the epoch, which is the unit the CLI writes.
    expires_at: int = 0


def credentials_file() -> Path:
    """Path to ``.credentials.json``, honouring ``CLAUDE_CONFIG_DIR``."""
    configured = os.environ.get(CONFIG_DIR_ENV, "").strip()
    directory = Path(configured) if configured else Path.home() / ".claude"
    return directory / CREDENTIALS_NAME


def read_credentials() -> StoredCredentials:
    """What the CLI would authenticate with, or an empty set.

    Empty covers every way there can be no answer — no file, unreadable file,
    half-written JSON, an installation signed in some other way. All of them
    mean the same thing to the caller: what is on disk is not something ours
    can be compared against, so write ours.
    """
    data = _read_file()
    section = data.get(OAUTH_SECTION)
    if not isinstance(section, dict):
        return StoredCredentials()
    expires_at = section.get("expiresAt")
    return StoredCredentials(
        access_token=str(section.get("accessToken") or ""),
        refresh_token=str(section.get("refreshToken") or ""),
        expires_at=int(expires_at) if isinstance(expires_at, int) else 0,
    )


def read_access_token() -> str:
    """Just the access token the CLI would authenticate with, or ``""``."""
    return read_credentials().access_token


def write_credentials(access_token: str, refresh_token: str = "",
                      expires_at: int = 0, scopes: str = "",
                      subscription_type: str = "") -> None:
    """Sign the CLI in as one account, keeping everything else in the file.

    Written through a temp file in the same directory and renamed into place:
    the CLI reads this file at the start of every run, and a torn read there
    would look like a signed-out installation to whichever agent run happened
    to land on it.
    """
    data = _read_file()
    section = data.get(OAUTH_SECTION)
    section = dict(section) if isinstance(section, dict) else {}
    section["accessToken"] = access_token
    section["refreshToken"] = refresh_token
    section["expiresAt"] = expires_at
    # Only written when known: a sign-in reports both, but a caller that has
    # neither must not blank out what the file already says about the plan.
    if scopes:
        section["scopes"] = scopes.split()
    if subscription_type:
        section["subscriptionType"] = subscription_type
    data[OAUTH_SECTION] = section

    path = credentials_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".codee.tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    # On the temp file, before the rename: a chmod after it would leave the
    # real credentials world-readable for as long as the two calls take.
    os.chmod(temp, _OWNER_ONLY)
    os.replace(temp, path)
    log.debug("signed Claude Code in through %s", path)


def _read_file() -> dict:
    """The credentials file as a dict, or an empty one when it can't be read.

    A file that cannot be parsed is replaced rather than merged into — there is
    nothing in it worth preserving — but one that simply isn't there yet is the
    ordinary case for a machine the CLI has never signed in on.
    """
    path = credentials_file()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        log.debug("could not read %s: %s", path, error)
        return {}
    return data if isinstance(data, dict) else {}
