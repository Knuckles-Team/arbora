import pytest

from arbora.messaging.action_dispatch import (
    NotificationSpec,
    RecordingNotifier,
    WebhookSpec,
    send_notification,
    send_webhook,
)

CLOCK = lambda: 7.0  # noqa: E731


class Boom:
    def notify(self, spec, message):
        raise RuntimeError("sink down")


@pytest.mark.spec("GRAPHOS-MESSAGING-R700")
def test_recording_notifier_journals_without_delivering():
    sink = RecordingNotifier(clock=CLOCK)
    out = send_notification(
        NotificationSpec(channel="ops", recipient="a"), "hi", sink, CLOCK
    )
    assert sink.records == [out]
    assert (out["delivered"], out["transport"], out["timestamp"]) == (
        False,
        "recorded",
        7.0,
    )


@pytest.mark.spec("GRAPHOS-MESSAGING-R700")
def test_raising_sink_is_recorded_as_error():
    out = send_notification(NotificationSpec(), "m", Boom(), CLOCK)
    assert (out["transport"], out["delivered"], out["error"]) == (
        "error",
        False,
        "sink down",
    )


def _raise(*_):
    raise OSError("refused")


@pytest.mark.parametrize(
    ("post", "delivered", "transport", "extra"),
    [
        (None, False, "recorded", None),
        (lambda *_: 204, True, "http", ("status_code", 204)),
        (lambda *_: 500, False, "http", ("status_code", 500)),
        (_raise, False, "http", ("error", "refused")),
    ],
)
@pytest.mark.spec("GRAPHOS-MESSAGING-R700")
def test_webhook_outcomes(post, delivered, transport, extra):
    spec = WebhookSpec(url="http://x", payload={"a": 1})
    out = send_webhook(spec, {"b": 2}, post, CLOCK)
    assert out["payload"] == {"a": 1, "b": 2}
    assert (out["delivered"], out["transport"]) == (delivered, transport)
    if extra:
        assert out[extra[0]] == extra[1]
