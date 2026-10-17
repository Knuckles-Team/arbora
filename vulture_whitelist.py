"""Names that are intentionally load-bearing public Protocol parameters."""

# These names are keyword-callable parts of the repository contracts.  Their
# implementations land in adapters, so static analysis cannot yet see their
# use.  Renaming them with a leading underscore would silently change the API.
after_agent_id: object = None
after_version_id: object = None
after_server_id: object = None
after_sequence: object = None
target_url: object = None
_ = (after_agent_id, after_version_id, after_server_id, after_sequence, target_url)

# Parameters of operations that refuse until their owner serves them
# (graph_os.api.errors.not_served). Remove each name when its operation binds.
answer_id: object = None
argument_map: object = None
argument_map_json: object = None
chunk_size: object = None
concept_ids: object = None
corrected_value: object = None
correction_type: object = None
dedup: object = None
defer_background_start: object = None
deviations: object = None
draft: object = None
drain_id: object = None
envelope_json: object = None
etype: object = None
incremental: object = None
link_name: object = None
loop_budget: object = None
map_id: object = None
mask: object = None
max_communities: object = None
max_per_source: object = None
min_size: object = None
min_trust: object = None
objects_json: object = None
policy_tags: object = None
required_capability_type: object = None
root_popularity: object = None
rounds: object = None
rule_kind: object = None
rule_scope: object = None
scheme_id: object = None
scheme_kind: object = None
scheme_name: object = None
skill_id: object = None
skill_type: object = None
spec_id: object = None
synthesize: object = None
synthesize_answer: object = None
trajectories: object = None
_ = (
    answer_id,
    argument_map,
    argument_map_json,
    chunk_size,
    concept_ids,
    corrected_value,
    correction_type,
    dedup,
    defer_background_start,
    deviations,
    draft,
    drain_id,
    envelope_json,
    etype,
    incremental,
    link_name,
    loop_budget,
    map_id,
    mask,
    max_communities,
    max_per_source,
    min_size,
    min_trust,
    objects_json,
    policy_tags,
    required_capability_type,
    root_popularity,
    rounds,
    rule_kind,
    rule_scope,
    scheme_id,
    scheme_kind,
    scheme_name,
    skill_id,
    skill_type,
    spec_id,
    synthesize,
    synthesize_answer,
    trajectories,
)
