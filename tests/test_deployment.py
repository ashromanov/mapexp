"""Deployment transaction checks without a live Docker daemon or server credentials."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.release = self.root / "releases" / ("a" * 40)
        self.release.mkdir(parents=True)
        for name in ["deploy.sh", "compose.production.yml"]:
            shutil.copy2(ROOT / "deploy" / name, self.release / name)
        (self.root / ".env").write_text("CITY_NAME=Test\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake = self.bin / "docker"
        fake.write_text("""#!/usr/bin/env python3
import json,os,sys
with open(os.environ['DOCKER_TRACE'],'a') as f:
    f.write(json.dumps({'args':sys.argv[1:],'image':os.environ['MAPEXP_IMAGE']})+'\\n')
if os.getenv('FAIL_NEW') and 'up' in sys.argv and os.environ['MAPEXP_IMAGE'].endswith('a'*40):
    sys.exit(42)
""")
        fake.chmod(0o755)
        curl = self.bin / "curl"
        curl.write_text('#!/bin/sh\nprintf "{\\"status\\":\\"ok\\"}"\n')
        curl.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "MAPEXP_ROOT": str(self.root),
            "MAPEXP_REVISION": "a" * 40,
            "MAPEXP_IMAGE": "ghcr.io/ashromanov/mapexp:" + ("a" * 40),
            "DOCKER_TRACE": str(self.root / "trace.jsonl"),
        }

    def calls(self):
        return [
            json.loads(line)
            for line in (self.root / "trace.jsonl").read_text().splitlines()
        ]

    def test_first_deployment_backs_up_before_start_and_switches_current(self):
        result = subprocess.run(
            ["bash", str(self.release / "deploy.sh")],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "current").resolve(), self.release)
        calls = self.calls()
        backup = next(i for i, c in enumerate(calls) if c["args"][0] == "run")
        first_up = next(i for i, c in enumerate(calls) if "up" in c["args"])
        self.assertLess(backup, first_up)
        self.assertIn("stop", calls[0]["args"])

    def test_failed_release_recovers_previous_image_without_switching_current(self):
        previous = self.root / "releases" / ("b" * 40)
        previous.mkdir()
        shutil.copy2(
            self.release / "compose.production.yml", previous / "compose.production.yml"
        )
        old_image = "ghcr.io/ashromanov/mapexp:" + ("b" * 40)
        (previous / ".image").write_text(old_image + "\n")
        (self.root / "current").symlink_to(previous)
        result = subprocess.run(
            ["bash", str(self.release / "deploy.sh")],
            env={**self.env, "FAIL_NEW": "1"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual((self.root / "current").resolve(), previous)
        recoveries = [
            call
            for call in self.calls()
            if "up" in call["args"] and call["image"] == old_image
        ]
        self.assertEqual(len(recoveries), 1)
        self.assertIn("ingest", recoveries[0]["args"])

    def test_forced_ssh_key_rejects_arbitrary_commands_and_revision_injection(self):
        script = ROOT / "deploy/remote-command.sh"
        for command in [
            "bash",
            "deploy main",
            "deploy " + "a" * 40 + " extra",
            "deploy $(id)",
            "deploy " + "a" * 40 + "/../",
        ]:
            result = subprocess.run(
                ["bash", str(script)],
                env={**self.env, "SSH_ORIGINAL_COMMAND": command},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.root / "trace.jsonl").exists())
        ping = subprocess.run(
            ["bash", str(script)],
            env={**self.env, "SSH_ORIGINAL_COMMAND": "ping"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(ping.returncode, 0)
        self.assertIn("mapexp-deploy-ready", ping.stdout)
