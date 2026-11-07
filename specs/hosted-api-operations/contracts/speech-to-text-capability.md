# Seam: Speech-to-text capability for audio prompts

Provider: audio-transcriber `TRANSCRIBE-R002` (tool `transcribe_speech`), or any connector serving the same contract.
Consumer: arbora `ARBORA-OPS-R899`.

## Wire shape

Arbora finds the tool with `fleet.catalog.search` for `speech-to-text`. A tool qualifies when its input schema takes `artifact_b64`, `digest` and `media_type`, takes no `action` argument, and its name or description names the `speech-to-text` capability. Arbora calls it through `fleet.call` with `{artifact_b64, digest: "sha256:<hex>", media_type}`. The tool returns `speech-transcript/1`: `{schema_version, status: ok|refused, refusal_code, source_digest, media_type, text, language, language_probability, duration_s, segments[{start_ms, end_ms, text, speaker, words[]}], diarization{requested, supported, reason}, converted, model, backend}`.

## Refusal codes

Provider: `INVALID_ARGUMENT`, `DIGEST_MISMATCH`, `MEDIA_TOO_LARGE`, `MEDIA_TOO_LONG`, `UNSUPPORTED_MEDIA`, `TRANSCRIPTION_FAILED`. Arbora: `TRANSCRIPTION_UNAVAILABLE`, `TRANSCRIPTION_REFUSED`, `MEDIA_TOO_LARGE`, `MEDIA_TOO_LONG`, `MEDIA_UNREADABLE`, `MODALITY_UNSUPPORTED`. Arbora maps provider `UNSUPPORTED_MEDIA` to `MEDIA_UNREADABLE`, the two limit codes to themselves, and every other provider refusal to `TRANSCRIPTION_REFUSED`.

## Authoritative types

Provider `SpeechTranscript` in `audio_transcriber/speech_contract.py`; arbora `MediaPart`, `Derivation` and `ModalityRefused` in `arbora/modality/models.py`.

## Test vectors

`tests/modality/_fakes.py` holds the arbora-side payload; the provider's `tests/test_speech_contract.py` holds its side.
