import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "guards", Path(__file__).with_name("qualification_guards.py")
)
guards = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guards)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.wheels = self.root / "wheels"
        self.wheels.mkdir()
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        (self.bundle / "pytest.ini").write_text("[pytest]\n")
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(
            json.dumps({"pytest.ini": guards.digest(self.bundle / "pytest.ini")})
        )
        self.tuig = self.root / "tuig"
        self.tuig.write_bytes(b"fixture")
        hashes = {}
        for name in ("medulla", "medulla_stem", "arbora", "scion_sdk", "meristem"):
            p = self.wheels / f"{name}-1-py3-none-any.whl"
            p.write_bytes(name.encode())
            hashes[name] = guards.digest(p)
        self.bindings = self.root / "bindings.json"
        self.b = {
            "arbora": {
                "candidate_commit": "a" * 40,
                "local_wheel_sha256": hashes["arbora"],
            },
            "scion": {
                "candidate_commit": "b" * 40,
                "local_wheel_sha256": hashes["scion_sdk"],
            },
            "producer": {
                "client_wheel_sha256": hashes["medulla"],
                "server_wheel_sha256": hashes["medulla_stem"],
            },
            "meristem": {"wheel_sha256": hashes["meristem"]},
            "tuig": {"binary_sha256": guards.digest(self.tuig)},
            "compatibility": {"blocked_closure_count": 35},
            "restart_bundle_manifest_sha256": guards.digest(self.manifest),
        }
        self.target = self.root / "fresh"

    def run_guard(self):
        self.bindings.write_text(json.dumps(self.b))
        guards.preflight(
            list(
                map(
                    str,
                    (
                        self.bindings,
                        self.wheels,
                        self.bundle,
                        self.tuig,
                        self.manifest,
                        self.target,
                    ),
                )
            )
        )

    def test_valid(self):
        self.run_guard()
        self.assertTrue((self.target / "requirements.in").is_file())

    def test_null_commit(self):
        self.b["arbora"]["candidate_commit"] = None
        with self.assertRaises(SystemExit):
            self.run_guard()

    def test_extra_wheel(self):
        (self.wheels / "extra-1.whl").write_bytes(b"bad")
        with self.assertRaises(SystemExit):
            self.run_guard()

    def test_extra_bundle_file(self):
        (self.bundle / "conftest.py").write_text("")
        with self.assertRaises(SystemExit):
            self.run_guard()

    def test_modified_manifest(self):
        self.manifest.write_text("{}")
        with self.assertRaises(SystemExit):
            self.run_guard()

    def test_existing_root(self):
        self.target.mkdir()
        with self.assertRaises(SystemExit):
            self.run_guard()

    def test_relative_root(self):
        self.target = Path("relative")
        with self.assertRaises(SystemExit):
            self.run_guard()

    def test_bundle_symlink(self):
        (self.bundle / "alias").symlink_to(self.tuig)
        with self.assertRaises(SystemExit):
            self.run_guard()

    def test_changed_wheel(self):
        next(self.wheels.glob("medulla-*.whl")).write_bytes(b"bad")
        with self.assertRaises(SystemExit):
            self.run_guard()

    def test_junit(self):
        p = self.root / "junit.xml"
        cases = "".join(
            f'<testcase classname="tests.qualification.test_native_history_restart" name="test_completed_turn_survives_both_process_restarts[{kind}]" />'
            for kind in ("overstory-wire", "tuig")
        )
        p.write_text("<testsuites><testsuite>" + cases + "</testsuite></testsuites>")
        guards.junit(str(p))
        p.write_text(
            "<testsuites><testsuite>"
            + cases.replace(" />", "><skipped /></testcase>", 1)
            + "</testsuite></testsuites>"
        )
        with self.assertRaises(SystemExit):
            guards.junit(str(p))

    def test_junit_wrong_ids(self):
        p = self.root / "junit.xml"
        p.write_text(
            '<testsuites><testsuite><testcase name="x"/><testcase name="y"/></testsuite></testsuites>'
        )
        with self.assertRaises(SystemExit):
            guards.junit(str(p))


if __name__ == "__main__":
    unittest.main()
