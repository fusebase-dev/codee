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

On macOS the CLI keeps the same JSON in the login Keychain instead, and reads
the Keychain before the file — so a file write alone would be ignored there.
Both are written on a Mac, and the Keychain is what is read back.
"""
import getpass
import json
import os
import shutil
import stat
import subprocess
import sys
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
# The CLI's global config, which names the account it is signed in as. It sits
# in the home directory by default, not in ``~/.claude``, and moves into the
# config directory when ``CLAUDE_CONFIG_DIR`` is set.
GLOBAL_CONFIG_NAME = ".claude.json"
OAUTH_ACCOUNT_SECTION = "oauthAccount"
# The Keychain item the CLI stores its credentials under on macOS.
KEYCHAIN_SERVICE = "Claude Code-credentials"
KEYCHAIN_TIMEOUT_SECONDS = 10

# Credentials are readable by their owner and nobody else. The CLI writes them
# that way and a rewrite has to keep it, including when this module is what
# creates the file.
_OWNER_ONLY = stat.S_IRUSR | stat.S_IWUSR


class CredentialsUnwritable(Exception):
    """The Keychain item could not be written, so the CLI is not signed in.

    Raised rather than logged: on macOS the Keychain is what the CLI reads, so
    a sign-in that only reached the file has not happened.
    """


@dataclass(frozen=True)
class StoredCredentials:
    """The ``claudeAiOauth`` section, as far as Codee reads it."""

    access_token: str = ""
    refresh_token: str = ""
    # Milliseconds since the epoch, which is the unit the CLI writes.
    expires_at: int = 0


def config_dir() -> Path:
    """The CLI's config directory, honouring ``CLAUDE_CONFIG_DIR``."""
    configured = os.environ.get(CONFIG_DIR_ENV, "").strip()
    return Path(configured) if configured else Path.home() / ".claude"


def credentials_file() -> Path:
    """Path to ``.credentials.json``, honouring ``CLAUDE_CONFIG_DIR``."""
    return config_dir() / CREDENTIALS_NAME


def global_config_file() -> Path:
    """Path to ``.claude.json``, honouring ``CLAUDE_CONFIG_DIR``."""
    configured = os.environ.get(CONFIG_DIR_ENV, "").strip()
    return (Path(configured) if configured else Path.home()) / GLOBAL_CONFIG_NAME


def read_credentials() -> StoredCredentials:
    """What the CLI would authenticate with, or an empty set.

    Empty covers every way there can be no answer — no file, unreadable file,
    half-written JSON, an installation signed in some other way. All of them
    mean the same thing to the caller: what is on disk is not something ours
    can be compared against, so write ours.

    On macOS the Keychain is read first, because that is what the CLI reads
    first; the file only counts when the Keychain holds nothing.
    """
    data = _read_keychain() if _uses_keychain() else {}
    if not data:
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

    On macOS the Keychain item is written as well, with the same contents,
    and :class:`CredentialsUnwritable` is raised when it cannot be.
    """
    data = _read_file()
    _merge_oauth(data, access_token, refresh_token, expires_at, scopes,
                 subscription_type)
    _write_json(credentials_file(), data)
    log.debug("signed Claude Code in through %s", credentials_file())

    if _uses_keychain():
        keychain = _read_keychain()
        _merge_oauth(keychain, access_token, refresh_token, expires_at, scopes,
                     subscription_type)
        _write_keychain(keychain)


def write_oauth_account(profile: dict) -> bool:
    """Record whose account the CLI is signed in as, from a profile response.

    The CLI keeps this next to the token in ``.claude.json`` and shows it in
    ``/status``; after a switch it would otherwise go on naming the previous
    account. The section is replaced, not merged: a field the new profile does
    not carry would otherwise keep describing the old account. The CLI fills
    in the rest itself the next time it fetches the profile.

    Only an existing file is touched — one that is not there belongs to a CLI
    that has never run, and will be written by its own first run. Returns
    whether the file was written.
    """
    path = global_config_file()
    if not path.exists():
        return False
    data = _read_json(path)
    account = profile.get("account")
    account = account if isinstance(account, dict) else {}
    organization = profile.get("organization")
    organization = organization if isinstance(organization, dict) else {}
    section = {}
    for source, key, field in (
            (account, "uuid", "accountUuid"),
            (account, "email", "emailAddress"),
            (account, "display_name", "displayName"),
            (account, "full_name", "fullName"),
            (account, "created_at", "accountCreatedAt"),
            (organization, "uuid", "organizationUuid"),
            (organization, "name", "organizationName"),
            (organization, "organization_type", "organizationType"),
            (organization, "billing_type", "billingType"),
            (organization, "rate_limit_tier", "organizationRateLimitTier"),
            (organization, "has_extra_usage_enabled", "hasExtraUsageEnabled"),
            (organization, "subscription_created_at", "subscriptionCreatedAt")):
        if source.get(key) is not None:
            section[field] = source[key]
    data[OAUTH_ACCOUNT_SECTION] = section
    _write_json(path, data, mode=_mode_of(path))
    log.debug("recorded the signed-in account in %s", path)
    return True


def _merge_oauth(data: dict, access_token: str, refresh_token: str,
                 expires_at: int, scopes: str, subscription_type: str) -> None:
    """Write one account into ``data``'s ``claudeAiOauth``, in place."""
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


