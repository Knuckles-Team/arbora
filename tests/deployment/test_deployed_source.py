"""The deployed-source manager is a published command (ARBORA-DEPLOY-R602)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from arbora.deployment import deployed_source as source_manager


def _run_git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(directory), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _run_manager(source: Path, worktree: Path, *arguments: str) -> dict[str, object]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "arbora.deployment.deployed_source",
            "--source-repo",
            str(source),
            "--worktree",
            str(worktree),
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return json.loads(result.stdout)


def _manager_process(
    source: Path, worktree: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "arbora.deployment.deployed_source",
            "--source-repo",
            str(source),
            "--worktree",
            str(worktree),
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


@pytest.mark.spec("ARBORA-DEPLOY-R602")
class DeployedSourceManagerTest(unittest.TestCase):
    def _new_source(self, root: Path) -> tuple[Path, str, str]:
        source = root / "source"
        source.mkdir()
        _run_git(source, "init", "-b", "main")
        _run_git(source, "config", "user.name", "test")
        _run_git(source, "config", "user.email", "test@example.invalid")
        (source / "state.txt").write_text("one\n", encoding="utf-8")
        _run_git(source, "add", "state.txt")
        _run_git(source, "commit", "-m", "one")
        first = _run_git(source, "rev-parse", "HEAD")
        (source / "state.txt").write_text("two\n", encoding="utf-8")
        _run_git(source, "add", "state.txt")
        _run_git(source, "commit", "-m", "two")
        second = _run_git(source, "rev-parse", "HEAD")
        return source, first, second

    def test_init_promote_and_rollback_follow_only_merged_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, initial, promoted = self._new_source(Path(temporary))
            worktree = Path(temporary) / "deployed" / "agent-utilities"

            before = _run_manager(source, worktree, "status")
            self.assertEqual(before["state"], "undecoupled")
            self.assertFalse(before["decoupled"])

            initialized = _run_manager(
                source,
                worktree,
                "init",
                "--initial-sha",
                initial,
            )
            self.assertEqual(initialized["state"], "ready")
            self.assertEqual(initialized["deployed"], initial)
            self.assertTrue(initialized["worktree_clean"])

            promoted_state = _run_manager(
                source,
                worktree,
                "promote",
                "--target-sha",
                promoted,
            )
            self.assertEqual(promoted_state["state"], "ready")
            self.assertEqual(promoted_state["deployed"], promoted)
            self.assertEqual(_run_git(worktree, "rev-parse", "HEAD"), promoted)

            rollback_state = _run_manager(
                source,
                worktree,
                "rollback",
                "--target-sha",
                initial,
                "--confirm-rollback",
            )
            self.assertEqual(rollback_state["state"], "ready")
            self.assertEqual(rollback_state["deployed"], initial)
            self.assertEqual(_run_git(source, "rev-parse", "refs/heads/main"), promoted)

    def test_init_requires_an_ancestor_of_main(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, _initial, _promoted = self._new_source(Path(temporary))
            worktree = Path(temporary) / "deployed" / "agent-utilities"
            orphan = Path(temporary) / "orphan"
            orphan.mkdir()
            _run_git(orphan, "init", "-b", "other")
            _run_git(orphan, "config", "user.name", "test")
            _run_git(orphan, "config", "user.email", "test@example.invalid")
            (orphan / "orphan.txt").write_text("orphan\n", encoding="utf-8")
            _run_git(orphan, "add", "orphan.txt")
            _run_git(orphan, "commit", "-m", "orphan")
            orphan_sha = _run_git(orphan, "rev-parse", "HEAD")
            _run_git(source, "fetch", str(orphan), "other:refs/heads/orphan")

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "arbora.deployment.deployed_source",
                    "--source-repo",
                    str(source),
                    "--worktree",
                    str(worktree),
                    "init",
                    "--initial-sha",
                    orphan_sha,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not an ancestor", result.stderr)

    def test_ensure_repairs_a_registered_but_missing_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, initial, _promoted = self._new_source(Path(temporary))
            worktree = Path(temporary) / "deployed" / "agent-utilities"
            _run_manager(source, worktree, "init", "--initial-sha", initial)

            # Removing the directory leaves Git's linked-worktree admin record
            # behind, exactly what happens when the shared checkout disk is
            # restored incompletely.
            shutil.rmtree(worktree)
            missing = _run_manager(source, worktree, "status")
            self.assertEqual(missing["state"], "worktree_missing")
            self.assertEqual(missing["deployed"], initial)

            repaired = _run_manager(source, worktree, "ensure")
            self.assertEqual(repaired["state"], "ready")
            self.assertTrue(repaired["worktree_exists"])
            self.assertEqual(repaired["deployed"], initial)
            self.assertEqual(
                _run_git(worktree, "rev-parse", "--abbrev-ref", "HEAD"), "deployed"
            )

    def test_unrelated_clone_is_rejected_by_git_common_dir_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, initial, _promoted = self._new_source(Path(temporary))
            linked = Path(temporary) / "deployed" / "agent-utilities"
            _run_manager(source, linked, "init", "--initial-sha", initial)
            unrelated = Path(temporary) / "unrelated"
            _run_git(source, "clone", str(source), str(unrelated))

            status = _manager_process(source, unrelated, "status")
            self.assertEqual(status.returncode, 0)
            snapshot = json.loads(status.stdout)
            self.assertEqual(snapshot["state"], "worktree_mismatch")
            self.assertFalse(snapshot["worktree_identity_valid"])
            self.assertTrue(snapshot["worktree_git_valid"])
            self.assertIn("different git-common-dir", snapshot["reason"])

            refused = _manager_process(source, unrelated, "ensure")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("different git-common-dir", refused.stderr)

    def test_registered_worktree_must_still_be_on_deployed_branch_and_head(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, initial, _promoted = self._new_source(Path(temporary))
            worktree = Path(temporary) / "deployed" / "agent-utilities"
            _run_manager(source, worktree, "init", "--initial-sha", initial)
            _run_git(worktree, "checkout", "-b", "unrelated-branch")

            snapshot = _run_manager(source, worktree, "status")
            self.assertEqual(snapshot["state"], "worktree_mismatch")
            self.assertEqual(snapshot["worktree_branch"], "refs/heads/unrelated-branch")
            self.assertIn("refs/heads/deployed", str(snapshot["reason"]))
            refused = _manager_process(source, worktree, "ensure")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("not checked out at refs/heads/deployed", refused.stderr)

    def test_symlink_is_rejected_before_path_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, _initial, _promoted = self._new_source(Path(temporary))
            target = Path(temporary) / "real-target"
            target.mkdir()
            link = Path(temporary) / "deployed-link"
            link.symlink_to(target, target_is_directory=True)

            refused = _manager_process(source, link, "status")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("must not be a symlink", refused.stderr)

    def test_failed_init_removes_new_ref_and_worktree_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, initial, _promoted = self._new_source(Path(temporary))
            worktree = Path(temporary) / "deployed" / "agent-utilities"

            with mock.patch.object(
                source_manager,
                "inspect",
                side_effect=RuntimeError("synthetic post-add failure"),
            ):
                with self.assertRaisesRegex(
                    source_manager.OperatorError, "initialization failed"
                ):
                    source_manager.initialize(source, worktree, initial)

            self.assertIsNone(source_manager._deployed_tip(source))
            self.assertNotIn(
                str(worktree), _run_git(source, "worktree", "list", "--porcelain")
            )
            self.assertFalse(worktree.exists())

    def test_failed_init_never_deletes_same_sha_created_after_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, initial, _promoted = self._new_source(Path(temporary))
            worktree = Path(temporary) / "deployed" / "agent-utilities"

            def create_competing_ref(
                _source: Path, _target: str, *, label: str
            ) -> None:
                # This hook runs after initialize()'s ref-absent preflight and
                # before its CAS. It models another operator winning the same
                # creation race with the exact same commit value.
                source_manager._git(
                    source,
                    "update-ref",
                    source_manager.PROMOTION_REF,
                    initial,
                    "",
                )

            with (
                mock.patch.object(
                    source_manager,
                    "_require_main_ancestor",
                    side_effect=create_competing_ref,
                ),
                self.assertRaises(source_manager.OperatorError),
            ):
                source_manager.initialize(source, worktree, initial)

            # The failed CAS did not own the ref, so cleanup must leave the
            # competing ref—even though its value equals our requested SHA.
            self.assertEqual(source_manager._deployed_tip(source), initial)
            self.assertNotIn(
                str(worktree), _run_git(source, "worktree", "list", "--porcelain")
            )

    def test_failed_init_preserves_ref_checked_out_by_competitor_after_cas(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, initial, _promoted = self._new_source(root)
            worktree = root / "deployed" / "agent-utilities"
            competitor = root / "competitor"
            real_ensure_parent = source_manager._ensure_parent

            def create_competing_worktree(path: Path) -> None:
                # initialize() has already won the ref CAS when this hook is
                # called.  The competitor then checks out the same ref at a
                # different path before initialize() can run its own add.
                real_ensure_parent(path)
                _run_git(
                    source,
                    "worktree",
                    "add",
                    str(competitor),
                    source_manager.PROMOTION_BRANCH,
                )

            with (
                mock.patch.object(
                    source_manager,
                    "_ensure_parent",
                    side_effect=create_competing_worktree,
                ),
                self.assertRaises(source_manager.OperatorError),
            ):
                source_manager.initialize(source, worktree, initial)

            # The requested add failed because the branch is checked out by a
            # different actor.  Cleanup must preserve that actor's ref and
            # registration rather than deleting the branch to zero SHA.
            self.assertEqual(source_manager._deployed_tip(source), initial)
            self.assertEqual(_run_git(competitor, "rev-parse", "HEAD"), initial)
            self.assertEqual(
                _run_git(competitor, "symbolic-ref", "--quiet", "HEAD"),
                source_manager.PROMOTION_REF,
            )
            records = _run_git(source, "worktree", "list", "--porcelain")
            self.assertIn(str(competitor), records)
            self.assertNotIn(str(worktree), records)
