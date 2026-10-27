"""Failure evidence must not change execution or expose fixture payloads."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from tests import _boot_diagnostics as diagnostics


def test_snapshot_is_bounded_and_excludes_process_payloads(monkeypatch, tmp_path):
    private = "private-token private-command private-path"
    child = SimpleNamespace(pid=123, poll=lambda: None, args=private)
    text = (
        f"Name:\t{private}\nState:\tS (sleeping)\nVmRSS:\t42 kB\n"
        "VmHWM:\t99 kB\nread_bytes: 17\nwrite_bytes: 25\n"
        "some avg10=1.25 avg60=2 avg300=3 total=40\n"
        "full avg10=nan avg60=inf avg300=-1 total=bad\n"
        f"private: {private}\n"
    )
    monkeypatch.setattr(diagnostics, "_read", lambda path: text)
    recorder = diagnostics.BootDiagnostics(tmp_path, [child] * 100)
    result = recorder.snapshot()
    encoded = json.dumps(result)
    assert private not in encoded and "123" not in encoded
    assert len(result["children"]) == 5
    assert result["children"][0]["memory_kib"] == {"VmRSS": 42, "VmHWM": 99}
    assert result["children"][0]["state"] == "S"
    assert result["pressure"]["cpu"] == {
        "some_avg10": 1.25,
        "some_avg60": 2,
        "some_avg300": 3,
        "some_total": 40,
    }
    assert len(encoded) < 4096


@pytest.mark.parametrize("error", [TimeoutError("private"), asyncio.CancelledError()])
def test_phase_preserves_original_exception_and_records_before_teardown(
    monkeypatch, tmp_path, error
):
    recorder = diagnostics.BootDiagnostics(tmp_path, [])
    calls = []
    monkeypatch.setattr(recorder, "snapshot", lambda: calls.append("snapshot") or {})
    with pytest.raises(type(error)) as caught:
        try:
            with recorder.phase("engine_socket"):
                calls.append("execution")
                raise error
        finally:
            calls.append("teardown")
    assert caught.value is error
    assert calls == ["execution", "snapshot", "teardown"]
    note = error.__notes__[0]
    assert "private" not in note
    assert (
        json.loads(note.removeprefix("boot_diagnostics="))["phase"] == "engine_socket"
    )


def test_success_does_not_sample_or_change_result(monkeypatch, tmp_path):
    recorder = diagnostics.BootDiagnostics(tmp_path, [])
    monkeypatch.setattr(recorder, "snapshot", lambda: pytest.fail("not on success"))
    result = object()
    with recorder.phase("http_operation"):
        actual = result
    assert actual is result


def test_unavailable_sampler_cannot_mask_original_failure(monkeypatch, tmp_path):
    recorder = diagnostics.BootDiagnostics(tmp_path, [])

    def broken():
        raise OSError("private path")

    monkeypatch.setattr(recorder, "snapshot", broken)
    error = TimeoutError()
    with pytest.raises(TimeoutError) as caught:
        with recorder.phase("engine_restart_socket"):
            raise error
    assert caught.value is error
    assert error.__notes__ == ["boot_diagnostics=unavailable"]


def test_file_reads_are_bounded_and_missing_files_are_unavailable(tmp_path):
    path = tmp_path / "sample"
    path.write_text("x" * 10000)
    assert len(diagnostics._read(path)) == 8192
    assert diagnostics._read(tmp_path / "missing") == ""


def test_unknown_phase_refuses_before_execution(tmp_path):
    with pytest.raises(ValueError, match="Unsupported diagnostic phase"):
        with diagnostics.BootDiagnostics(tmp_path, []).phase("private arbitrary text"):
            pytest.fail("must refuse")


def test_http_wiring_keeps_timeout_and_original_exception(monkeypatch, tmp_path):
    import httpx

    from tests import test_real_boot as boot

    recorder = diagnostics.BootDiagnostics(tmp_path, [])
    monkeypatch.setattr(recorder, "snapshot", lambda: {})
    error = httpx.ReadTimeout("private response")
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs)
        raise error

    monkeypatch.setattr(httpx, "post", post)
    stack = {
        "diagnostics": recorder,
        "base_url": "http://fixture.invalid",
        "token": "private-token",
    }
    params = {"prompt": "private-prompt"}
    with pytest.raises(httpx.ReadTimeout) as caught:
        boot._post_op(stack, "runs.get", params)
    assert caught.value is error
    assert calls == [
        {
            "json": params,
            "headers": {"Authorization": "Bearer private-token"},
            "timeout": 30,
        }
    ]
    assert "private" not in error.__notes__[0]
    assert '"phase":"http_operation"' in error.__notes__[0]


def test_socket_wait_keeps_deadline_and_samples_before_unwind(monkeypatch, tmp_path):
    from tests import test_real_boot as boot

    recorder = diagnostics.BootDiagnostics(tmp_path, [])
    calls = []
    clock = iter((0, 0, 61, 61))
    monkeypatch.setattr(boot.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(boot.time, "sleep", lambda _: pytest.fail("deadline elapsed"))
    monkeypatch.setattr(recorder, "snapshot", lambda: calls.append("snapshot") or {})
    with pytest.raises(AssertionError, match="timed out waiting") as caught:
        with recorder.phase("engine_socket"):
            boot._wait(lambda: False, timeout=60, what="the epistemic-graph socket")
    assert calls == ["snapshot"]
    assert '"elapsed_ms":61000' in caught.value.__notes__[0]
