import asyncio
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from core.config import BASE_DIR
from core.process_guard import (
    PORT,
    claim_pid_file,
    metadata_matches_process,
    release_pid_file,
    stop_local_tavern,
    wait_for_stop_request,
    write_stop_request,
)


class ProcessGuardTests(unittest.TestCase):
    def test_metadata_requires_project_command_and_listening_pid(self):
        metadata = {"pid": 4321, "port": PORT, "project_root": str(BASE_DIR)}
        self.assertTrue(metadata_matches_process(
            metadata,
            command_line="python -m uvicorn server:app --port 8765",
            listening_pids={4321},
        ))
        self.assertFalse(metadata_matches_process(
            metadata,
            command_line="python long_running_job.py",
            listening_pids={4321},
        ))
        self.assertFalse(metadata_matches_process(
            metadata,
            command_line="python -m uvicorn server:app --port 8765",
            listening_pids={9999},
        ))

    def test_pid_claim_is_exclusive_and_release_is_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = Path(tmp) / "tavern.pid"
            stop_path = Path(tmp) / "tavern.stop.pid"
            metadata = claim_pid_file(pid_path)
            self.assertEqual(json.loads(pid_path.read_text(encoding="utf-8"))["pid"], metadata["pid"])
            with self.assertRaises(RuntimeError):
                claim_pid_file(pid_path)
            release_pid_file(metadata, pid_path, stop_path)
            self.assertFalse(pid_path.exists())

    def test_unrelated_python_process_is_not_stopped(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pid_path = Path(tmp) / "tavern.pid"
                stop_path = Path(tmp) / "tavern.stop.pid"
                pid_path.write_text(json.dumps({
                    "pid": process.pid,
                    "token": "test-token",
                    "port": PORT,
                    "project_root": str(BASE_DIR),
                }), encoding="utf-8")
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = stop_local_tavern(
                        pid_path=pid_path,
                        stop_path=stop_path,
                        timeout_seconds=0.1,
                    )
                self.assertEqual(result, 3)
                self.assertIn("未结束任何进程", output.getvalue())
                self.assertIsNone(process.poll())
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_batch_script_has_no_broad_python_kill(self):
        text = (BASE_DIR / "stop_tavern.bat").read_text(encoding="utf-8").lower()
        self.assertNotIn("imagename eq python.exe", text)
        self.assertNotIn("findstr \"listening\"", text)
        self.assertIn("core.process_guard stop", text)


class StopRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_matching_token_triggers_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop_path = Path(tmp) / "tavern.stop.pid"
            metadata = {"pid": 1234, "token": "owned-token"}
            called = []
            write_stop_request(metadata, stop_path)
            with self.assertLogs("core.process_guard", level="INFO") as logs:
                await asyncio.wait_for(
                    wait_for_stop_request(
                        metadata,
                        path=stop_path,
                        poll_seconds=0.01,
                        on_stop=lambda: called.append(True),
                    ),
                    timeout=1,
                )
            self.assertEqual(called, [True])
            self.assertTrue(any("1234" in entry for entry in logs.output))
            self.assertFalse(stop_path.exists())


if __name__ == "__main__":
    unittest.main()
