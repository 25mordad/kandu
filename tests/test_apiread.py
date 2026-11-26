import io
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import yaml

from kandu import apiread

ROOT = Path(__file__).resolve().parent.parent


class ApiReadTest(unittest.TestCase):
    """`bin/kandu-api PATH`: the job check reads Kandu's API with it, by a GET to one path under the API only."""

    def setUp(self):
        seen = self.seen = []

        class Api(BaseHTTPRequestHandler):
            def _answer(self):
                seen.append((self.command, self.path))
                body = b'{"boards": []}' if self.path == "/api/v1/boards" else b"no such path"
                self.send_response(200 if self.path == "/api/v1/boards" else 404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_DELETE = _answer

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Api)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name) / "local"
        self.local.mkdir()
        shutil.copy(ROOT / "config.example" / "projects.example.yaml", self.local / "projects.yaml")
        settings = yaml.safe_load((ROOT / "config.example" / "kandu.example.yaml").read_text(encoding="utf-8"))
        settings["dashboard"]["port"] = self.server.server_port
        (self.local / "kandu.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")

    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = apiread.main(list(args), local_dir=self.local)
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_a_path_under_the_api_is_read_by_a_get(self):
        self.assertEqual(self.main("boards"), (0, '{"boards": []}', ""))
        self.assertEqual(self.seen, [("GET", "/api/v1/boards")])

    def test_a_path_the_api_does_not_serve_exits_1_with_its_answer(self):
        code, out, err = self.main("tasks/acme/app/3")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("404: no such path", err)
        self.assertEqual(self.seen, [("GET", "/api/v1/tasks/acme/app/3")])

    def test_it_takes_no_address_file_flag_query_or_second_argument(self):
        for args in (["http://example.com/x"], ["file:///etc/hostname"], ["../../etc"], ["boards/../state"],
                     ["boards?x=1"], ["boards", "file:///etc/hostname"], ["boards", "-o", "/tmp/x"],
                     ["-d", "@/etc/hostname", "boards"], ["boards x"], [""]):
            with self.subTest(args=args):
                code, out, err = self.main(*args)
                self.assertNotEqual(code, 0)
                self.assertEqual(out, "")
        self.assertEqual(self.seen, [])

    def test_the_command_runs_from_any_folder(self):
        script = ROOT / "bin" / "kandu-api"
        self.assertTrue(script.stat().st_mode & 0o111)
        self.assertIn("-m kandu.apiread", script.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
