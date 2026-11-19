"""The plugin against a real blob API: Azurite, the Azure Storage emulator.

Start it with ``docker run -d -p 10000:10000
mcr.microsoft.com/azure-storage/azurite azurite-blob --blobHost 0.0.0.0``;
the tests skip when it is not running.
"""
import socket
import unittest
import uuid

from azure.storage.blob import BlobServiceClient, ContentSettings
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient

from codee_azure_blob_files.plugin import AzureBlobFilesPlugin, blob_name

# Azurite's documented, public development account.
AZURITE = (
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq"
    "/K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;")


def _azurite_running() -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", 10000)) == 0


class BlobNameTest(unittest.TestCase):
    def test_a_folder_means_its_index(self) -> None:
        self.assertEqual(blob_name(""), "index.html")
        self.assertEqual(blob_name("a/"), "a/index.html")
        self.assertEqual(blob_name("a/b.css"), "a/b.css")

    def test_climbing_out_is_refused(self) -> None:
        self.assertIsNone(blob_name("a/../b"))
        self.assertIsNone(blob_name(".."))


@unittest.skipUnless(_azurite_running(), "Azurite is not running on :10000")
class ServeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.container = f"t{uuid.uuid4().hex[:12]}"
        client = BlobServiceClient.from_connection_string(AZURITE)
        container = client.create_container(self.container)
        self.addCleanup(container.delete_container)
        container.upload_blob("site/index.html", b"<h1>hi</h1>",
                              content_settings=ContentSettings("text/html"))
        # Uploaded with no type: served by its extension.
        container.upload_blob("site/app.css", b"h1{}")

        plugin = AzureBlobFilesPlugin()
        settings = {"container": self.container, "connection_string": AZURITE}

        def serve(request):
            response = plugin.serve_http(
                request, request.path_params["path"], settings)
            return response or Response(status_code=404)

        app = Starlette(routes=[Route("/p/files/{path:path}", serve,
                                      methods=["GET", "HEAD"])])
        self.client = TestClient(app, follow_redirects=False)

    def test_a_folder_serves_its_index_html(self) -> None:
        response = self.client.get("/p/files/site/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "<h1>hi</h1>")
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertEqual(response.headers["cache-control"], "no-cache")

    def test_a_file_gets_its_type_from_the_name(self) -> None:
        response = self.client.get("/p/files/site/app.css")
        self.assertEqual(response.text, "h1{}")
        self.assertTrue(response.headers["content-type"].startswith("text/css"))

    def test_a_folder_without_its_slash_redirects(self) -> None:
        response = self.client.get("/p/files/site")
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response.headers["location"], "/p/files/site/")

    def test_head_sends_headers_only(self) -> None:
        response = self.client.head("/p/files/site/app.css")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-length"], "4")
        self.assertEqual(response.content, b"")

    def test_a_missing_file_is_a_404(self) -> None:
        self.assertEqual(self.client.get("/p/files/nope.txt").status_code, 404)
