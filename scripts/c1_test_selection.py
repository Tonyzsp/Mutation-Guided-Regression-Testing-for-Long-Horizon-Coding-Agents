"""Helpers for selecting the stable C1 tests without changing pytest order."""

from __future__ import annotations

from typing import Any


def stable_pass_selection(baseline_record: dict[str, Any]) -> list[str]:
    """Return stable passing node IDs in their original base-suite collection order.

    The baseline audit stores ``stable_pass_test_ids`` sorted for deterministic
    reporting. Passing that sorted list directly to pytest changes execution
    order for stateful suites, so recover the order from the first recorded run.
    """
    stable_ids = [str(item) for item in baseline_record.get("stable_pass_test_ids", [])]
    stable_set = set(stable_ids)
    repetitions = baseline_record.get("repetitions", [])
    observed = repetitions[0].get("tests", []) if repetitions else []

    ordered: list[str] = []
    seen: set[str] = set()
    for item in observed:
        nodeid = str(item.get("nodeid", ""))
        if nodeid in stable_set and nodeid not in seen:
            ordered.append(nodeid)
            seen.add(nodeid)

    # Preserve deterministic behavior if an old or partial audit lacks entries.
    ordered.extend(sorted(stable_set - seen))
    return ordered
