"""Serve the files of a local folder at ``/p/files/<path>`` on the admin UI.

Codee (or anyone on the host) writes files into the folder; whoever can open
the admin UI can open them, nobody else. Like an S3 static website: a folder
URL serves its ``index.html``.
"""
from pathlib import Path

from starlette.responses import FileResponse, RedirectResponse

from codee_plugin_abstract.plugin import AbstractCodeePlugin, PluginField

INDEX = "index.html"


def resolve(root: Path, path: str) -> Path | None:
    """The file a URL path names inside ``root``; a folder means its
    index.html. None when it is missing or would leave ``root``."""
    root = root.resolve()
    target = (root / path.lstrip("/")).resolve()
    if not target.is_relative_to(root):
        return None
    if target.is_dir():
        target = target / INDEX
    return target if target.is_file() else None


class FilesPlugin(AbstractCodeePlugin):
    NAME = "files"
    DISPLAY_NAME = "Files"
    # Being on shows the folder to everyone who can open the admin UI, so
    # that is a choice someone makes on the Plugins page.
    ENABLED_BY_DEFAULT = False
    SETTINGS_FIELDS = [
        PluginField("folder", "Folder",
                    hint="Absolute path of the folder to serve. Empty: "
                         ".codee/plugins/files in the Codee project.",
                    placeholder="/home/me/codee-files"),
    ]
    DESCRIPTION = """\
Off until you turn it on. Serves the files of a folder on this host at
`/p/files/<path>` on the admin UI: put `report/index.html` in the folder and
open `<admin URL>/p/files/report/`. A folder URL serves its `index.html`.

Anyone who can open the admin UI can open the files, so keep the UI behind a
sign-in proxy. Nothing is cached, so a changed file shows at once.

**Tell the agents about it.** They do not read this card. Add a line to
`AGENTS.md` (or to the skill that makes reports or mockups) with the folder
and the URL, for example:

> To share a page with the team, write it to `<folder>/<name>/index.html`
> and give the link `<admin URL>/p/files/<name>/`.
"""

    def folder(self, settings: dict[str, str]) -> Path:
        configured = settings.get("folder", "").strip()
        return Path(configured) if configured else self.data_dir

    def serve_http(self, request, path, settings):
        root = self.folder(settings)
        target = resolve(root, path)
        if target is None:
            return None
        # /report without its slash: send the browser to /report/ so the
        # page's relative links resolve inside the folder.
        if target.name == INDEX and path and not path.endswith("/") \
                and not path.endswith(INDEX):
            return RedirectResponse(request.url.path + "/", status_code=301)
        return FileResponse(target, headers={"Cache-Control": "no-cache"})
