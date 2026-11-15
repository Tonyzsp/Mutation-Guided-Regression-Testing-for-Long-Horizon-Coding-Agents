"""Validate shared JSON Schemas against the Draft 2020-12 meta-schema."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator


SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


def main() -> None:
    schema_files = sorted(SCHEMA_DIR.glob("*.schema.json"))
    if not schema_files:
        raise SystemExit(f"No schema files found in {SCHEMA_DIR}")

    for schema_path in schema_files:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        print(f"OK {schema_path.name}")


if __name__ == "__main__":
    main()
