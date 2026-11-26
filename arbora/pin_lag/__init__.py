"""Capability guards for upstream shapes that are on ``main`` but not yet released.

One module per upstream. Each guard names what the pinned release lacks and refuses
``UNAVAILABLE``; the owner deletes the package in one step after re-pinning.
"""
