"""The sandbox boundary is declared from kernel facts only (ARBORA-OPS-R894)."""

from __future__ import annotations

import pytest

from arbora.agent_host.sandbox_boundary import verify_boundary

STATUS = "Name:\tx\nCapEff:\t0000000000000000\nNoNewPrivs:\t1\nSeccomp:\t2\n"
UID_MAP = "         0       1000          1\n"


def _reader(**files: str):
    def read(name: str) -> str:
        if name not in files:
            raise FileNotFoundError(name)
        return files[name]

    return read


@pytest.mark.spec("ARBORA-OPS-R894")
def test_boundary_is_verified_only_when_every_kernel_fact_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = verify_boundary(_reader(status=STATUS, uid_map=UID_MAP))
    assert held.verified and held.missing == ()
    cases = {
        "no_effective_capabilities": STATUS.replace(
            "0000000000000000", "00000000a80425fb"
        ),
        "no_new_privs": STATUS.replace("NoNewPrivs:\t1", "NoNewPrivs:\t0"),
        "seccomp_filter": STATUS.replace("Seccomp:\t2", "Seccomp:\t0"),
    }
    for name, status in cases.items():
        evidence = verify_boundary(_reader(status=status, uid_map=UID_MAP))
        assert not evidence.verified and evidence.missing == (name,)
    host_root = verify_boundary(
        _reader(status=STATUS, uid_map="         0          0 4294967295\n")
    )
    assert host_root.missing == ("user_namespace",)
    # An unreadable fact is an absent fact, and no environment flag can supply one.
    monkeypatch.setenv("ARBORA_SANDBOX_VERIFIED", "1")
    assert verify_boundary(_reader()).missing == (
        "no_effective_capabilities",
        "no_new_privs",
        "seccomp_filter",
        "user_namespace",
    )


def _proc_fixture(root, *, status: str, uid_map: str):
    """A directory of kernel fact files and the reader that serves them like /proc/self."""
    (root / "status").write_text(status, encoding="utf-8")
    (root / "uid_map").write_text(uid_map, encoding="utf-8")
    return lambda name: (root / name).read_text(encoding="utf-8")


@pytest.mark.spec("ARBORA-OPS-R894")
@pytest.mark.parametrize(
    ("status", "uid_map", "verified", "missing"),
    [
        (STATUS, UID_MAP, True, ()),
        (
            STATUS.replace("Seccomp:\t2", "Seccomp:\t0"),
            UID_MAP,
            False,
            ("seccomp_filter",),
        ),
        (STATUS, "", False, ("user_namespace",)),
        (STATUS, "0 0 4294967295\n", False, ("user_namespace",)),
    ],
    ids=["verified", "missing-seccomp", "no-user-namespace-map", "host-root-map"],
)
def test_fixture_kernel_facts_prove_each_boundary_case_without_a_pod(
    tmp_path, status: str, uid_map: str, verified: bool, missing: tuple[str, ...]
) -> None:
    evidence = verify_boundary(_proc_fixture(tmp_path, status=status, uid_map=uid_map))
    assert evidence.verified is verified
    assert evidence.missing == missing
