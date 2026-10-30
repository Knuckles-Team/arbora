"""Candidate preparation validates actual source and artifact identity boundaries."""

import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "prepare", Path(__file__).with_name("prepare_native_history_candidate.py")
)
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self.ref, self.tree = "a" * 40, "b" * 40

    def producer(self):
        producer, stem = self.root / "inputs/medulla", self.root / "inputs/stem"
        producer.mkdir(parents=True)
        stem.mkdir(parents=True)
        source = self.sources / "medulla"
        source.mkdir()
        recipe = source / "Cargo.toml"
        recipe.write_bytes(b"reviewed recipe")
        client, server = producer / "medulla-1.whl", stem / "medulla_stem-1.whl"
        client.write_bytes(b"client")
        server.write_bytes(b"server")
        (producer / "source.sha").write_text(self.ref)
        (producer / "source.tree").write_text(self.tree)
        (producer / "SHA256SUMS").write_text(f"{prepare.sha(client)}  {client.name}\n")
        (producer / "build-recipe.sha256").write_text(
            f"{prepare.sha(recipe)}  Cargo.toml\n"
        )
        p = {
            "source_revision": self.ref,
            "source_tree": self.tree,
            "source_dirty": False,
            "installed_proof": {"verified": True, "binary_sha256": "c" * 64},
            "recipe_sha256": {"Cargo.toml": prepare.sha(recipe)},
            "artifact_sha256": {server.name: prepare.sha(server)},
        }
        (stem / "provenance.json").write_text(json.dumps(p))
        return producer, stem, p

    def test_producer_valid(self):
        self.producer()
        client, server, p = prepare.verify_producer(
            self.root, self.sources, self.ref, self.tree
        )
        self.assertEqual(client.read_bytes(), b"client")
        self.assertEqual(server.read_bytes(), b"server")
        self.assertTrue(p["installed_proof"]["verified"])

    def test_producer_rejects_wrong_source(self):
        producer, _, _ = self.producer()
        (producer / "source.sha").write_text("wrong")
        with self.assertRaises(SystemExit):
            prepare.verify_producer(self.root, self.sources, self.ref, self.tree)

    def test_producer_rejects_changed_artifact(self):
        _, stem, _ = self.producer()
        (stem / "medulla_stem-1.whl").write_bytes(b"changed")
        with self.assertRaises(SystemExit):
            prepare.verify_producer(self.root, self.sources, self.ref, self.tree)

    def test_producer_rejects_unverified_smoke(self):
        _, stem, p = self.producer()
        p["installed_proof"]["verified"] = False
        (stem / "provenance.json").write_text(json.dumps(p))
        with self.assertRaises(SystemExit):
            prepare.verify_producer(self.root, self.sources, self.ref, self.tree)

    def test_producer_rejects_recipe_traversal(self):
        producer, _, _ = self.producer()
        (producer / "build-recipe.sha256").write_text("0" * 64 + " ../outside\n")
        with self.assertRaises(SystemExit):
            prepare.verify_producer(self.root, self.sources, self.ref, self.tree)

    def test_sources_valid_and_dirty_refused(self):
        with patch.object(prepare, "git", side_effect=[self.ref, "", self.tree]):
            receipt = prepare.verify_sources(self.sources, {"arbora": self.ref})
        self.assertEqual(receipt["arbora"]["tree"], self.tree)
        with patch.object(prepare, "git", side_effect=[self.ref, " M pyproject.toml"]):
            with self.assertRaises(SystemExit):
                prepare.verify_sources(self.sources, {"arbora": self.ref})

    def test_sources_wrong_ref_refused(self):
        with patch.object(prepare, "git", return_value="c" * 40):
            with self.assertRaises(SystemExit):
                prepare.verify_sources(self.sources, {"arbora": self.ref})
        with self.assertRaises(SystemExit):
            prepare.verify_sources(self.sources, {"arbora": "main"})

    def bundle(self, changed=False):
        here = self.sources / "arbora/scripts/qualification"
        here.mkdir(parents=True)
        source = self.sources / "arbora/tests/test_example.py"
        source.parent.mkdir()
        source.write_bytes(b"reviewed")
        manifest = {"tests/test_example.py": prepare.sha(source)}
        (here / "restart-bundle-manifest.json").write_text(json.dumps(manifest))
        if changed:
            source.write_bytes(b"changed")
        return here

    def test_bundle_hashes_and_files(self):
        here = self.bundle()
        prepare.prepare_bundle(self.root, self.sources, here)
        self.assertEqual(
            (self.root / "restart-bundle/tests/test_example.py").read_bytes(),
            b"reviewed",
        )

    def test_bundle_changed_source_refused(self):
        here = self.bundle(changed=True)
        with self.assertRaises(SystemExit):
            prepare.prepare_bundle(self.root, self.sources, here)

    def test_bundle_reused_target_refused(self):
        here = self.bundle()
        (self.root / "restart-bundle").mkdir()
        with self.assertRaises(FileExistsError):
            prepare.prepare_bundle(self.root, self.sources, here)

    def test_meristem_bad_origin_refused(self):
        payload = {
            "urls": [
                {
                    "filename": "meristem-4.whl",
                    "digests": {"sha256": "d" * 64},
                    "url": "https://bad.example/a.whl",
                }
            ]
        }
        with patch.object(
            prepare.urllib.request,
            "urlopen",
            return_value=io.BytesIO(json.dumps(payload).encode()),
        ):
            with self.assertRaises(SystemExit):
                prepare.fetch_meristem(self.root, "d" * 64, "4.0.1")


if __name__ == "__main__":
    unittest.main()
