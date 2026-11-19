"""Apply the frozen task-specific exclusions to all model-facing C1 inputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "rq1_model_input_exclusions_v1.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_policy() -> tuple[dict[str, Any], str]:
    raw = POLICY_PATH.read_bytes()
    policy = json.loads(raw.decode("utf-8"))
    if policy.get("format") != "rq1-model-input-exclusions/v1":
        raise ValueError("unsupported model-input exclusion policy")
    if not isinstance(policy.get("policy_id"), str) or not policy["policy_id"]:
        raise ValueError("model-input exclusion policy has no policy ID")
    return policy, hashlib.sha256(raw).hexdigest()


def exclusions_for_package(
    package_root: Path,
    package: dict[str, Any],
    *,
    root_name: str,
    consumer: str,
) -> tuple[set[str], list[dict[str, Any]], str]:
    """Return verified root-relative exclusions and their provenance records."""
    policy, policy_sha = load_policy()
    matches = [
        row for row in policy["exclusions"]
        if row.get("task_id") == package.get("task_id")
        and row.get("checkpoint_id") == package.get("checkpoint_id")
        and row.get("root") == root_name
        and consumer in row.get("applies_to", [])
    ]
    relative_paths: set[str] = set()
    records: list[dict[str, Any]] = []
    package_root = package_root.resolve(strict=True)
    for row in matches:
        relative = row.get("path")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError(f"unsafe model-input exclusion path: {relative!r}")
        root_relative = package.get(f"{root_name}_path")
        if not isinstance(root_relative, str):
            raise ValueError(f"package has no {root_name}_path")
        root = (package_root / Path(root_relative)).resolve(strict=True)
        target = (root / Path(relative)).resolve(strict=True)
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError(f"excluded model input is missing or escapes {root_name}: {relative}")
        actual_sha = sha256_file(target)
        if actual_sha != row.get("sha256"):
            raise ValueError(f"excluded model input hash differs from policy: {package['task_id']}/{relative}")
        if relative in relative_paths:
            raise ValueError(f"duplicate model-input exclusion: {relative}")
        relative_paths.add(relative)
        records.append({
            "path": f"{root_relative}/{relative}",
            "sha256": actual_sha,
            "bytes": target.stat().st_size,
            "reason": row["reason"],
            "policy_id": policy["policy_id"],
        })
    return relative_paths, records, policy_sha
