"""Versioned private workflow roles with a contract-derived public projection.

The legacy recipient reference contains both public routing rules and private
people targets.  This module keeps those concerns separate:

* region and department membership always comes from the versioned
  ``legacy-workflows.json`` contract;
* private accounts live in ``local/workflow-roles.json`` and are never printed
  by the operator-facing helpers;
* importing the old reference is explicit and writes only the destination the
  caller supplied.

The module intentionally has no database, Hermes, model, scheduler, or
transport dependency.  ``load`` is the sole internal compatibility adapter for
the existing workflow callers; ``validate``, ``prepare``, ``import_legacy``
and ``diagnose`` return metadata only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "datasage-workflow-roles/v1"
VERSION = 1
ACTIVE_RELATIVE_PATH = Path("local") / "workflow-roles.json"
LEGACY_RELATIVE_PATH = (
    Path("plugins") / "datasage-query" / "contracts" / "legacy-workflows.json"
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_LABEL_RE = re.compile(r"^[^\x00-\x1f\x7f-\x9f]{1,256}$")
_TOP_KEYS = {
    "schema",
    "version",
    "source",
    "regions",
    "price_manager_fixed",
    "purchase_target",
    "approval",
}
_SOURCE_KEYS = {"kind", "content_sha256", "label", "legacy_reference"}
_REGION_KEYS = {"executors", "managers"}
_EXECUTOR_KEYS = {"account", "name"}
_SOURCE_KINDS = {"legacy_recipient_reference", "explicit_private_roles"}


class RoleConfigurationError(ValueError):
    """Stable, non-sensitive error taxonomy for the role configuration API."""

    def __init__(self, code: str, message: str = "role configuration is invalid"):
        super().__init__(message)
        self.code = code
        self.message = message


def active_path(profile: str | os.PathLike[str]) -> Path:
    """Return the only path used for the active private role configuration."""

    return Path(profile) / ACTIVE_RELATIVE_PATH


def _as_profile(profile: str | os.PathLike[str] | None) -> Path:
    if profile is None:
        raise RoleConfigurationError(
            "ROLE_PROFILE_REQUIRED", "role configuration requires a profile root"
        )
    value = Path(profile)
    if not str(value):
        raise RoleConfigurationError(
            "ROLE_PROFILE_REQUIRED", "role configuration requires a profile root"
        )
    try:
        return value.resolve()
    except OSError as exc:
        raise RoleConfigurationError(
            "ROLE_PROFILE_INVALID", "role configuration profile root is unavailable"
        ) from exc


def _as_path(value: str | os.PathLike[str] | None, *, code: str) -> Path:
    if value is None or not str(value).strip():
        raise RoleConfigurationError(code, "role configuration path is required")
    return Path(value)


def _safe_label(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoleConfigurationError("ROLE_CONFIGURATION_INVALID", f"{field} is invalid")
    value = value.strip()
    if _SAFE_LABEL_RE.fullmatch(value) is None or "http://" in value.lower() or "https://" in value.lower():
        raise RoleConfigurationError("ROLE_CONFIGURATION_INVALID", f"{field} is invalid")
    return value


def _account(value: Any, *, field: str) -> str:
    value = _safe_label(value, field=field)
    if any(character.isspace() for character in value):
        raise RoleConfigurationError("ROLE_CONFIGURATION_INVALID", f"{field} is invalid")
    return value


def _string_list(value: Any, *, field: str, account_values: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise RoleConfigurationError("ROLE_CONFIGURATION_INVALID", f"{field} is invalid")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        text = (
            _account(item, field=f"{field}[{index}]")
            if account_values
            else _safe_label(item, field=f"{field}[{index}]")
        )
        if text in seen:
            raise RoleConfigurationError(
                "ROLE_CONFIGURATION_DUPLICATE", f"{field} contains duplicate values"
            )
        seen.add(text)
        result.append(text)
    return result


def _read_json(path: Path, *, missing_code: str) -> tuple[dict[str, Any], bytes]:
    try:
        if path.is_symlink():
            raise RoleConfigurationError(
                "ROLE_CONFIGURATION_SYMLINK", "role configuration path cannot be a symlink"
            )
        if not path.is_file():
            raise RoleConfigurationError(missing_code, "role configuration file is required")
        raw = path.read_bytes()
    except RoleConfigurationError:
        raise
    except OSError as exc:
        raise RoleConfigurationError(missing_code, "role configuration file is unavailable") from exc
    if len(raw) > 256 * 1024:
        raise RoleConfigurationError("ROLE_CONFIGURATION_TOO_LARGE", "role configuration is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RoleConfigurationError("ROLE_CONFIGURATION_INVALID", "role configuration is not valid JSON") from exc
    if not isinstance(value, dict):
        raise RoleConfigurationError("ROLE_CONFIGURATION_INVALID", "role configuration must be an object")
    return value, raw


def _digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _digest_document(value: Mapping[str, Any]) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RoleConfigurationError("ROLE_CONFIGURATION_INVALID", "role configuration cannot be hashed") from exc
    return _digest_bytes(raw)


def _is_reparse_point(path: Path) -> bool:
    """Return whether an existing Windows path component is a reparse point."""

    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _public_contract(
    profile: str | os.PathLike[str], *, prefer_pinned: bool = False
) -> tuple[dict[str, Any], str]:
    root = _as_profile(profile)
    if prefer_pinned and __package__:
        # Runtime callers and legacy_workflow.policy() must consume the same
        # process-pinned contract.  CLI prepare/import calls intentionally use
        # the explicit recovery root below so they can validate an isolated
        # destination without bootstrapping the live profile.
        try:
            from . import contract_store

            pinned_root = contract_store.profile_root().resolve()
            if pinned_root == root:
                public = contract_store.read_yaml(LEGACY_RELATIVE_PATH.as_posix())
                _path, digest = contract_store.content_signature(
                    LEGACY_RELATIVE_PATH.as_posix()
                )
                if not isinstance(public, dict):
                    raise RoleConfigurationError(
                        "ROLE_PUBLIC_CONTRACT_INVALID", "legacy workflow public contract is invalid"
                    )
                return public, digest
            # An explicit recovery/test root is allowed to use its own public
            # contract.  Only the module's actual owning Profile uses the
            # process-pinned contract path above.
        except RoleConfigurationError:
            raise
        except Exception as exc:
            raise RoleConfigurationError(
                "ROLE_PUBLIC_CONTRACT_REQUIRED",
                "pinned legacy workflow public contract is unavailable",
            ) from exc
    path = root / LEGACY_RELATIVE_PATH
    public, raw = _read_json(path, missing_code="ROLE_PUBLIC_CONTRACT_REQUIRED")
    if (
        type(public.get("version")) is not int
        or public.get("version") != 1
        or not isinstance(public.get("regions"), Mapping)
    ):
        raise RoleConfigurationError(
            "ROLE_PUBLIC_CONTRACT_INVALID", "legacy workflow public contract is invalid"
        )
    regions = public["regions"]
    if not regions:
        raise RoleConfigurationError(
            "ROLE_PUBLIC_CONTRACT_INVALID", "legacy workflow public contract has no regions"
        )
    for region, definition in regions.items():
        _safe_label(region, field="public region")
        if not isinstance(definition, Mapping):
            raise RoleConfigurationError("ROLE_PUBLIC_CONTRACT_INVALID", "public region is invalid")
        if set(definition) - {"departments", "task_sales_departments"}:
            # The legacy contract may grow unrelated job metadata at the root,
            # but each public region must keep this small derivation surface.
            raise RoleConfigurationError("ROLE_PUBLIC_CONTRACT_INVALID", "public region contains unsupported fields")
        _string_list(definition.get("departments"), field="public departments")
        _string_list(definition.get("task_sales_departments"), field="public task departments")
    return public, _digest_bytes(raw)


def _validate_source(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - _SOURCE_KEYS:
        raise RoleConfigurationError("ROLE_CONFIGURATION_INVALID", "role configuration source is invalid")
    kind = value.get("kind")
    if kind not in _SOURCE_KINDS:
        raise RoleConfigurationError("ROLE_CONFIGURATION_INVALID", "role configuration source kind is invalid")
    content_sha256 = value.get("content_sha256")
    if not isinstance(content_sha256, str) or _HASH_RE.fullmatch(content_sha256) is None:
        raise RoleConfigurationError("ROLE_CONFIGURATION_INVALID", "role configuration source hash is invalid")
    label = _safe_label(value.get("label"), field="source label")
    result: dict[str, Any] = {
        "kind": kind,
        "content_sha256": content_sha256,
        "label": label,
    }
    if value.get("legacy_reference") is not None:
        result["legacy_reference"] = _safe_label(
            value.get("legacy_reference"), field="legacy reference"
        )
    return result


def _validate_private(
    document: Mapping[str, Any],
    public: Mapping[str, Any],
    *,
    raw: bytes | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    if set(document) - _TOP_KEYS or not _TOP_KEYS.intersection(document):
        raise RoleConfigurationError("ROLE_CONFIGURATION_INVALID", "role configuration fields are invalid")
    if (
        document.get("schema") != SCHEMA
        or type(document.get("version")) is not int
        or document.get("version") != VERSION
    ):
        raise RoleConfigurationError("ROLE_CONFIGURATION_INVALID", "role configuration schema is invalid")
    source = _validate_source(document.get("source"))
    regions = document.get("regions")
    public_regions = public.get("regions")
    if not isinstance(regions, Mapping) or not isinstance(public_regions, Mapping):
        raise RoleConfigurationError("ROLE_CONFIGURATION_INVALID", "role configuration regions are invalid")
    if set(regions) != set(public_regions):
        raise RoleConfigurationError(
            "ROLE_CONFIGURATION_REGIONS_MISMATCH",
            "private role regions do not match the public contract",
        )

    executor_count = 0
    manager_count = 0
    for region in sorted(public_regions, key=str):
        private_region = regions.get(region)
        if not isinstance(private_region, Mapping) or set(private_region) != _REGION_KEYS:
            raise RoleConfigurationError("ROLE_CONFIGURATION_INVALID", "private region fields are invalid")
        executors = private_region.get("executors")
        if not isinstance(executors, list):
            raise RoleConfigurationError("ROLE_CONFIGURATION_INVALID", "executor targets are invalid")
        executor_accounts: set[str] = set()
        for index, item in enumerate(executors):
            if not isinstance(item, Mapping) or set(item) != _EXECUTOR_KEYS:
                raise RoleConfigurationError("ROLE_CONFIGURATION_INVALID", "executor target fields are invalid")
            account = _account(item.get("account"), field=f"executor account {region}[{index}]")
            _safe_label(item.get("name"), field=f"executor name {region}[{index}]")
            if account in executor_accounts:
                raise RoleConfigurationError("ROLE_CONFIGURATION_DUPLICATE", "executor account is duplicated in a region")
            executor_accounts.add(account)
            executor_count += 1
        managers = _string_list(private_region.get("managers"), field=f"managers {region}", account_values=True)
        manager_count += len(managers)

    fixed = _string_list(document.get("price_manager_fixed"), field="price_manager_fixed", account_values=True)
    optional_text: dict[str, Any] = {}
    if document.get("purchase_target") is not None:
        optional_text["purchase_target"] = _safe_label(
            document.get("purchase_target"), field="purchase_target"
        )
    if document.get("approval") is not None:
        optional_text["approval"] = _safe_label(document.get("approval"), field="approval")

    result: dict[str, Any] = {
        "status": "valid",
        "schema": SCHEMA,
        "version": VERSION,
        "content_sha256": _digest_bytes(raw) if raw is not None else _digest_document(document),
        "source_sha256": source["content_sha256"],
        "source_kind": source["kind"],
        "region_count": len(public_regions),
        "executor_count": executor_count,
        "manager_count": manager_count,
        "price_manager_fixed_count": len(fixed),
        "purchase_target_present": "purchase_target" in optional_text,
        "public_contract_sha256": _digest_document(public)
        if raw is None
        else None,
    }
    if path is not None:
        result["active_path"] = str(path)
    if result.get("public_contract_sha256") is None:
        # The caller supplies the real public-contract digest separately when
        # reading from disk; this marker prevents a fake value in summaries.
        result.pop("public_contract_sha256", None)
    return result


def _validate_legacy_shape(
    legacy: Mapping[str, Any],
    public: Mapping[str, Any],
    *,
    raw: bytes,
) -> dict[str, Any]:
    # The existing legacy reference predates a version field.  Absence is
    # accepted only for this explicit import source; a present, wrong version
    # is rejected rather than silently treated as the old shape.
    if (
        "version" in legacy
        and (
            type(legacy.get("version")) is not int
            or legacy.get("version") != VERSION
        )
    ):
        raise RoleConfigurationError("ROLE_LEGACY_SOURCE_INVALID", "legacy role source version is invalid")
    regions = legacy.get("regions")
    public_regions = public.get("regions")
    if not isinstance(regions, Mapping) or not isinstance(public_regions, Mapping):
        raise RoleConfigurationError("ROLE_LEGACY_SOURCE_INVALID", "legacy role source has no valid regions")
    if set(regions) != set(public_regions):
        raise RoleConfigurationError(
            "ROLE_LEGACY_SOURCE_REGIONS_MISMATCH",
            "legacy role source regions do not match the public contract",
        )
    for region in sorted(public_regions, key=str):
        definition = regions.get(region)
        if not isinstance(definition, Mapping):
            raise RoleConfigurationError("ROLE_LEGACY_SOURCE_INVALID", "legacy role region is invalid")
        executors = definition.get("executors")
        if not isinstance(executors, list):
            raise RoleConfigurationError("ROLE_LEGACY_SOURCE_INVALID", "legacy executor targets are invalid")
        for index, item in enumerate(executors):
            if not isinstance(item, Mapping) or set(item) != _EXECUTOR_KEYS:
                raise RoleConfigurationError("ROLE_LEGACY_SOURCE_INVALID", "legacy executor target fields are invalid")
            _account(item.get("account"), field=f"legacy executor account {region}[{index}]")
            _safe_label(item.get("name"), field=f"legacy executor name {region}[{index}]")
        _string_list(definition.get("managers"), field=f"legacy managers {region}", account_values=True)
        old_departments = definition.get("dynamic_sales_departments")
        if old_departments is not None and old_departments != public_regions[region].get("task_sales_departments"):
            raise RoleConfigurationError(
                "ROLE_LEGACY_PUBLIC_DRIFT",
                "legacy private reference disagrees with the public department contract",
            )
    fixed = _string_list(legacy.get("price_manager_fixed"), field="legacy price_manager_fixed", account_values=True)
    return {
        "status": "ready",
        "schema": SCHEMA,
        "version": VERSION,
        "content_sha256": _digest_bytes(raw),
        "source_sha256": _digest_bytes(raw),
        "source_kind": "legacy_recipient_reference",
        "region_count": len(public_regions),
        "executor_count": sum(len(regions[r].get("executors", [])) for r in public_regions),
        "manager_count": sum(len(regions[r].get("managers", [])) for r in public_regions),
        "price_manager_fixed_count": len(fixed),
        "purchase_target_present": legacy.get("purchase_target") is not None,
        "legacy_source": True,
    }


def _legacy_to_private(
    legacy: Mapping[str, Any],
    public: Mapping[str, Any],
    *,
    raw: bytes,
) -> dict[str, Any]:
    _validate_legacy_shape(legacy, public, raw=raw)
    public_regions = public["regions"]
    regions: dict[str, dict[str, Any]] = {}
    for region in sorted(public_regions, key=str):
        old = legacy["regions"][region]
        regions[region] = {
            "executors": [
                {"account": str(item["account"]).strip(), "name": str(item["name"]).strip()}
                for item in old["executors"]
            ],
            "managers": [str(item).strip() for item in old["managers"]],
        }
    source: dict[str, Any] = {
        "kind": "legacy_recipient_reference",
        "content_sha256": _digest_bytes(raw),
        "label": "legacy-recipient-reference.json",
    }
    if legacy.get("reference") is not None:
        source["legacy_reference"] = str(legacy["reference"]).strip()
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "version": VERSION,
        "source": source,
        "regions": regions,
        "price_manager_fixed": [str(value).strip() for value in legacy["price_manager_fixed"]],
    }
    for key in ("purchase_target", "approval"):
        if legacy.get(key) is not None:
            result[key] = legacy[key]
    return result


def _compatibility_reference(
    document: Mapping[str, Any], public: Mapping[str, Any]
) -> dict[str, Any]:
    public_regions = public["regions"]
    private_regions = document["regions"]
    regions: dict[str, dict[str, Any]] = {}
    for region in sorted(public_regions, key=str):
        public_definition = public_regions[region]
        private_definition = private_regions[region]
        task_departments = list(public_definition["task_sales_departments"])
        regions[region] = {
            "departments": list(public_definition["departments"]),
            "task_sales_departments": task_departments,
            "dynamic_sales_departments": task_departments,
            "executors": [
                {"account": item["account"], "name": item["name"]}
                for item in private_definition["executors"]
            ],
            "managers": list(private_definition["managers"]),
        }
    source = document["source"]
    return {
        "reference": source.get("legacy_reference") or source["content_sha256"],
        "approval": document.get("approval") or "review_only_not_send_authorization",
        "regions": regions,
        "price_manager_fixed": list(document["price_manager_fixed"]),
        "purchase_target": document.get("purchase_target"),
    }


def validate(
    source: str | os.PathLike[str] | Mapping[str, Any],
    profile: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Validate a private or legacy source and return metadata only."""

    if isinstance(source, Mapping):
        document = dict(source)
        raw: bytes | None = None
        source_path = None
    else:
        source_path = _as_path(source, code="ROLE_CONFIGURATION_REQUIRED")
        document, raw_bytes = _read_json(source_path, missing_code="ROLE_CONFIGURATION_REQUIRED")
        raw = raw_bytes
    public, public_digest = _public_contract(profile) if profile is not None else (
        None,
        None,
    )
    if document.get("schema") == SCHEMA:
        if public is None:
            raise RoleConfigurationError(
                "ROLE_PUBLIC_CONTRACT_REQUIRED", "role validation requires the public workflow contract"
            )
        result = _validate_private(document, public, raw=raw, path=source_path)
        result["public_contract_sha256"] = public_digest
        return result
    if public is None:
        raise RoleConfigurationError(
            "ROLE_PUBLIC_CONTRACT_REQUIRED", "legacy role validation requires the public workflow contract"
        )
    result = _validate_legacy_shape(document, public, raw=raw or b"")
    result["public_contract_sha256"] = public_digest
    if source_path is not None:
        result["source_path"] = str(source_path)
    return result


