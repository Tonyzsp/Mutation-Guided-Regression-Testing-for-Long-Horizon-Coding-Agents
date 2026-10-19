"""Propagate mutmut trampoline hits from benchmark CLI subprocesses to pytest."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mutmut.stats import record_trampoline_hit


_STATS_RUN = "stats"
_TEST_ID_ENV = "RQ1_MUTMUT_TEST_ID"
_HITS_PATH_ENV = "RQ1_MUTMUT_HITS_PATH"
_PARENT_PID_ENV = "RQ1_MUTMUT_PARENT_PID"


def _hits_path() -> Path | None:
    value = os.environ.get(_HITS_PATH_ENV)
    return Path(value) if value else None


def pytest_sessionstart(session: pytest.Session) -> None:
    """Start a fresh child-hit log for each mutmut stats pytest session."""
    del session
    if os.environ.get("MUTANT_UNDER_TEST") != _STATS_RUN:
        return

    hits_path = _hits_path()
    if hits_path is None:
        return

    hits_path.parent.mkdir(parents=True, exist_ok=True)
    hits_path.write_text("", encoding="utf-8")
    os.environ[_PARENT_PID_ENV] = str(os.getpid())


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

    hits_path = _hits_path()
    test_id = item.nodeid
    os.environ.pop(_TEST_ID_ENV, None)
    if hits_path is None or not hits_path.is_file():
        return

    for line_number, line in enumerate(hits_path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Malformed mutmut subprocess hit record at {hits_path}:{line_number}"
            ) from error
        if record.get("test_id") == test_id:
            record_trampoline_hit(record["name"], caller=record.get("caller"))
