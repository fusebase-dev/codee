"""Serve files from a private Azure Blob container at ``/p/files/<path>``.

The S3 static-website pattern, minus the public bucket: files are uploaded one
folder at a time with ``az storage blob upload-batch``, and read here with the
host's Azure identity, so the container never needs public access. Whoever can
open the admin UI can open the files; nobody else can.
"""
import mimetypes
from functools import lru_cache

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import ContainerClient
from starlette.responses import RedirectResponse, Response, StreamingResponse

from codee_plugin_abstract.plugin import AbstractCodeePlugin, PluginField

INDEX = "index.html"


@lru_cache(maxsize=4)
def _container(account_url: str, container: str,
               connection_string: str) -> ContainerClient:
    """One client per setting combination; it caches its own tokens."""
    if connection_string:
        return ContainerClient.from_connection_string(
            connection_string, container)
    return ContainerClient(account_url, container,
                           credential=DefaultAzureCredential())


def blob_name(path: str) -> str | None:
    """The blob a URL path names: a folder means its index.html.

    None for a path that tries to climb out with "..".
    """
    if ".." in path.split("/"):
        return None
    path = path.lstrip("/")
    if path == "" or path.endswith("/"):
        return path + INDEX
    return path


def _content_type(name: str, stored: str | None) -> str:
    # az upload-batch guesses a type; trust it unless it is the generic one.
    if stored and stored != "application/octet-stream":
        return stored
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


class AzureBlobFilesPlugin(AbstractCodeePlugin):
    NAME = "files"
    DISPLAY_NAME = "Azure Blob files"
    # Being on publishes a container to everyone who can open the admin UI,
    # so that is a choice someone makes on the Plugins page.
    ENABLED_BY_DEFAULT = False
    SETTINGS_FIELDS = [
        PluginField("account_url", "Storage account URL",
                    placeholder="https://<account>.blob.core.windows.net"),
        PluginField("container", "Container", placeholder="files"),
        PluginField("connection_string", "Connection string",
                    hint="Optional. Leave empty to sign in with the host's "
                         "Azure identity (az login, managed identity).",
                    secret=True),
    ]
    DESCRIPTION = """\
Off until you turn it on. Serves the files of a private Azure Blob container at `/p/files/<path>` on
this admin UI. A folder URL serves its `index.html`. Anyone who can open the
admin UI can open the files, so put the UI behind a sign-in proxy.

Upload a folder:

```bash
az storage blob upload-batch --auth-mode login --account-name <account> \\
  -d <container>/<folder> -s ./my-folder
```

The host's identity needs **Storage Blob Data Reader** on the account to
serve, **Contributor** to upload.
"""

    def serve_http(self, request, path, settings):
        name = blob_name(path)
        if name is None:
            return None
        container = _container(settings.get("account_url", "").strip(),
                               settings.get("container", "").strip(),
                               settings.get("connection_string", "").strip())
        try:
            properties = container.get_blob_client(name).get_blob_properties()
        except ResourceNotFoundError:
            # /folder without its slash: send the browser to /folder/ so the
            # page's relative links resolve inside the folder, like S3 does.
            if not name.endswith(INDEX) and self._exists(
                    container, f"{name}/{INDEX}"):
                return RedirectResponse(request.url.path + "/",
                                        status_code=301)
            return None
        headers = {
            "Content-Length": str(properties.size),
            # No CDN in front and no caching: a re-upload shows at once.
            "Cache-Control": "no-cache",
        }
        if properties.etag:
            headers["ETag"] = properties.etag
        media_type = _content_type(
            name, properties.content_settings.content_type)
        if request.method == "HEAD":
            return Response(headers=headers, media_type=media_type)
        # ponytail: no Range support, so a video plays but seeks only once
        # loaded; add offset/length from the Range header if that matters.
        stream = container.download_blob(name)
        return StreamingResponse(stream.chunks(), headers=headers,
                                 media_type=media_type)

    @staticmethod
    def _exists(container: ContainerClient, name: str) -> bool:
        return container.get_blob_client(name).exists()