def _write_json(path: Path, data: dict, mode: int = _OWNER_ONLY) -> None:
    """Replace ``path`` with ``data`` through a temp file and a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".codee.tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    # On the temp file, before the rename: a chmod after it would leave the
    # real credentials world-readable for as long as the two calls take.
    os.chmod(temp, mode)
    os.replace(temp, path)


def _mode_of(path: Path) -> int:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return _OWNER_ONLY


def _read_file() -> dict:
    """The credentials file as a dict, or an empty one when it can't be read.

    A file that cannot be parsed is replaced rather than merged into — there is
    nothing in it worth preserving — but one that simply isn't there yet is the
    ordinary case for a machine the CLI has never signed in on.
    """
    return _read_json(credentials_file())


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        log.debug("could not read %s: %s", path, error)
        return {}
    return data if isinstance(data, dict) else {}


def _uses_keychain() -> bool:
    """Whether the CLI here keeps its credentials in the macOS Keychain.

    Not when ``CLAUDE_CONFIG_DIR`` is set: the CLI then files them under a
    Keychain item named after that directory, so the default item would be
    the wrong one to write. The file is still written in that case.
    """
    return (sys.platform == "darwin"
            and not os.environ.get(CONFIG_DIR_ENV, "").strip()
            and shutil.which("security") is not None)


def _keychain_account() -> str:
    return os.environ.get("USER") or getpass.getuser()


def _read_keychain() -> dict:
    """The CLI's Keychain item as a dict, or an empty one when there is none."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", _keychain_account(),
             "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=KEYCHAIN_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        log.debug("could not read the Keychain: %s", error)
        return {}
    if result.returncode != 0:
        return {}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        log.debug("could not parse the Keychain item: %s", error)
        return {}
    return data if isinstance(data, dict) else {}


def _write_keychain(data: dict) -> None:
    """Create or replace the CLI's Keychain item.

    The secret goes in on stdin, hex-encoded, through ``security -i``, the
    same way the CLI writes it: passed as an argument it would be readable by
    anyone listing processes while the command runs.
    """
    secret = json.dumps(data).encode().hex()
    command = (f'add-generic-password -U -a "{_keychain_account()}" '
               f'-s "{KEYCHAIN_SERVICE}" -X "{secret}"\n')
    try:
        result = subprocess.run(["security", "-i"], input=command,
                                capture_output=True, text=True,
                                timeout=KEYCHAIN_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        raise CredentialsUnwritable(
            f"could not write the Claude Code Keychain item: {error}") from error
    if result.returncode != 0:
        raise CredentialsUnwritable(
            "could not write the Claude Code Keychain item: "
            f"{(result.stderr or result.stdout).strip()}")
    log.debug("signed Claude Code in through the Keychain")
