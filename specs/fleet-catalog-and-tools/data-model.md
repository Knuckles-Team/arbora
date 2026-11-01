# Data model — ARBORA-FLEET

Arbora holds typed in-process models for the catalog and the fleet operations; the engine stores the durable records.

- `OpSpec` and its enums describe one registry operation; `Registry` carries the canonical serialization and `registry_digest`.
- `CatalogItem` (with `schema_fingerprint`), `FleetCatalog` and `CatalogGeneration` describe the filtered catalog; `ReadContext`, `ReadReceipt`, `ServerPage` and `ComponentPin` describe a catalog read.
- `OnboardingTrigger`, `PackSurfaceCounts` and `SourceApprovalDecision` describe an onboarding pass and its contract decisions; `ConnectContract` (planned) records resource digests.
- `WriteBackPreviewRequest`, `WriteBackPreview` and `WriteBackReceipt` describe write-back; `FeedEvent` describes a sanitized feed event; `WorkItemView` projects a work item.
- Planned models: `VersionedDefinition` and `DefinitionAdmissionReceipt`, `SkillAuditReport`, `WorkflowAdmissionRecord`, `CertificationBinding`, `WorkflowBinding`, `ProvisionRequest`, `ProvisionResult`, `FleetCapacityProof`, `IntegratedProvisionReceipt`, `JobLoadRequest`, `JobLoadResult`, `DomainPackRequest`, `DomainPackResult`, `CatalogDeltaReceipt`.
