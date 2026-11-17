"""Write mutmut stats hits made by benchmark CLI child processes to JSONL."""

from __future__ import annotations

import json
import os
import threading
from functools import wraps


_HIT_WRITE_LOCK = threading.Lock()
_REQUEST_TEST_CONTEXT = threading.local()
_HTTP_TEST_ID_HEADER = "X-RQ1-Mutmut-Test-Id"


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
        test_id = getattr(_REQUEST_TEST_CONTEXT, "test_id", None) or os.environ.get(
            "RQ1_MUTMUT_TEST_ID"
        )
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
        # Separate files avoid interleaved writes from concurrent subprocesses
        # on Docker Desktop bind mounts, where O_APPEND is not reliably atomic.
        child_hits_path = f"{hits_path}.{os.getpid()}.jsonl"
        with _HIT_WRITE_LOCK:
            descriptor = os.open(child_hits_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, record)
            finally:
                os.close(descriptor)

    mutmut_stats.record_trampoline_hit = record_trampoline_hit

    # The SCBench execution_server keeps one HTTP server process alive for an
    # entire pytest class. Forward the current test ID through the request so
    # child-process hits are attributed to the test that caused that request.
    try:
        from http.server import BaseHTTPRequestHandler
    except ImportError:
        return

    original_parse_request = BaseHTTPRequestHandler.parse_request

    @wraps(original_parse_request)
    def parse_request_with_test_id(self):
        parsed = original_parse_request(self)
        if parsed:
            test_id = self.headers.get(_HTTP_TEST_ID_HEADER)
            if test_id:
                _REQUEST_TEST_CONTEXT.test_id = test_id
        return parsed

    BaseHTTPRequestHandler.parse_request = parse_request_with_test_id


_install_subprocess_hit_recorder()
