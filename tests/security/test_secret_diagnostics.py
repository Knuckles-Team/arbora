"""Value-free secret diagnostics (ARBORA-DEPLOY-R602.4).

Moved from the deployment repository with the module: no API accepts a secret
value, and an environment is read by membership only.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from arbora.security import secret_diagnostics as diagnostics

pytestmark = pytest.mark.spec("ARBORA-DEPLOY-R602.4")


class SecretDiagnosticContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.diagnostics = diagnostics
        self.env = self.diagnostics.SourceKind.ENVIRONMENT
        self.ref = self.diagnostics.SourceKind.SECRET_REF

    def test_environment_membership_only(self) -> None:
        d = self.diagnostics
        report = d.diagnose(
            [d.AllowedSecret("OIDC_CLIENT_SECRET", self.env)],
            environment_names={"OIDC_CLIENT_SECRET"},
        )

        self.assertEqual(report.status, "complete")
        self.assertEqual(
            report.to_dict(),
            {
                "schema_version": "secret-diagnostics.v1",
                "status": "complete",
                "entries": [
                    {
                        "name": "OIDC_CLIENT_SECRET",
                        "source_kind": "environment",
                        "presence": "present",
                        "health": "unknown",
                    }
                ],
            },
        )
        rendered = json.dumps(report.to_dict())
        for forbidden in ("value", "length", "hash", "prefix", "rendered_env"):
            self.assertNotIn(forbidden, rendered)

    def test_secret_ref_health_is_status_only_and_missing_is_incomplete(self) -> None:
        d = self.diagnostics
        complete = d.diagnose(
            [d.AllowedSecret("arbora-secrets", self.ref)],
            secret_refs=[d.SecretRefObservation("arbora-secrets", True, True)],
        )
        self.assertEqual(complete.entries[0].presence, d.Presence.PRESENT)
        self.assertEqual(complete.entries[0].health, d.Health.HEALTHY)

        incomplete = d.diagnose(
            [d.AllowedSecret("arbora-secrets", self.ref)],
        )
        self.assertEqual(incomplete.status, "incomplete")
        self.assertEqual(incomplete.entries[0].presence, d.Presence.UNKNOWN)
        self.assertEqual(incomplete.entries[0].health, d.Health.UNKNOWN)

    def test_allowlist_and_observation_boundaries_fail_closed(self) -> None:
        d = self.diagnostics
        with self.assertRaisesRegex(d.DiagnosticInputError, "allowlist-required"):
            d.diagnose([])
        with self.assertRaisesRegex(d.DiagnosticInputError, "allowlist-duplicate"):
            d.diagnose(
                [
                    d.AllowedSecret("arbora-secrets", self.ref),
                    d.AllowedSecret("arbora-secrets", self.ref),
                ]
            )
        with self.assertRaisesRegex(
            d.DiagnosticInputError, "observation-unallowlisted"
        ):
            d.diagnose(
                [d.AllowedSecret("arbora-secrets", self.ref)],
                secret_refs=[d.SecretRefObservation("other-ref", False, None)],
            )
        with self.assertRaisesRegex(d.DiagnosticInputError, "observation-invalid"):
            d.SecretRefObservation("arbora-secrets", False, True)

    def test_no_api_accepts_a_secret_value(self) -> None:
        d = self.diagnostics
        with self.assertRaisesRegex(d.DiagnosticInputError, "sensitive-field"):
            d.allowlist_from_json(
                [
                    {
                        "name": "OIDC_CLIENT_SECRET",
                        "source_kind": "environment",
                        "token": "omitted",
                    }
                ]
            )
        with self.assertRaisesRegex(d.DiagnosticInputError, "observation-invalid"):
            d.observations_from_json([{"name": "arbora-secrets", "present": True}])

    def test_scanner_has_fixed_outcomes_and_no_content_echo(self) -> None:
        d = self.diagnostics
        self.assertEqual(
            d.scan_text_artifact("rollout completed").status,
            d.ArtifactStatus.SAFE,
        )
        rejected = d.scan_text_artifact("OIDC_CLIENT_SECRET=omitted")
        self.assertEqual(rejected.status, d.ArtifactStatus.SECRET_LIKE_CONTENT)
        self.assertNotIn("omitted", json.dumps(rejected.to_dict()))
        self.assertEqual(
            d.scan_text_artifact("x" * (d.MAX_ARTIFACT_BYTES + 1)).status,
            d.ArtifactStatus.TOO_LARGE,
        )
        self.assertEqual(
            d.scan_text_artifact("\x00").status,
            d.ArtifactStatus.INVALID,
        )

    def test_file_scanner_is_bounded(self) -> None:
        d = self.diagnostics
        with TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.log"
            path.write_text("Authorization: Bearer omitted", encoding="utf-8")
            self.assertEqual(
                d.scan_artifact_file(path).status,
                d.ArtifactStatus.SECRET_LIKE_CONTENT,
            )


def test_the_console_routes_secrets_diagnose_to_the_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sys

    from arbora import console

    seen: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        seen.append(list(argv))
        return 0

    monkeypatch.setattr(diagnostics, "main", fake_main)
    allowlist = tmp_path / "allow.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["arbora", "secrets", "diagnose", "--allowlist-file", str(allowlist)],
    )
    with pytest.raises(SystemExit) as exit_info:
        console.main()
    assert exit_info.value.code == 0
    assert seen == [["--allowlist-file", str(allowlist)]]
