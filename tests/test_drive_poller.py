import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
POLL_SCRIPT = REPO_ROOT / "fetch_drive_recordings.sh"


def shell_quote(value):
    return shlex.quote(str(value))


def sanitize_drive_name(name):
    stem = name[:-4] if name.endswith(".mp4") else name
    return stem.translate(str.maketrans({" ": "-", "/": "-", ":": "-"})) + ".mp4"


class TestDrivePoller(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.output_dir = self.root / "out"
        self.log_file = self.output_dir / "auto_transcribe.log"
        self.list_json = self.root / "drive-list.json"
        self.gws_bin = self.root / "fake-gws.sh"
        self.enqueue_script = self.root / "fake-enqueue.sh"
        self.enqueue_calls = self.root / "enqueue-calls.txt"
        self.env_file = self.root / "poller.env"

        self.write_drive_list([])
        self.write_fake_gws()
        self.write_fake_enqueue()
        self.write_env()

    def tearDown(self):
        self.tmp.cleanup()

    def write_drive_list(self, files):
        self.list_json.write_text(json.dumps({"files": files}), encoding="utf-8")

    def write_fake_gws(self):
        self.gws_bin.write_text(
            f"""#!/bin/bash
set -e

if [ "$1" = "drive" ] && [ "$2" = "list" ]; then
    cat {shell_quote(self.list_json)}
    exit 0
fi

if [ "$1" = "drive" ] && [ "$2" = "download" ]; then
    file_id="$3"
    shift 3
    output=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --output)
                output="$2"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done
    [ -n "$output" ] || exit 3
    printf 'downloaded:%s\\n' "$file_id" > "$output"
    exit 0
fi

printf 'unexpected fake gws args:' >&2
printf ' %s' "$@" >&2
printf '\\n' >&2
exit 2
""",
            encoding="utf-8",
        )
        self.gws_bin.chmod(0o755)

    def write_fake_enqueue(self, exit_code=0):
        self.enqueue_script.write_text(
            f"""#!/bin/bash
printf '%s\\n' "$1" >> {shell_quote(self.enqueue_calls)}
exit {exit_code}
""",
            encoding="utf-8",
        )
        self.enqueue_script.chmod(0o755)

    def write_env(self):
        values = {
            "OUTPUT_DIR": self.output_dir,
            "LOG_FILE": self.log_file,
            "DRIVE_FOLDER_ID": "folder-id",
            "GWS_BIN": self.gws_bin,
            "ENQUEUE_SCRIPT": self.enqueue_script,
            "DRIVE_NOTIFY": "0",
            "STAGING_RETENTION_SECONDS": "7200",
        }
        self.env_file.write_text(
            "\n".join(f"{key}={shell_quote(value)}" for key, value in values.items()) + "\n",
            encoding="utf-8",
        )

    def run_poller(self, **env_overrides):
        env = os.environ.copy()
        env["ENV_FILE"] = str(self.env_file)
        env.update({key: str(value) for key, value in env_overrides.items()})
        return subprocess.run(
            ["bash", str(POLL_SCRIPT)],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def log_text(self):
        return self.log_file.read_text(encoding="utf-8")

    def test_dry_run_filters_to_mp4_without_touching_ledger(self):
        self.write_drive_list([
            {"id": "video-1", "name": "Team / sync: 2026", "mime_type": "video/mp4"},
            {
                "id": "notes-1",
                "name": "Team / sync: 2026 - Notes",
                "mime_type": "application/vnd.google-apps.document",
            },
        ])

        result = self.run_poller(DRY_RUN="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DRY_RUN: would download video-1", self.log_text())
        self.assertIn("Poll complete: 1 new, 0 already handled.", self.log_text())
        self.assertFalse(self.enqueue_calls.exists())
        self.assertFalse((self.output_dir / ".drive_done" / "video-1").exists())
        self.assertEqual(list((self.output_dir / ".staging").glob("*.mp4")), [])

    def test_downloads_enqueues_and_writes_ledger_marker(self):
        file_name = "Team / sync: 2026"
        self.write_drive_list([
            {"id": "video-1", "name": file_name, "mime_type": "video/mp4"},
        ])

        result = self.run_poller()

        self.assertEqual(result.returncode, 0, result.stderr)
        staged_path = self.output_dir / ".staging" / sanitize_drive_name(file_name)
        ledger_marker = self.output_dir / ".drive_done" / "video-1"
        self.assertEqual(staged_path.read_text(encoding="utf-8"), "downloaded:video-1\n")
        self.assertEqual(self.enqueue_calls.read_text(encoding="utf-8").strip(), str(staged_path))
        self.assertTrue(ledger_marker.exists())
        self.assertIn(f"name={file_name}", ledger_marker.read_text(encoding="utf-8"))
        self.assertIn("Poll complete: 1 new, 0 already handled.", self.log_text())

    def test_existing_ledger_marker_skips_download_and_enqueue(self):
        self.write_drive_list([
            {"id": "video-1", "name": "Already handled", "mime_type": "video/mp4"},
        ])
        ledger_dir = self.output_dir / ".drive_done"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "video-1").write_text("seeded\n", encoding="utf-8")

        result = self.run_poller()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.enqueue_calls.exists())
        self.assertEqual(list((self.output_dir / ".staging").glob("*.mp4")), [])
        self.assertIn("Poll complete: 0 new, 1 already handled.", self.log_text())

    def test_enqueue_failure_does_not_write_ledger_and_removes_staged_file(self):
        self.write_fake_enqueue(exit_code=42)
        self.write_drive_list([
            {"id": "video-1", "name": "Will fail", "mime_type": "video/mp4"},
        ])

        result = self.run_poller()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.output_dir / ".drive_done" / "video-1").exists())
        self.assertEqual(list((self.output_dir / ".staging").glob("*.mp4")), [])
        self.assertIn("Poll complete: 0 new, 0 already handled, 1 errors.", self.log_text())


if __name__ == "__main__":
    unittest.main()
