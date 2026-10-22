"""RED regressions for F05/F14, using the current ignore and documented gate.

Run from the repository root:
    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover \
        -s tests -p 'test_repository_guards.py' -v

Temporary repositories and harmless shell fixtures keep the real index,
operational scripts and user configuration outside the test's write boundary.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

import _bootstrap  # noqa: F401
from _bootstrap import ROOT


SYNC_SCRIPT = "sync/monitoramento-sync.sh"
SYNC_INSTALLER = "sync/install-monitoramento-sync.sh"
SYNC_DISPATCHER = "sync/99-monitoramento-sync"
OPS_LAUNCHER = "ops/launch_codex_rpi.sh"
REQUIRED_SHELL_SCRIPTS = (
    OPS_LAUNCHER,
    "ops/setup_codex_rpi_lan.sh",
    "ops/setup_codex_rpi_ssh.sh",
    "ops/validate_shell.sh",
    SYNC_INSTALLER,
    SYNC_SCRIPT,
    SYNC_DISPATCHER,
)
EXECUTION_MARKER = "REPOSITORY_GUARD_SCRIPT_WAS_EXECUTED"
VALID_SHELL = f"#!/bin/bash\nprintf '%s\\n' '{EXECUTION_MARKER}'\nexit 73\n"
INVALID_SHELL = "#!/bin/bash\nif then\n"


def isolated_tool_environment() -> dict[str, str]:
    """Exclude inherited Git overrides, shell startup files and user excludes."""
    return {
        "PATH": os.defpath,
        "LC_ALL": "C",
        "TZ": "UTC",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def required_tool(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise AssertionError(f"Required local test dependency unavailable: {name}")
    return executable


def current_shell_scripts() -> tuple[Path, ...]:
    return (
        *sorted(ROOT.glob("ops/*.sh")),
        *sorted(ROOT.glob("sync/*.sh")),
        ROOT / SYNC_DISPATCHER,
    )


@dataclass(frozen=True)
class IgnoreDecision:
    source: str
    pattern: str

    @property
    def ignored(self) -> bool:
        return bool(self.source) and not self.pattern.startswith("!")


class IgnoreRepository:
    """Real temporary Git repository containing only the current ignore rules."""

    def __init__(self, directory: Path) -> None:
        self.root = directory
        self.git = required_tool("git")
        self.environment = isolated_tool_environment()
        initialized = self.run_git(["init", "--quiet", "--template="])
        if initialized.returncode != 0:
            raise AssertionError(f"Temporary git init failed: {initialized.stderr}")
        (self.root / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())

    def run_git(
        self, arguments: list[str], stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.git, "-c", f"core.excludesFile={os.devnull}", *arguments],
            cwd=self.root, env=self.environment, input=stdin, text=True,
            capture_output=True, timeout=5, check=False,
        )

    def decisions(self, paths: tuple[str, ...]) -> dict[str, IgnoreDecision]:
        result = self.run_git(
            ["check-ignore", "--no-index", "--verbose", "--non-matching", "-z", "--stdin"],
            "\0".join(paths) + "\0",
        )
        if result.returncode not in (0, 1):
            raise AssertionError(f"git check-ignore failed: {result.stderr}")
        fields = result.stdout.split("\0")[:-1]
        if len(fields) != 4 * len(paths):
            raise AssertionError(f"Unexpected Git response for {paths!r}: {result.stdout!r}")
        return {
            fields[index + 3]: IgnoreDecision(fields[index], fields[index + 2])
            for index in range(0, len(fields), 4)
        }


class TestGitignoreProtection(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="fatigue-ignore-red-")
        self.addCleanup(temporary.cleanup)
        self.repository = IgnoreRepository(Path(temporary.name))

    def assert_ignored_by_root(self, paths: tuple[str, ...]) -> None:
        decisions = self.repository.decisions(paths)
        protected = {
            path for path, decision in decisions.items()
            if decision.ignored and decision.source == ".gitignore"
        }
        self.assertEqual(
            set(paths), protected,
            f"F05: expected root .gitignore protection; received {decisions!r}",
        )

    def assert_versionable(self, paths: tuple[str, ...]) -> None:
        decisions = self.repository.decisions(paths)
        ignored = {path for path, decision in decisions.items() if decision.ignored}
        self.assertEqual(set(), ignored, f"Legitimate versioned paths hidden: {decisions!r}")

    def test_local_environment_and_bytecode_are_ignored_by_root_rules(self) -> None:
        self.assert_ignored_by_root((
            ".venv/probe.py", "__pycache__/probe.pyc",
            "tests/__pycache__/probe.pyc", "package/probe.pyc", ".ai-jail",
        ))

    def test_sensitive_environment_files_are_ignored_even_in_evidence(self) -> None:
        self.assert_ignored_by_root((
            ".env", ".env.production", "subsystem/.env",
            "docs/evidencias/session/.env.production", "tests/fixtures/.env",
        ))

    def test_pycache_directories_ignore_files_independently_of_extension(self) -> None:
        # F05: *.pyc alone must not mask loss of the directory protection.
        self.assert_ignored_by_root((
            "package/__pycache__/probe.cpython-314.pyc.123456",
            "__pycache__/probe",
            "tests/__pycache__/probe.txt",
        ))

    def test_root_logs_runs_and_local_calibration_are_ignored(self) -> None:
        self.assert_ignored_by_root((
            "logs/events/probe.json", "runs/probe.json", "config/eye_calibration.json",
        ))

    def test_generated_videos_outside_evidence_and_fixtures_are_ignored(self) -> None:
        self.assert_ignored_by_root((
            "capture.mp4", "capture.avi", "capture.264", "recordings/capture.mp4",
        ))

    def test_sources_examples_skills_and_configuration_docs_remain_versionable(self) -> None:
        self.assert_versionable((
            ".gitignore", "AGENTS.md", "CLAUDE.md", "README.md", "Dockerfile",
            "docker-compose.yml", "config/README.md", "face_landmarker.task",
            "tests/test_repository_guards.py", OPS_LAUNCHER, SYNC_DISPATCHER,
            ".agents/skills/agentic-test-engineering/SKILL.md",
            ".env.example", ".env.production.example", "subsystem/.env.example",
        ))

    def test_historical_logs_and_video_evidence_remain_versionable(self) -> None:
        self.assert_versionable((
            "docs/evidencias/session/replay-logs/runs/run/frames.jsonl",
            "docs/evidencias/session/logs/events.json",
            "docs/evidencias/session/black.avi", "docs/evidencias/session/replay.mp4",
            "docs/evidencias/session/replay.264", "tests/fixtures/replay.avi",
            "tests/fixtures/nested/replay.mp4", "tests/fixtures/nested/replay.264",
        ))

    def test_check_ignore_does_not_change_an_existing_tracked_file(self) -> None:
        (self.repository.root / ".env").write_text("EXAMPLE_ONLY=synthetic\n", encoding="utf-8")
        added = self.repository.run_git(["add", "--force", "--", ".env"])
        self.assertEqual(0, added.returncode, added.stderr)
        before = self.repository.run_git(["ls-files", "--stage", "--", ".env"])
        self.repository.decisions((".env",))
        after = self.repository.run_git(["ls-files", "--stage", "--", ".env"])
        self.assertTrue(before.stdout.endswith("\t.env\n"), before)
        self.assertEqual(before.stdout, after.stdout)


class ShellValidationTree:
    """Use the documented command with harmless fixtures at existing script paths."""

    def __init__(self, directory: Path, omit: str | None = None) -> None:
        self.root = directory
        self.root.mkdir(parents=True, exist_ok=True)
        self.bash = required_tool("bash")
        self.environment = isolated_tool_environment()
        lines = (ROOT / "TESTE REGRECAO.txt").read_text(encoding="utf-8").splitlines()
        commands = [shlex.split(line.strip()) for line in lines if line.strip().startswith("bash ")]
        if len(commands) != 1:
            raise AssertionError(f"Expected one documented shell gate; received {commands!r}")
        self.command = [self.bash, *commands[0][1:]]
        for source in current_shell_scripts():
            relative = str(source.relative_to(ROOT))
            if relative == omit:
                continue
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            # Only a documented checker entry point may run. Operational script
            # bodies are replaced so a broken gate cannot perform real actions.
            content = source.read_text(encoding="utf-8") if relative == self.command[1] else VALID_SHELL
            target.write_text(content, encoding="utf-8")

    def write_script(self, relative: str, content: str) -> None:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def run_command(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command, cwd=self.root, env=self.environment, text=True,
            capture_output=True, timeout=5, check=False,
        )

    def check_one(self, relative: str) -> subprocess.CompletedProcess[str]:
        return self.run_command([self.bash, "-n", "--", relative])

    def run_canonical(self) -> subprocess.CompletedProcess[str]:
        return self.run_command(self.command)

    def run_selected(self, paths: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        # Preserve Bash and the documented checking option/entry point; only
        # replace its input list, keeping explicit paths as separate argv items.
        return self.run_command([*self.command[:2], *paths])


class ShellSyntaxProbe:
    """Record syntax-check argv while delegating to the real local Bash."""

    def __init__(self, directory: Path, bash: str) -> None:
        self.binary_directory = directory / "syntax-probe-bin"
        self.binary_directory.mkdir()
        self.arguments_file = directory / "syntax-probe-arguments.bin"
        wrapper = self.binary_directory / "bash"
        wrapper.write_text(
            f"#!{bash}\n"
            "set -eu\n"
            'printf \'%s\\0\' "$@" >> "$FATIGUE_SHELL_SYNTAX_ARGV"\n'
            f'exec {shlex.quote(bash)} "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o700)

    def environment(self, original: dict[str, str]) -> dict[str, str]:
        return dict(
            original,
            PATH=f"{self.binary_directory}{os.pathsep}{original['PATH']}",
            FATIGUE_SHELL_SYNTAX_ARGV=str(self.arguments_file),
        )

    def checked_scripts(self) -> tuple[str, ...]:
        if not self.arguments_file.exists():
            return ()
        fields = self.arguments_file.read_bytes().split(b"\0")
        if fields.pop() != b"" or len(fields) % 3:
            raise AssertionError(f"Unexpected syntax-check argv: {fields!r}; expected NUL-separated triples")
        checked: list[str] = []
        for index in range(0, len(fields), 3):
            if fields[index:index + 2] != [b"-n", b"--"]:
                raise AssertionError(f"Unexpected syntax-check argv: {fields!r}; expected -n, --, path")
            checked.append(os.fsdecode(fields[index + 2]))
        return tuple(checked)


class TestCanonicalShellValidation(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="fatigue shell red ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tree = ShellValidationTree(self.root)
        for argument in self.tree.command[1:]:
            if not argument.startswith("-"):
                self.assertTrue((self.root / argument).is_file(), f"Missing current gate input: {argument}")

    def assert_canonical_rejects_invalid_script(self, relative: str) -> None:
        self.tree.write_script(relative, INVALID_SHELL)
        individual = self.tree.check_one(relative)
        self.assertEqual(2, individual.returncode, f"Invalid fixture was not rejected: {individual}")
        canonical = self.tree.run_canonical()
        self.assertNotEqual(
            0, canonical.returncode,
            f"F14: documented gate accepted {relative!r}; individual bash -n returned "
            f"{individual.returncode}; canonical stderr={canonical.stderr!r}",
        )

    def test_documented_gate_accepts_all_valid_scripts(self) -> None:
        result = self.tree.run_canonical()
        self.assertEqual(0, result.returncode, result.stderr)

    def test_documented_gate_accepts_valid_scripts_with_cdpath(self) -> None:
        for relative in REQUIRED_SHELL_SCRIPTS:
            self.assertTrue((self.root / relative).is_file(), f"Missing required fixture: {relative}")
            individual = self.tree.check_one(relative)
            self.assertEqual(0, individual.returncode, individual.stderr)
        baseline = self.tree.run_canonical()
        self.assertEqual(0, baseline.returncode, baseline.stderr)
        self.tree.environment["CDPATH"] = "."
        result = self.tree.run_canonical()
        self.assertEqual(
            0, result.returncode,
            f"F14: valid scripts rejected with CDPATH=.; baseline exit=0; stderr={result.stderr!r}",
        )

    def test_documented_gate_checks_every_required_script_by_default(self) -> None:
        # F14: observe the actual syntax checks, including the checker itself.
        # NUL-separated argv preserves Unicode under LC_ALL=C without parsing xtrace.
        tree = ShellValidationTree(self.root / "seleção Unicode")
        probe = ShellSyntaxProbe(tree.root, tree.bash)
        tree.environment = probe.environment(tree.environment)
        result = tree.run_canonical()
        self.assertEqual(0, result.returncode, result.stderr)
        checked = probe.checked_scripts()
        expected = [str(tree.root / relative) for relative in REQUIRED_SHELL_SCRIPTS]
        self.assertCountEqual(
            expected, checked,
            f"F14: incomplete default selection; expected={expected!r}, checked={checked!r}",
        )

    def test_documented_gate_rejects_invalid_syntax_in_each_required_script(self) -> None:
        for index, relative in enumerate(REQUIRED_SHELL_SCRIPTS):
            with self.subTest(script=relative):
                tree = ShellValidationTree(self.root / f"invalid-required-{index}")
                self.assertTrue((tree.root / relative).is_file(), f"Missing required fixture: {relative}")
                tree.write_script(relative, INVALID_SHELL)
                individual = tree.check_one(relative)
                self.assertEqual(2, individual.returncode, individual.stderr)
                result = tree.run_canonical()
                self.assertNotEqual(
                    0, result.returncode,
                    f"F14: required script {relative!r} accepted; individual exit=2; gate={result!r}",
                )
                self.assertIn(relative, result.stderr)
                self.assertNotIn(EXECUTION_MARKER, result.stdout + result.stderr)

    def test_documented_gate_rejects_invalid_first_script(self) -> None:
        self.assert_canonical_rejects_invalid_script(SYNC_SCRIPT)

    def test_documented_gate_rejects_invalid_second_script(self) -> None:
        self.assert_canonical_rejects_invalid_script(SYNC_INSTALLER)

    def test_documented_gate_rejects_invalid_dispatcher_without_extension(self) -> None:
        self.assert_canonical_rejects_invalid_script(SYNC_DISPATCHER)

    def test_documented_gate_rejects_invalid_operations_script(self) -> None:
        self.assert_canonical_rejects_invalid_script(OPS_LAUNCHER)

    def test_documented_gate_reports_both_invalid_scripts(self) -> None:
        for relative in (SYNC_SCRIPT, SYNC_DISPATCHER):
            self.tree.write_script(relative, INVALID_SHELL)
            self.assertEqual(2, self.tree.check_one(relative).returncode, relative)
        result = self.tree.run_canonical()
        self.assertNotEqual(0, result.returncode, result)
        for relative in (SYNC_SCRIPT, SYNC_DISPATCHER):
            self.assertIn(relative, result.stderr, f"F14: unchecked script {relative!r}; stderr={result.stderr!r}")

    def test_documented_gate_rejects_deliberately_missing_second_input(self) -> None:
        tree = ShellValidationTree(self.root / "missing-input", omit=SYNC_INSTALLER)
        self.assertFalse((tree.root / SYNC_INSTALLER).exists())
        self.assertNotEqual(0, tree.check_one(SYNC_INSTALLER).returncode)
        result = tree.run_canonical()
        self.assertNotEqual(0, result.returncode, f"F14: missing existing gate input accepted: {result}")

    def test_selected_path_with_spaces_is_checked_without_execution(self) -> None:
        relative = "sync/fixture with spaces.sh"
        self.tree.write_script(relative, VALID_SHELL)
        result = self.tree.run_selected((relative,))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn(EXECUTION_MARKER, result.stdout + result.stderr)

    def test_selected_invalid_second_path_with_spaces_is_rejected(self) -> None:
        relative = "sync/invalid fixture with spaces.sh"
        self.tree.write_script(relative, INVALID_SHELL)
        self.assertEqual(2, self.tree.check_one(relative).returncode)
        result = self.tree.run_selected((SYNC_SCRIPT, relative))
        self.assertNotEqual(0, result.returncode, f"F14: invalid second path with spaces accepted: {result}")

    def test_documented_gate_does_not_execute_script_bodies(self) -> None:
        result = self.tree.run_canonical()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn(EXECUTION_MARKER, result.stdout + result.stderr)

    def test_existing_operational_scripts_individually_have_valid_syntax(self) -> None:
        for script in current_shell_scripts():
            with self.subTest(script=str(script.relative_to(ROOT))):
                result = self.tree.run_command([self.tree.bash, "-n", "--", str(script)])
                self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
