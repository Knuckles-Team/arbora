# ARBORA-FLEET — Data model

## PinRecord

Defined in `arbora/fleet/pin_store.py`. Fields: `tenant`, `principal`, `item_id`, `binding`, `pinned_at_ms`. The key is `tenant`, `principal` and `item_id`. The medulla store holds the durable copy.

## CoverageReport

Defined in `arbora/fleet/coverage.py`. Fields: `registered`, `covered`, `missing`, `zero_tools`, `ok`. Each `missing` entry holds a `server` and a `reason`.
