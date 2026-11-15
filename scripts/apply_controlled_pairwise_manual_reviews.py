"""Apply frozen, anonymized specification-review verdicts to paired runs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = ROOT / "artifacts" / "results" / "controlled_pairwise_v1_20261019"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    experiment_root = args.experiment_root.resolve(strict=True)
    if not experiment_root.is_relative_to((ROOT / "artifacts" / "results").resolve()):
        raise ValueError("experiment root must remain under artifacts/results")

    blind_root = experiment_root / "blind_reviews" / "local_blind_review"
    mapping = read_json(blind_root / "mapping.json")
    verdicts = read_json(blind_root / "manual_verdicts_blind.json")
    records: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for record in mapping["records"]:
        key = (record["task_id"], record["pair_id"], record["method"], record["candidate_path"])
        if key in records:
            raise ValueError(f"duplicate blind mapping record: {key}")
        records[key] = record

    method_names = ("vanilla_llm", "mutation_guided")
    expected_review_keys: set[tuple[str, str, str, str]] = set()
    written = 0
    for task_id_dir in sorted(experiment_root.iterdir()):
        if not task_id_dir.is_dir() or task_id_dir.name in {"blind_reviews"}:
            continue
        for mutant_dir in sorted(task_id_dir.iterdir()):
            if not mutant_dir.is_dir():
                continue
            for method in method_names:
                run_dir = mutant_dir / method
                response_path = run_dir / "candidate_response.json"
                if not response_path.is_file():
                    continue
                response = read_json(response_path)
                candidates = response.get("tests", [])
                if not isinstance(candidates, list):
                    raise ValueError(f"tests must be an array: {response_path}")
                reviews: list[dict[str, str]] = []
                for candidate in candidates:
                    relative_path = candidate.get("relative_path")
                    content = candidate.get("content")
                    if not isinstance(relative_path, str) or not isinstance(content, str):
                        raise ValueError(f"candidate needs string relative_path/content: {response_path}")
                    key = (task_id_dir.name, mutant_dir.name, method, relative_path)
                    record = records.get(key)
                    if record is None:
                        raise ValueError(f"candidate missing from frozen blind mapping: {key}")
                    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    if digest != record["content_sha256"]:
                        raise ValueError(f"candidate changed after blind review: {key}")
                    decision = verdicts["verdicts"][task_id_dir.name][record["alias"]]
                    reviews.append({
                        "candidate_path": relative_path,
                        "verdict": decision["verdict"],
                        "rationale": decision["rationale"],
                        "reviewer": verdicts["reviewer"],
                    })
                    expected_review_keys.add(key)

                review_path = run_dir / "correctness_review.json"
                if review_path.exists():
                    raise FileExistsError(f"refusing to overwrite existing review: {review_path}")
                write_json(review_path, {"reviews": reviews})
                written += 1

    if expected_review_keys != set(records):
        missing = sorted(set(records) - expected_review_keys)
        raise ValueError(f"not all blind-mapped candidates were applied: {missing[:5]}")
    print(f"Wrote {written} per-run reviews; reviewed candidates: {len(expected_review_keys)}")
    print("Zero-candidate generation runs received an empty review array.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
