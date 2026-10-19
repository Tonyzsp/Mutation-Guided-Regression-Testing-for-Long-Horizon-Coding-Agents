"""Write mutmut stats hits made by benchmark CLI child processes to JSONL."""

from __future__ import annotations

import json
import os


def _install_subprocess_hit_recorder() -> None:
    try:
        from mutmut import stats as mutmut_stats
    except ImportError:
        return

    original_record_trampoline_hit = mutmut_stats.record_trampoline_hit
    main_module = os.environ.get("RQ1_MUTMUT_MAIN_MODULE")

    def record_trampoline_hit(name: str, caller: str | None = None) -> None:
        if main_module and name.startswith("__main__."):
            name = main_module + name[len("__main__"):]
        is_stats_run = os.environ.get("MUTANT_UNDER_TEST") == "stats"
        test_id = os.environ.get("RQ1_MUTMUT_TEST_ID")
        hits_path = os.environ.get("RQ1_MUTMUT_HITS_PATH")
        parent_pid = os.environ.get("RQ1_MUTMUT_PARENT_PID")
        is_captured_child = (
            is_stats_run
            and bool(test_id)
            and bool(hits_path)
            and bool(parent_pid)
            and os.getpid() != int(parent_pid)
        )

        # A subprocess can run from mutmut's temporary mutants/ directory, where
        # mutmut cannot discover the root pyproject.toml. Defer these hits to the
        # pytest parent, which replays them from the project root during teardown.
        if not is_captured_child:
            original_record_trampoline_hit(name, caller=caller)

        if not is_stats_run:
            return
        if not test_id or not hits_path or not parent_pid:
            return
        if os.getpid() == int(parent_pid):
            return

        record = json.dumps(
            {"test_id": test_id, "name": name, "caller": caller},
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        descriptor = os.open(hits_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, record)
        finally:
            os.close(descriptor)

    mutmut_stats.record_trampoline_hit = record_trampoline_hit


_install_subprocess_hit_recorder()
