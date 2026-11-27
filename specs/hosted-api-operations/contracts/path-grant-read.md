# Path grant read

Seam between `ARBORA-OPS` and Medulla. Owner rows: `MEDULLA-SCM-R007.1` in the medulla repository and `ARBORA-OPS-R894` in this repository.

Provider: medulla MEDULLA-SCM-R007.1
Consumer: arbora ARBORA-OPS-R894

## Wire shape

The engine method `ListPathGrants` takes `{principal, repo?}` and answers `PathGrantList{grants}`, each grant a repository, a path prefix and a read and a write bit. The tenant is the verified tenant and is never a request field. Arbora asks for the verified caller's own grants, keeps the write prefixes of the one repository the caller may write, and binds them as the run's source grant for the changeset submitter. A principal with no grants answers an empty list and gets no source grant.

## Refusal codes

- `ACCESS_DENIED`: the caller asked for another principal's grants without `graph:admin`. The answer is the same whether that principal exists or not.
- `PATH_GRANT_INVALID`: a field is out of bounds. Arbora forwards the code unchanged.

## Authoritative types

- Medulla `ListPathGrantsRequest` and `PathGrantList`.
- `arbora/a2a/source_grant.py`: `read_engine_grant` and `grant_from_path_grants`.

## Test vectors

- `tests/a2a/test_source_grant.py::test_resolution_lists_the_path_grants_of_the_verified_principal`.
- `tests/a2a/test_source_grant.py::test_the_pinned_release_grants_nothing_and_names_the_missing_method`.
