"""The host composition of the operation ports (ARBORA-HOST-R038.1).

Each port is a protocol declared by the operation layer; the subsystem that owns the work
implements it, and the host composes the one record that every handler reads from
``context.services``. A handler never imports the subsystem.
"""

from __future__ import annotations

from arbora.a2a.run_identity import A2ARunIdentity
from arbora.api.ops.ports import OperationPorts
from arbora.control_plane.admission.port import ControlPlaneAppAdmission
from arbora.fleet.acceptance_source import OnboardingAcceptance

__all__ = ["build_operation_ports"]


def build_operation_ports() -> OperationPorts:
    """The record of every port this host serves."""
    return OperationPorts(
        apps=ControlPlaneAppAdmission(),
        fleet_acceptance=OnboardingAcceptance(),
        run_identity=A2ARunIdentity(),
    )
