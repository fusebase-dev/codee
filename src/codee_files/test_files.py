import tempfile
import unittest
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient

from codee_files.plugin import FilesPlugin


class FilesPluginTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "files"
        (self.root / "site").mkdir(parents=True)
        (self.root / "site" / "index.html").write_text("<h1>hi</h1>")
        (self.root / "site" / "app.css").write_text("h1{}")
        (Path(temporary.name) / "secret.txt").write_text("no")

        plugin = FilesPlugin()
        plugin.data_dir = self.root
        self.settings: dict[str, str] = {}

        def serve(request):
            response = plugin.serve_http(
                request, request.path_params["path"], self.settings)
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

    def test_missing_files_and_escapes_are_404(self) -> None:
        self.assertEqual(self.client.get("/p/files/nope.txt").status_code, 404)
        self.assertEqual(
            self.client.get("/p/files/..%2Fsecret.txt").status_code, 404)

    def test_the_folder_setting_wins_over_the_default(self) -> None:
        other = self.root.parent / "other"
        other.mkdir()
        (other / "a.txt").write_text("other")
        self.settings["folder"] = str(other)
        self.assertEqual(self.client.get("/p/files/a.txt").text, "other")
        self.assertEqual(self.client.get("/p/files/site/").status_code, 404)

    def test_the_card_shows_the_folder_it_serves(self) -> None:
        plugin = FilesPlugin()
        plugin.data_dir = self.root
        self.assertIn(f"`{self.root.resolve()}/<name>/index.html`",
                      plugin.describe({}))
        self.assertIn("`/srv/x`", plugin.describe({"folder": "/srv/x"}))
        self.assertNotIn("{FOLDER}", plugin.describe({}))
