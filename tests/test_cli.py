import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from mercury.__main__ import main


class CLITests(unittest.TestCase):
    def call(self, args, code=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = main(args)
        self.assertEqual(result, code, err.getvalue())
        return json.loads(out.getvalue() if result == 0 else err.getvalue())

    def test_demo(self):
        result = self.call(["demo"])
        self.assertTrue(result["synthetic"])
        self.assertTrue(result["audit"]["ok"])

    def test_persistent_cli_flow(self):
        with tempfile.TemporaryDirectory() as d:
            base = ["--db", str(Path(d) / "paper.db")]
            self.call(base + ["init", "--start", "2026-01-05T15:00:00Z"])
            f = Path(d) / "q.json"
            f.write_text(json.dumps({"ticker": "TEST", "price": "100", "asof": "2026-01-05T15:00:00Z", "evidence": {"synthetic": True}}))
            self.call(base + ["import-fixture", str(f)])
            order = self.call(base + ["buy", "TEST", "10", "--request-id", "cli-1"])
            self.assertEqual(order["status"], "open")
            self.call(base + ["cancel", order["id"]])
            self.assertEqual(self.call(base + ["orders"])[0]["status"], "cancelled")
            self.assertTrue(self.call(base + ["audit"])["ok"])
            self.call(base + ["init", "--start", "2026-01-05T15:00:00Z"], code=2)


if __name__ == "__main__":
    unittest.main()
