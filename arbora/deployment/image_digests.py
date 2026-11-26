"""Check explicitly supplied deployment YAML for digest-pinned images.

Run ``python -m arbora.deployment.image_digests rendered.yaml [--set NAME=value]``
(the ``arbora-image-digests`` console script is the same entry). Exit 0 means
every checked image passed; exit 1 means an input or policy error. At least one
container or service across the input files must be checked. Non-workload
resources are ignored; this is not a general Kubernetes schema validator.

A document with a top-level ``services`` mapping and no ``kind`` is a Compose
or Swarm file (ARBORA-DEPLOY-R017.9). Each service ``image`` is a literal pin,
or in template mode exactly ``${NAME:?message}`` (pin required at deploy time)
or ``${NAME:-default}`` whose default is itself a pin. A bare ``${NAME}`` is
refused. ``--set NAME=value`` substitutes ``value`` for ``NAME`` before
validation (resolve mode); a name no file uses is refused ``UNUSED_SET_NAME``.
Refusal codes close each message: ``IMAGE_NOT_DIGEST_PINNED``,
``SERVICE_IMAGE_MISSING``, ``IMAGE_VARIABLE_UNPINNED``, ``UNSUPPORTED_DOCUMENT``.

Only Pod, Deployment, StatefulSet, DaemonSet, ReplicaSet, Job, CronJob and
recursive List resources are traversed, checking containers and initContainers.
Custom resources, typed lists and ephemeralContainers are outside this scope.
Images must use a lowercase repository (optionally a DNS registry and numeric
port) followed by @sha256:<64 lowercase hex>. Tags, including tag@digest, IPv6
registries, other digest algorithms and unrendered templates are unsupported.
YAML duplicate/non-string mapping keys and merge keys are rejected; ordinary
non-cyclic aliases are allowed. No images are resolved or pulled.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_REGISTRY = rf"{_HOST_LABEL}(?:\.{_HOST_LABEL})*(?::[0-9]+)?"
_IMAGE = re.compile(
    rf"(?P<repository>(?:{_REGISTRY}/)?{_COMPONENT}(?:/{_COMPONENT})*)"
    r"@sha256:[0-9a-f]{64}"
)
_VARIABLE = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<form>:\?|:-)?(?P<arg>[^}]*)\}"
)
_POD_PATHS = {
    "Pod": ("spec",),
    "Deployment": ("spec", "template", "spec"),
    "StatefulSet": ("spec", "template", "spec"),
    "DaemonSet": ("spec", "template", "spec"),
    "ReplicaSet": ("spec", "template", "spec"),
    "Job": ("spec", "template", "spec"),
    "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
}


class StrictLoader(yaml.SafeLoader):
    """Preserve mapping ambiguity as an error instead of discarding evidence."""

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict:
        keys: set[str] = set()
        for key, _ in node.value:
            if key.tag != "tag:yaml.org,2002:str":
                raise ConstructorError(
                    "while reading a mapping",
                    node.start_mark,
                    "only string keys are supported (YAML merge keys are unsupported)",
                    key.start_mark,
                )
            if key.value in keys:
                raise ConstructorError(
                    "while reading a mapping",
                    node.start_mark,
                    f"duplicate key {key.value!r}",
                    key.start_mark,
                )
            keys.add(key.value)
        return super().construct_mapping(node, deep=deep)


def load_strict(text: str) -> Any:
    """Parse one YAML document with :class:`StrictLoader` (a safe loader)."""
    return yaml.load(text, Loader=StrictLoader)  # nosec B506 - SafeLoader subclass


def _mapping(value: object, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{location}: expected a mapping")
    return value


def _valid_image(image: object) -> bool:
    match = _IMAGE.fullmatch(image) if isinstance(image, str) else None
    if match is None or len(match["repository"]) > 255:
        return False
    first, separator, _ = match["repository"].partition("/")
    if separator and ("." in first or ":" in first or first == "localhost"):
        return re.fullmatch(_REGISTRY, first) is not None
    return True


def is_digest_pinned(image: object) -> bool:
    """True when ``image`` is ``repository@sha256:<64 lowercase hex>`` with no tag."""
    return _valid_image(image)


def _check_containers(pod: dict[str, Any], location: str) -> int:
    count = 0
    for field in ("containers", "initContainers"):
        entries = pod.get(field, [] if field == "initContainers" else None)
        field_location = f"{location}.{field}"
        if not isinstance(entries, list) or (field == "containers" and not entries):
            raise ValueError(
                f"{field_location}: expected a {'nonempty ' if field == 'containers' else ''}list"
            )
        for index, entry in enumerate(entries):
            entry_location = f"{field_location}[{index}]"
            container = _mapping(entry, entry_location)
            if not _valid_image(container.get("image")):
                raise ValueError(
                    f"{entry_location}.image: expected repository@sha256:"
                    "<64 lowercase hex> with no tag (repository max 255 characters)"
                    " (IMAGE_NOT_DIGEST_PINNED)"
                )
            count += 1
    return count


def _substitute(image: str, values: Mapping[str, str], used: set[str]) -> str:
    """Replace each variable named in ``values``; record which names were used."""

    def replace(match: re.Match[str]) -> str:
        name = match["name"]
        if name not in values:
            return match.group(0)
        used.add(name)
        return values[name]

    return _VARIABLE.sub(replace, image)


def _service_image(image: str, location: str) -> None:
    variable = _VARIABLE.fullmatch(image)
    if variable is None:
        if not _valid_image(image):
            raise ValueError(
                f"{location}: expected repository@sha256:<64 lowercase hex> "
                "with no tag (IMAGE_NOT_DIGEST_PINNED)"
            )
        return
    if variable["form"] == ":?":
        return
    if variable["form"] == ":-":
        if not _valid_image(variable["arg"]):
            raise ValueError(
                f"{location}: the default of ${{{variable['name']}:-...}} must be "
                "a digest reference (IMAGE_NOT_DIGEST_PINNED)"
            )
        return
    raise ValueError(
        f"{location}: ${{{variable['name']}}} carries no pin; use "
        f"${{{variable['name']}:?message}} (IMAGE_VARIABLE_UNPINNED)"
    )


def _check_services(
    resource: dict[str, Any],
    location: str,
    values: Mapping[str, str],
    used: set[str],
) -> int:
    services = _mapping(resource.get("services"), f"{location}.services")
    count = 0
    for name, entry in services.items():
        service_location = f"{location}.services.{name}"
        service = _mapping(entry, service_location)
        image = service.get("image")
        if image is None:
            raise ValueError(
                f"{service_location}.image: missing (SERVICE_IMAGE_MISSING)"
            )
        if not isinstance(image, str):
            raise ValueError(
                f"{service_location}.image: expected repository@sha256:"
                "<64 lowercase hex> (IMAGE_NOT_DIGEST_PINNED)"
            )
        _service_image(_substitute(image, values, used), f"{service_location}.image")
        count += 1
    return count


def _check_resource(
    value: object,
    location: str,
    ancestors: frozenset[int] = frozenset(),
    values: Mapping[str, str] | None = None,
    used: set[str] | None = None,
) -> int:
    resource = _mapping(value, location)
    if "kind" not in resource and isinstance(resource.get("services"), dict):
        return _check_services(
            resource, location, values or {}, used if used is not None else set()
        )
    if "kind" not in resource and "services" not in resource:
        raise ValueError(
            f"{location}: expected a Kubernetes or Compose document (UNSUPPORTED_DOCUMENT)"
        )
    if id(resource) in ancestors:
        raise ValueError(f"{location}: cyclic List aliases are unsupported")
    kind = resource.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError(f"{location}.kind: expected a nonempty string")
    metadata = resource.get("metadata")
    name = (
        metadata.get("name", "<unnamed>") if isinstance(metadata, dict) else "<unnamed>"
    )
    location = f"{location} ({kind}/{name})"
    if kind == "List":
        items = resource.get("items")
        if not isinstance(items, list):
            raise ValueError(f"{location}.items: expected a list")
        lineage = ancestors | {id(resource)}
        return sum(
            _check_resource(item, f"{location}.items[{index}]", lineage, values, used)
            for index, item in enumerate(items)
        )
    if kind not in _POD_PATHS:
        return 0
    pod = resource
    for key in _POD_PATHS[kind]:
        location = f"{location}.{key}"
        pod = _mapping(pod.get(key), location)
    return _check_containers(pod, location)


def check_file(
    path: Path,
    values: Mapping[str, str] | None = None,
    used: set[str] | None = None,
) -> tuple[int, list[str]]:
    """Return the checked count and diagnostics, including file/document context.

    ``values`` is the resolve-mode substitution; ``used`` collects the names
    the file referenced so the caller can refuse an unused one.
    """
    count = 0
    errors: list[str] = []
    values = values or {}
    used = used if used is not None else set()
    try:
        with path.open(encoding="utf-8") as stream:
            for index, resource in enumerate(
                yaml.load_all(stream, Loader=StrictLoader), 1
            ):
                location = f"{path}: document {index}"
                # Empty documents from rendered YAML separators carry no resources.
                if resource is None:
                    continue
                try:
                    count += _check_resource(
                        resource, location, values=values, used=used
                    )
                except ValueError as exc:
                    errors.append(str(exc))
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError) as exc:
        errors.append(f"{path}: {exc}")
    return count, errors


def check_paths(
    paths: Sequence[Path], values: Mapping[str, str] | None = None
) -> tuple[int, list[str]]:
    """Check every file; refuse zero checked images and any unused ``values`` name."""
    total = 0
    errors: list[str] = []
    used: set[str] = set()
    for path in paths:
        count, file_errors = check_file(path, values, used)
        total += count
        errors.extend(file_errors)
    errors.extend(
        f"{name}: not used by any supplied file (UNUSED_SET_NAME)"
        for name in sorted(set(values or {}) - used)
    )
    if not total and not errors:
        errors.append("no containers checked across the supplied rendered YAML files")
    return total, errors


def _assignment(text: str) -> tuple[str, str]:
    name, separator, value = text.partition("=")
    if not separator or not _VARIABLE.fullmatch("${" + name + "}"):
        raise argparse.ArgumentTypeError(f"expected NAME=value, got {text!r}")
    return name, value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="rendered YAML files")
    parser.add_argument(
        "--set",
        dest="values",
        action="append",
        default=[],
        type=_assignment,
        metavar="NAME=value",
        help="substitute value for ${NAME} before validation (repeatable)",
    )
    args = parser.parse_args(argv)
    total, errors = check_paths(args.paths, dict(args.values))
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"Checked {total} container images: all digest-pinned.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
