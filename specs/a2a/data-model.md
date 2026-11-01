# ARBORA-A2A data model

## ToolTaskRequest

Frozen, closed pydantic model in `arbora/a2a/tool_task.py`. It is the immutable admitted request of one single-tool task. Its content-addressed `request_ref` is the carrier reference.

| Field | Type | Meaning |
|---|---|---|
| `version` | `Literal[1]` | Wire version. |
| `operation_id` | `str` | The registry operation the task invokes. |
| `parameters_json` | `str` | Canonical JSON object text of the operation parameters, bounded in bytes. |
| `principal` | `str` | The original caller principal. |
| `tenant` | `str` | The tenant of the original caller. |
| `session_id` | `str` | The session reference bound at admission. |
| `authority_ref` | `str` | Reference that resolves the original caller when the call runs. |
| `policy_revision` | `str` | Policy revision seen at admission. |
| `registry_digest` | `str` | 64 lowercase hex digest of the operation registry. |

## ToolTaskCarrier

Frozen, closed model in `arbora/a2a/tool_task.py`. The verified queue carrier. It holds a reference and never the inputs: `job_id`, `payload_ref`, `tenant`, `session_id`, `allowed_tool_subset` (exactly one name) and an optional `deadline_unix`.

## ToolTaskResult

Frozen, closed model in `arbora/a2a/tool_task.py`. The owner's bound outcome of one execution: `job_id`, `request_ref`, `principal`, `outcome` (`completed`, `failed`, `refused` or `indeterminate`) and `receipt_ref`.

## ToolSubsetAdmissionRequest and ToolSubsetAdmissionDecision

Wire models in `arbora/a2a/admission.py`. The request carries the context budget and the routed capability; the decision carries `admitted`, the admitted tool names, a typed refusal reason and a provenance reference. Rows ARBORA-A2A-R016.2 to R016.4 extend them with the model profile, task class and harness answer.

## Records owned by the engine

The WorkItem, its lease and the human grant are engine records. Arbora reads them through `WorkItemView` (`arbora/a2a/work_items.py`) and adds no second store.
