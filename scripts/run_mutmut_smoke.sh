#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fixture_source="$repo_root/fixtures/mutmut-smoke"
results_dir="${MUTMUT_RESULTS_DIR:-$fixture_source/results}"
venv="${MUTMUT_VENV:-$HOME/.venvs/mutmut-smoke}"
run_dir="$(mktemp -d /tmp/mutmut-smoke-XXXXXX)"

mkdir -p "$results_dir"
cp -a "$fixture_source/pyproject.toml" "$fixture_source/src" "$fixture_source/tests" "$run_dir/"
cd "$run_dir"

{
    date --iso-8601=seconds
    uname -a
    "$venv/bin/python" --version
    "$venv/bin/python" -m pip freeze
} > "$results_dir/environment.txt"

"$venv/bin/python" -m pytest tests/base -q > "$results_dir/pytest-base.log" 2>&1
"$venv/bin/coverage" run --source=src -m pytest tests/base -q > "$results_dir/coverage-run.log" 2>&1
"$venv/bin/coverage" report -m > "$results_dir/coverage-report.log" 2>&1
if ! "$venv/bin/mutmut" run > "$results_dir/mutmut-run.log" 2>&1; then
    tail -n 100 "$results_dir/mutmut-run.log"
    exit 1
fi

target_mutant="fixture_pkg.discount.x_discount_rate__mutmut_1"
"$venv/bin/mutmut" results --all true > "$results_dir/mutmut-results.log"
"$venv/bin/mutmut" show "$target_mutant" > "$results_dir/guided-target-mutant.diff"
grep -Fq "$target_mutant: survived" "$results_dir/mutmut-results.log"
grep -Fq '+    if amount >= 100:' "$results_dir/guided-target-mutant.diff"

# The candidate must pass on the original and kill this surviving mutant.
"$venv/bin/python" -m pytest tests/candidate/test_discount_boundary.py -q > "$results_dir/guided-candidate-original.log" 2>&1
"$venv/bin/mutmut" apply "$target_mutant" > "$results_dir/mutmut-apply.log" 2>&1
PYTHONPATH="$run_dir/src" "$venv/bin/python" -c 'import fixture_pkg.discount as module; from pathlib import Path; actual = Path(module.__file__).resolve(); expected = Path("src/fixture_pkg/discount.py").resolve(); assert actual == expected, (actual, expected); assert module.discount_rate(100) == 10; print(f"Loaded mutated module: {actual}")' > "$results_dir/guided-mutant-import.log" 2>&1
if "$venv/bin/python" -m pytest tests/candidate/test_discount_boundary.py -q > "$results_dir/guided-candidate-mutant.log" 2>&1; then
    printf 'Expected candidate test to fail on target mutant, but it passed.\n' >&2
    exit 1
fi
grep -Fq 'assert 10 == 0' "$results_dir/guided-candidate-mutant.log"

printf '%s\n' "$run_dir" > "$results_dir/linux_run_directory.txt"
tail -n 60 "$results_dir/mutmut-run.log"
cat "$results_dir/guided-mutant-import.log"
tail -n 10 "$results_dir/guided-candidate-mutant.log"
printf 'Smoke run completed. Logs: %s\n' "$results_dir"
