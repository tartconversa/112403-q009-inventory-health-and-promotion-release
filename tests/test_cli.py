"""命令行与样例数据的端到端检查。"""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"

sys.path.insert(0, str(ROOT))

from inventory_app.__main__ import main  # noqa: E402


class CommandLineTests(unittest.TestCase):
    def run_cli(self, *args):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(list(args))
        return code, buffer.getvalue()

    def test_text_review_on_samples(self):
        code, output = self.run_cli("review", "--data", str(SAMPLES), "--as-of", "2026-10-01")
        self.assertEqual(code, 0)
        self.assertIn("库存健康与营销资源评审", output)
        self.assertIn("门店·ST1", output)

    def test_json_review_is_machine_readable(self):
        code, output = self.run_cli(
            "review", "--data", str(SAMPLES), "--as-of", "2026-10-01", "--format", "json"
        )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["as_of"], "2026-10-01")
        self.assertTrue(payload["inventory"])
        self.assertIn("allocations", payload["marketing"])

    def test_event_review_reports_deltas(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.csv"
            self.run_cli("sample-events", "--out", str(events))
            code, output = self.run_cli(
                "review", "--data", str(SAMPLES), "--as-of", "2026-10-01",
                "--events", str(events), "--format", "json",
            )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertIn("revision", payload)
        self.assertEqual(len(payload["revision"]["events_applied"]), 3)

    def test_missing_directory_is_reported(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["review", "--data", "/nonexistent", "--as-of", "2026-10-01"])
        self.assertEqual(code, 2)
        self.assertIn("找不到输入文件", stderr.getvalue())


class ModuleEntrypointTests(unittest.TestCase):
    def test_python_dash_m_entrypoint(self):
        result = subprocess.run(
            [sys.executable, "-m", "inventory_app", "review", "--data", str(SAMPLES),
             "--as-of", "2026-10-01", "--format", "json"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["inventory"])


if __name__ == "__main__":
    unittest.main()
