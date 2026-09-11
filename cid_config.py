"""Load and validate repository-owned ``.cid/build.yaml`` contracts."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

import yaml


class CidConfigError(ValueError):
    """Raised when a repository CID contract is unsafe or malformed."""


def _reject_forbidden(value: Any, location: str = "root") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"dependencies", "machine", "continue_on_error"}:
                raise CidConfigError(f"{location}.{key} is managed by robot-ci and must not be configured")
            _reject_forbidden(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden(child, f"{location}[{index}]")


def load_cid_config(repo_dir: Path, expected_service_id: str = "") -> dict[str, Any] | None:
    path = repo_dir / ".cid" / "build.yaml"
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise CidConfigError(f"cannot load {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CidConfigError("CID root must be a mapping")
    _reject_forbidden(data)
    if data.get("version") != 1:
        raise CidConfigError("CID version must be 1")
    service = data.get("service")
    if not isinstance(service, dict):
        raise CidConfigError("service must be a mapping")
    service_id = str(service.get("id") or "").strip()
    if not service_id:
        raise CidConfigError("service.id is required")
    if expected_service_id and service_id != expected_service_id:
        raise CidConfigError(
            f"service.id mismatch: expected {expected_service_id}, found {service_id}"
        )
    image = str(service.get("image") or "").strip()
    if not image:
        raise CidConfigError("service.image is required")
    scripts = data.get("scripts")
    if not isinstance(scripts, list) or not scripts:
        raise CidConfigError("scripts must be a non-empty list")
    seen: set[str] = set()
    build_count = 0
    for index, step in enumerate(scripts):
        if not isinstance(step, dict):
            raise CidConfigError(f"scripts[{index}] must be a mapping")
        step_id = str(step.get("id") or "").strip()
        if not step_id or step_id in seen:
            raise CidConfigError(f"scripts[{index}].id is missing or duplicated")
        seen.add(step_id)
        step_type = str(step.get("type") or "").strip()
        if step_type not in {"test", "build"}:
            raise CidConfigError(f"scripts[{index}].type must be test or build")
        enabled = step.get("enabled")
        if not isinstance(enabled, bool):
            raise CidConfigError(f"scripts[{index}].enabled must be boolean")
        if step_type == "build":
            build_count += int(enabled)
        if enabled and not str(step.get("command") or "").strip():
            raise CidConfigError(f"scripts[{index}].command is required when enabled")
        if step_type == "test" and enabled:
            if step.get("test_type") not in {"ut", "dt", "gamma"}:
                raise CidConfigError(f"scripts[{index}].test_type must be ut, dt or gamma")
            report = step.get("report")
            if not isinstance(report, dict):
                raise CidConfigError(f"scripts[{index}].report is required")
            if report.get("format") not in {"junit", "go-json", "case-json", "unittest"}:
                raise CidConfigError(f"scripts[{index}].report.format is unsupported")
            if not str(report.get("path") or "").strip():
                raise CidConfigError(f"scripts[{index}].report.path is required")
    if build_count != 1:
        raise CidConfigError("exactly one enabled build script is required")
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, dict):
        raise CidConfigError("artifacts must be a mapping")
    image_artifact = artifacts.get("image")
    if not isinstance(image_artifact, dict):
        raise CidConfigError("artifacts.image must be a mapping")
    image_enabled = image_artifact.get("enabled")
    if not isinstance(image_enabled, bool):
        raise CidConfigError("artifacts.image.enabled must be boolean")
    image_delivery = str(image_artifact.get("delivery") or "").strip()
    if image_delivery not in {"swr", "archive-only"}:
        raise CidConfigError("artifacts.image.delivery must be swr or archive-only")
    if "archive" in image_artifact and not isinstance(image_artifact.get("archive"), bool):
        raise CidConfigError("artifacts.image.archive must be boolean")

    package_artifact = artifacts.get("package")
    package_enabled = False
    if package_artifact is not None:
        if not isinstance(package_artifact, dict):
            raise CidConfigError("artifacts.package must be a mapping")
        package_enabled = package_artifact.get("enabled")
        if not isinstance(package_enabled, bool):
            raise CidConfigError("artifacts.package.enabled must be boolean")
        if package_enabled:
            pattern = str(package_artifact.get("pattern") or "").strip().replace("\\", "/")
            path = PurePosixPath(pattern)
            if not pattern or path.is_absolute() or ".." in path.parts:
                raise CidConfigError("artifacts.package.pattern must be a safe relative glob")
            delivery = str(package_artifact.get("delivery") or "archive-only").strip()
            if delivery != "archive-only":
                raise CidConfigError("artifacts.package.delivery must be archive-only")
    if not image_enabled and not package_enabled:
        raise CidConfigError("at least one artifact must be enabled")
    return data


def enabled_build_step(config: dict[str, Any]) -> dict[str, Any]:
    for step in config.get("scripts") or []:
        if step.get("type") == "build" and step.get("enabled") is True:
            return step
    raise CidConfigError("enabled build script was not found")


def _report_relative_path(configured: str, step_id: str) -> str:
    path = PurePosixPath(configured.replace("\\", "/"))
    parts = path.parts
    marker = (".cid", "output", "reports")
    for index in range(max(0, len(parts) - len(marker) + 1)):
        if parts[index : index + len(marker)] == marker:
            suffix = parts[index + len(marker) :]
            if suffix:
                return PurePosixPath(*suffix).as_posix()
    return PurePosixPath(step_id, path.name).as_posix()


def build_test_plan(
    config: dict[str, Any], *, test_types: set[str] | None = None
) -> dict[str, Any]:
    """Build a runner plan from one CID contract.

    Unit/deployment tests run before building by default.  Gamma tests are a
    separate post-rollout gate and callers must opt into them explicitly.
    """
    selected_types = test_types or {"ut", "dt"}
    service_id = str(config["service"]["id"])
    commands: list[dict[str, Any]] = []
    for step in config.get("scripts") or []:
        if (
            step.get("type") != "test"
            or step.get("enabled") is not True
            or step.get("test_type") not in selected_types
        ):
            continue
        report = step["report"]
        commands.append(
            {
                "name": str(step.get("name") or step["id"]),
                "command": str(step["command"]),
                "parser": str(report["format"]),
                "report": _report_relative_path(str(report["path"]), str(step["id"])),
                "timeout_sec": int(step.get("timeout_sec") or 1200),
                "test_type": str(step.get("test_type") or ""),
            }
        )
    if not commands:
        return {"version": 1, "services": {}, "profiles": {}}
    profile = f"cid-{service_id}"
    return {
        "version": 1,
        "services": {service_id: profile},
        "profiles": {profile: {"commands": commands}},
    }