def prepare(
    source: str | os.PathLike[str] | Mapping[str, Any],
    profile: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Purely inspect a source before an explicit import or activation."""

    result = validate(source, profile)
    if result.get("legacy_source"):
        result["status"] = "ready_to_import"
        result["target_schema"] = SCHEMA
    else:
        result["status"] = "ready"
    # No raw account/name/manager values are ever added to this return value.
    result.pop("source_path", None)
    return result


def _write_atomic(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # A hard-link publication is atomic and refuses to replace an existing
        # active file on both Windows and POSIX.  The temporary inode already
        # contains the complete JSON, so a concurrent importer cannot expose a
        # partial document or clobber an active configuration.
        os.link(temporary, path)
        try:
            temporary.unlink()
        except OSError:
            # The destination is already durably published; a leftover temp
            # file is harmless and can be cleaned by the operator later.
            pass
    except FileExistsError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise RoleConfigurationError(
            "ROLE_CONFIGURATION_EXISTS",
            "active role configuration already exists; replacement requires a reviewed migration",
        ) from exc
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def import_legacy(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    profile: str | os.PathLike[str],
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Explicitly convert the old private reference into the active schema.

    ``source`` is read only.  ``destination`` is required and must be inside
    the supplied profile's private ``local`` directory.  Existing destinations
    are never overwritten; replacement is intentionally a separate reviewed
    migration and is not exposed by this API.
    """

    source_path = _as_path(source, code="ROLE_IMPORT_SOURCE_REQUIRED")
    destination_path = _as_path(destination, code="ROLE_IMPORT_DESTINATION_REQUIRED")
    root = _as_profile(profile)
    try:
        if not destination_path.is_absolute():
            destination_path = root / destination_path
        local_root = root / "local"
        if local_root.is_symlink() or _is_reparse_point(local_root):
            raise RoleConfigurationError(
                "ROLE_IMPORT_DESTINATION_OUTSIDE_PRIVATE_LOCAL",
                "profile local directory cannot be a symlink or reparse point",
            )
        local_resolved = local_root.resolve()
        local_resolved.relative_to(root)
        destination_resolved = destination_path.resolve()
        destination_resolved.relative_to(local_resolved)
        destination_resolved.relative_to(root)
        component = destination_resolved.parent
        while component != local_resolved and component != component.parent:
            if _is_reparse_point(component):
                raise RoleConfigurationError(
                    "ROLE_IMPORT_DESTINATION_OUTSIDE_PRIVATE_LOCAL",
                    "role import destination contains a reparse-point component",
                )
            component = component.parent
    except RoleConfigurationError:
        raise
    except (OSError, ValueError) as exc:
        raise RoleConfigurationError(
            "ROLE_IMPORT_DESTINATION_OUTSIDE_PRIVATE_LOCAL",
            "role import destination must be inside the profile local directory",
        ) from exc
    if overwrite:
        raise RoleConfigurationError(
            "ROLE_CONFIGURATION_REPLACE_REQUIRES_REVIEW",
            "replacing an active role configuration requires a separately reviewed versioned migration",
        )
    if destination_path.is_symlink():
        raise RoleConfigurationError("ROLE_CONFIGURATION_SYMLINK", "role import destination cannot be a symlink")
    if destination_resolved.exists():
        raise RoleConfigurationError(
            "ROLE_CONFIGURATION_EXISTS",
            "active role configuration already exists; explicit replacement is required",
        )
    legacy, raw = _read_json(source_path, missing_code="ROLE_IMPORT_SOURCE_REQUIRED")
    if legacy.get("schema") == SCHEMA:
        raise RoleConfigurationError(
            "ROLE_IMPORT_SOURCE_NOT_LEGACY",
            "roles-import requires an explicit legacy recipient reference",
        )
    public, public_digest = _public_contract(root)
    document = _legacy_to_private(legacy, public, raw=raw)
    summary = _validate_private(document, public, raw=None, path=destination_resolved)
    summary["public_contract_sha256"] = public_digest
    _write_atomic(destination_resolved, document)
    summary["content_sha256"] = _digest_bytes(destination_resolved.read_bytes())
    summary["status"] = "imported"
    summary["destination"] = str(destination_resolved)
    return summary


def load(profile: str | os.PathLike[str]) -> dict[str, Any]:
    """Load the active private role map for existing workflow callers.

    No legacy path is probed here.  The returned mapping retains the old
    ``regions``/``price_manager_fixed`` shape so existing workflow planners
    keep their execution grain while public departments are derived afresh
    from the contract.
    """

    root = _as_profile(profile)
    path = active_path(root)
    document, raw = _read_json(path, missing_code="ROLE_CONFIGURATION_REQUIRED")
    public, _public_digest = _public_contract(root, prefer_pinned=True)
    if document.get("schema") != SCHEMA:
        raise RoleConfigurationError(
            "ROLE_CONFIGURATION_INVALID",
            "active role configuration must use the versioned private schema",
        )
    _validate_private(document, public, raw=raw, path=path)
    return _compatibility_reference(document, public)


def diagnose(profile: str | os.PathLike[str]) -> dict[str, Any]:
    """Return a no-personnel readiness result for the CLI/status surface."""

    root = _as_profile(profile)
    path = active_path(root)
    base = {"active_path": str(path), "schema": SCHEMA}
    if path.is_symlink() or not path.is_file():
        return {
            **base,
            "status": "blocked",
            "code": "ROLE_CONFIGURATION_REQUIRED",
        }
    try:
        document, raw = _read_json(path, missing_code="ROLE_CONFIGURATION_REQUIRED")
        public, public_digest = _public_contract(root)
        summary = _validate_private(document, public, raw=raw, path=path)
        summary["public_contract_sha256"] = public_digest
        summary["active_path"] = str(path)
        summary["status"] = "ready"
        return summary
    except RoleConfigurationError as exc:
        return {
            **base,
            "status": "blocked",
            "code": exc.code,
        }
