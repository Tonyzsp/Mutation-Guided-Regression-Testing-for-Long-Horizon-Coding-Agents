"""Build local, method-blind candidate bundles without exporting generated code."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "artifacts" / "results" / "controlled_pairwise_v1_20261019" / "sample_manifest.json"
SEED = 20261020


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def generation_dir(root: Path, pair: dict[str, Any], method: str) -> Path:
    return root / pair["task_id"] / slug(pair["mutant_id"]) / method


def legal_base_files(package_root: Path, relative_root: str) -> list[tuple[str, str]]:
    base_root = (package_root / relative_root).resolve(strict=True)
    records = []
    for path in sorted(p for p in base_root.rglob("*") if p.is_file()):
        if any(part.lower() in {"hidden", "future", "excluded", "reference"} for part in path.relative_to(base_root).parts):
            raise ValueError(f"prohibited tests path present in package base suite: {path}")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        records.append((path.relative_to(base_root).as_posix(), text))
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    manifest_path = args.manifest.resolve(strict=True)
    manifest = read_json(manifest_path)
    output_root = args.output_root.resolve() if args.output_root else manifest_path.parent.resolve()
    review_root = output_root / "blind_reviews" / "local_blind_review"
    review_root.mkdir(parents=True, exist_ok=True)
    mapping: list[dict[str, Any]] = []

    for task in sorted(manifest["tasks"]):
        package_root = ROOT / manifest["tasks"][task]["package"]
        package = read_json(package_root / "package.json")
        specification = (package_root / package["specification_path"]).read_text(encoding="utf-8")
        base_files = legal_base_files(package_root, package["base_tests_path"])
        candidates = []
        for pair in manifest["pairs"]:
            if pair["task_id"] != task:
                continue
            for method in ("vanilla_llm", "mutation_guided"):
                run_dir = generation_dir(output_root, pair, method)
                response_path = run_dir / "candidate_response.json"
                if not response_path.is_file():
                    continue
                response = read_json(response_path)
                for candidate in response.get("tests", []):
                    candidates.append({
                        "pair_id": pair["pair_id"],
                        "method": method,
                        "run_dir": run_dir,
                        "candidate_path": candidate["relative_path"],
                        "content": candidate["content"],
                        "content_sha256": hashlib.sha256(candidate["content"].encode("utf-8")).hexdigest(),
                    })
        rng = random.Random(SEED + sum(ord(char) for char in task))
        rng.shuffle(candidates)
        candidate_sections = []
        for index, item in enumerate(candidates, 1):
            alias = f"test_pair_{index:03d}.py"
            item["alias"] = alias
            candidate_sections.append(f"### {alias}\n\n```python\n{item['content'].rstrip()}\n```")
            mapping.append({
                "task_id": task,
                "alias": alias,
                "pair_id": item["pair_id"],
                "method": item["method"],
                "generation_dir": str(item["run_dir"].relative_to(ROOT).as_posix()),
                "candidate_path": item["candidate_path"],
                "content_sha256": item["content_sha256"],
            })

        base_sections = [f"### base_tests/{name}\n\n```python\n{text.rstrip()}\n```" for name, text in base_files]
        bundle = f"""# Local blind review bundle — {task}

Review each anonymized candidate independently. You see only the current C1 specification, complete legal C1 base tests, and candidate test code. Do not infer unsupported behavior or use hidden/future tests, later checkpoints, external data, or mutant information.

For each candidate, assign one verdict:
- **correct** — every expected behavior is required by the public specification, externally observable, and deterministic;
- **incorrect** — at least one assertion is unsupported, conflicts with the specification, or relies on internal implementation details;
- **uncertain** — the supplied specification and base tests do not settle the assertion.

Provide a short rationale naming the relevant requirement or missing evidence. Do not provide private chain-of-thought. This is a local review artifact; candidate code has not been sent to an external reviewer.

## C1 specification

{specification}

## Complete legal C1 base tests

{chr(10).join(base_sections)}

## Anonymized candidates

{chr(10).join(candidate_sections)}
"""
        (review_root / f"{task}.md").write_text(bundle, encoding="utf-8")
        (review_root / f"{task}.summary.json").write_text(json.dumps({
            "task_id": task,
            "candidate_count": len(candidates),
            "base_test_file_count": len(base_files),
            "blind_order_seed": SEED,
            "review_rubric": "correct/incorrect/uncertain per public specification; no method, mutant, or specification-basis fields shown",
            "bundle_sha256": hashlib.sha256(bundle.encode("utf-8")).hexdigest(),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    (review_root / "mapping.json").write_text(json.dumps({
        "blind_order_seed": SEED,
        "method_labels_hidden_from_bundles": True,
        "mapping_is_not_included_in_review_bundles": True,
        "records": mapping,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Built local blind-review bundles for {len(manifest['tasks'])} tasks with {len(mapping)} candidate tests.")
    print(f"Bundles: {review_root}")
    print(f"Private mapping: {review_root / 'mapping.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
