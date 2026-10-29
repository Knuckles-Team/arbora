# Exact provider-document review evidence

Reviewed baseline: `09efd52d96b9f25a90dbfb95a88abf758ade404c`.

Installed Medulla candidate wheel: `medulla-2.27.10-cp39-abi3-manylinux_2_34_x86_64.whl`.
SHA-256: `7c0acac9f4838851f67d23ea98585db45679bd4d2377456ec081404053eb24f0`.
Hosted run `40736412125`, artifact `12839505046`.
Tested revision `1dc5b2f9ac284c17fe9fd66b973b4387e53aa0be`,
tree `96a22308cb6708e8b1739ea4395bc247f44518bc`.

Independent review re-expanded all selected schemas and verified all 74 canonical
output hashes and all 51 packaged canonical files. Of 589 changed document pairs,
554 retain exactly identical expanded selected schemas. Their reviewed candidate
manifest SHA-256 is `3e0d93b54f35f186713b8c3947a5acbb8beaf7971c71f710c6e1921ee71981d9`.
Only those exact operation/field/document/closure identities are recorded here.
The composed consumer registry is byte-identical to the independently reviewed registry.

All 35 changed closures remain excluded. One removes only the IssueControlLease
request description. Its closure hashes still differ; this exact-identity ledger
does not admit it. The other 34 retain separate structural obligations.
No native response, runtime continuation, installed dependency closure or release
acceptance is inferred. Candidate artifact installation is not a PyPI release.
