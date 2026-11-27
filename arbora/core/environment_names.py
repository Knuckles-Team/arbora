"""The declared environment names of Arbora outside ``ArboraConfig`` (ARBORA-HOST-R010.2).

One row per name: ``(name, type, default text, owner row, call sites, secret flag)``. The
types are the manifest vocabulary (string, int, float, bool, secret_reference); a default
is text and the reader types it. A name with a ``{PLATFORM}`` slot declares the family
for every messaging platform. ``arbora.core.environment_manifest`` builds the
declarations from this table, and its census keeps the table and the source in step.
"""

from __future__ import annotations

from arbora.core.environment_names_a_to_e import ROWS as _A_TO_E
from arbora.core.environment_names_f_to_l import ROWS as _F_TO_L
from arbora.core.environment_names_m_to_z import ROWS as _M_TO_Z

NAMES: tuple[tuple[str, str, str | None, str, tuple[str, ...], bool], ...] = (
    *_A_TO_E,
    *_F_TO_L,
    *_M_TO_Z,
)
