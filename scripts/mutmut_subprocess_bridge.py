"""Propagate mutmut trampoline hits from benchmark CLI subprocesses to pytest."""

from __future__ import annotations

import json
import os
from pathlib import Path
from functools import wraps

import pytest

from mutmut.stats import record_trampoline_hit


_STATS_RUN = "stats"
_TEST_ID_ENV = "RQ1_MUTMUT_TEST_ID"
_HITS_PATH_ENV = "RQ1_MUTMUT_HITS_PATH"
_PARENT_PID_ENV = "RQ1_MUTMUT_PARENT_PID"
_TEST_ORDER_ENV = "RQ1_MUTMUT_TEST_ORDER_JSON"
_HITS_OFFSETS: dict[Path, int] = {}
_PARTIAL_HIT_LINES: dict[Path, bytes] = {}
_HTTP_TEST_ID_HEADER = "X-RQ1-Mutmut-Test-Id"


def _hits_path() -> Path | None:
    value = os.environ.get(_HITS_PATH_ENV)
    return Path(value) if value else None


def pytest_collection_modifyitems(
    session: pytest.Session,
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Keep Mutmut's per-mutant subset in the original base-suite order."""
    del session, config
    raw_order = os.environ.get(_TEST_ORDER_ENV)
    if not raw_order:
        return
    try:
        ordered_ids = json.loads(raw_order)
    except json.JSONDecodeError as error:
        raise pytest.UsageError(f"invalid {_TEST_ORDER_ENV}: {error}") from error
    if not isinstance(ordered_ids, list) or any(not isinstance(item, str) for item in ordered_ids):
        raise pytest.UsageError(f"{_TEST_ORDER_ENV} must be a JSON array of pytest node IDs")
    rank = {nodeid: index for index, nodeid in enumerate(ordered_ids)}
    items.sort(key=lambda item: (rank.get(item.nodeid, len(rank)), item.nodeid))


def _install_http_test_context() -> None:
    """Carry the active pytest node ID into long-lived local HTTP servers."""
    if os.environ.get("MUTANT_UNDER_TEST") != _STATS_RUN:
        return
    try:
        import httpx
    except ImportError:
        return

    original_send = httpx.Client.send

    @wraps(original_send)
    def send_with_test_id(self, request, *args, **kwargs):
        test_id = os.environ.get(_TEST_ID_ENV)
        if test_id:
            request.headers[_HTTP_TEST_ID_HEADER] = test_id
        return original_send(self, request, *args, **kwargs)

    httpx.Client.send = send_with_test_id


def pytest_sessionstart(session: pytest.Session) -> None:
    """Start a fresh child-hit log for each mutmut stats pytest session."""
    global _HITS_OFFSETS, _PARTIAL_HIT_LINES
    del session
    if os.environ.get("MUTANT_UNDER_TEST") != _STATS_RUN:
        return

    hits_path = _hits_path()
    if hits_path is None:
        return

    hits_path.parent.mkdir(parents=True, exist_ok=True)
    hits_path.write_text("", encoding="utf-8")
    for child_hits_path in hits_path.parent.glob(f"{hits_path.name}.*.jsonl"):
        child_hits_path.unlink()
    _HITS_OFFSETS = {}
    _PARTIAL_HIT_LINES = {}
    os.environ[_PARENT_PID_ENV] = str(os.getpid())
    _install_http_test_context()


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Tag subprocesses with the test currently being executed."""
    if os.environ.get("MUTANT_UNDER_TEST") == _STATS_RUN:
        os.environ[_TEST_ID_ENV] = item.nodeid


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    """Import child-process hits before mutmut's own teardown hook reads them."""
    del nextitem
    if os.environ.get("MUTANT_UNDER_TEST") != _STATS_RUN:
        return

    global _HITS_OFFSETS, _PARTIAL_HIT_LINES
    hits_path = _hits_path()
    test_id = item.nodeid
    os.environ.pop(_TEST_ID_ENV, None)
    if hits_path is None:
        return

    # Each child PID writes its own file, so multiple benchmark subprocesses
    # cannot interleave JSONL records. Read only appended bytes to keep this
    # linear in the number of subprocess hits.
    child_paths = sorted(hits_path.parent.glob(f"{hits_path.name}.*.jsonl"))
    with hits_path.open("ab") as aggregate:
        for child_path in child_paths:
            if child_path not in _HITS_OFFSETS:
                _HITS_OFFSETS[child_path] = 0
                _PARTIAL_HIT_LINES[child_path] = b""
            with child_path.open("rb") as handle:
                handle.seek(_HITS_OFFSETS[child_path])
                appended = handle.read()
                _HITS_OFFSETS[child_path] = handle.tell()
            lines = (_PARTIAL_HIT_LINES[child_path] + appended).splitlines(keepends=True)
            if lines and not lines[-1].endswith((b"\n", b"\r")):
                _PARTIAL_HIT_LINES[child_path] = lines.pop()
            else:
                _PARTIAL_HIT_LINES[child_path] = b""

            for raw_line in lines:
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise RuntimeError(
                        f"Malformed mutmut subprocess hit record at {child_path}"
                    ) from error
                aggregate.write(raw_line)
                if record.get("test_id") == test_id:
                    record_trampoline_hit(record["name"], caller=record.get("caller"))
