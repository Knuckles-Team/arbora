"""Grouped MCP tool registration modules for the graph-os server.

Each exposes register_<group>(mcp), called from runtime._build_server —
the strangler split of the former ~5k-line monolithic builder.
"""

from graph_os.mcp_server.backing.tools.agent_execution_tools import (
    register_agent_execution_tools,
)
from graph_os.mcp_server.backing.tools.analysis_tools import register_analysis_tools
from graph_os.mcp_server.backing.tools.analyze_suite import register_analyze_suite_tools
from graph_os.mcp_server.backing.tools.argument_tools import register_argument_tools
from graph_os.mcp_server.backing.tools.audit_tools import register_audit_tools
from graph_os.mcp_server.backing.tools.bus_tools import register_bus_tools
from graph_os.mcp_server.backing.tools.candidate_claim_tools import (
    register_candidate_claim_tools,
)
from graph_os.mcp_server.backing.tools.claim_tools import register_claim_tools
from graph_os.mcp_server.backing.tools.compliance_tools import register_compliance_tools
from graph_os.mcp_server.backing.tools.config_tools import register_config_tools
from graph_os.mcp_server.backing.tools.data_prep_tools import register_data_prep_tools
from graph_os.mcp_server.backing.tools.domain_ops_tools import register_domain_ops_tools
from graph_os.mcp_server.backing.tools.durable_tools import register_durable_tools
from graph_os.mcp_server.backing.tools.engine_surface_tools import (
    register_engine_surface_tools,
)
from graph_os.mcp_server.backing.tools.engine_tools import register_engine_tools
from graph_os.mcp_server.backing.tools.epistemic_tools import register_epistemic_tools
from graph_os.mcp_server.backing.tools.evolution_tools import register_evolution_tools
from graph_os.mcp_server.backing.tools.governance_tools import register_governance_tools
from graph_os.mcp_server.backing.tools.graph_engineering_tools import (
    register_graph_engineering_tools,
)
from graph_os.mcp_server.backing.tools.incident_tools import register_incident_tools
from graph_os.mcp_server.backing.tools.job_tools import register_job_tools
from graph_os.mcp_server.backing.tools.mcp_apps import register_mcp_apps_tools
from graph_os.mcp_server.backing.tools.media_sidecar_tools import (
    register_media_sidecar_tools,
)
from graph_os.mcp_server.backing.tools.ontology_tools import register_ontology_tools
from graph_os.mcp_server.backing.tools.query_tools import register_query_tools
from graph_os.mcp_server.backing.tools.reach_tools import register_reach_tools
from graph_os.mcp_server.backing.tools.secret_tools import register_secret_tools
from graph_os.mcp_server.backing.tools.state_tools import register_state_tools
from graph_os.mcp_server.backing.tools.workflow_tools import register_workflow_tools
from graph_os.mcp_server.backing.tools.write_ingest_tools import (
    register_write_ingest_tools,
)

__all__ = [
    "register_query_tools",
    "register_write_ingest_tools",
    "register_analysis_tools",
    "register_agent_execution_tools",
    "register_analyze_suite_tools",
    "register_state_tools",
    "register_ontology_tools",
    "register_reach_tools",
    "register_bus_tools",
    "register_candidate_claim_tools",
    "register_claim_tools",
    "register_secret_tools",
    "register_engine_tools",
    "register_domain_ops_tools",
    "register_engine_surface_tools",
    "register_evolution_tools",
    "register_governance_tools",
    "register_audit_tools",
    "register_epistemic_tools",
    "register_incident_tools",
    "register_job_tools",
    "register_mcp_apps_tools",
    "register_media_sidecar_tools",
    "register_compliance_tools",
    "register_config_tools",
    "register_data_prep_tools",
    "register_workflow_tools",
    "register_argument_tools",
    "register_graph_engineering_tools",
    "register_durable_tools",
]
