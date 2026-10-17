"""The source catalogue is static and bounded without a registry."""

from __future__ import annotations

import pytest

from arbora.ingest import source_catalog as sc


def test_catalogue_without_registry_lists_providers_as_unavailable():
    catalog = sc.build_source_catalog()
    assert catalog["schema_version"] == "atlas-source-catalog.v1"
    assert catalog["sources"]
    assert catalog["querySurfaces"]
    assert not any(source["available"] for source in catalog["sources"])


def test_sync_preview_refuses_malformed_ids():
    with pytest.raises(ValueError, match="JSON list"):
        sc.normalize_source_sync_preview(
            source="gitlab", mode="delta", ids_json="[", connection="", graph=""
        )
