"""Probe bidirectional routing using a synthetic one-sided SDE jump row."""

from __future__ import annotations

import bz2
import csv
import subprocess
from pathlib import Path


def write_sde_csv(
    root: Path,
    filename: str,
    columns: tuple[str, ...],
    rows: list[dict[str, str]],
) -> None:
    with bz2.open(root / filename, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def create_one_sided_sde(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    write_sde_csv(
        root,
        "mapSolarSystems.csv.bz2",
        ("solarSystemID", "solarSystemName", "security"),
        [
            {"solarSystemID": "100", "solarSystemName": "Alpha", "security": "0.5"},
            {"solarSystemID": "200", "solarSystemName": "Beta", "security": "0.7"},
        ],
    )
    write_sde_csv(
        root,
        "mapSolarSystemJumps.csv.bz2",
        ("fromSolarSystemID", "toSolarSystemID"),
        [{"fromSolarSystemID": "100", "toSolarSystemID": "200"}],
    )
    write_sde_csv(
        root,
        "staStations.csv.bz2",
        ("stationName", "solarSystemID", "x", "y", "z"),
        [],
    )
    write_sde_csv(
        root,
        "mapDenormalize.csv.bz2",
        ("itemName", "solarSystemID", "x", "y", "z"),
        [
            {
                "itemName": "Stargate (Beta)",
                "solarSystemID": "100",
                "x": "0",
                "y": "0",
                "z": "0",
            },
            {
                "itemName": "Stargate (Alpha)",
                "solarSystemID": "200",
                "x": "0",
                "y": "0",
                "z": "0",
            },
        ],
    )


def test_one_sided_jump_record_supports_both_route_directions(
    entrypoint_argv: list[str], tmp_path: Path
) -> None:
    sde_dir = tmp_path / "synthetic_sde"
    create_one_sided_sde(sde_dir)

    for start, end, route in (
        ("Alpha", "Beta", "GO: Alpha (0.5) -> Beta (0.7)"),
        ("Beta", "Alpha", "GO: Beta (0.7) -> Alpha (0.5)"),
    ):
        result = subprocess.run(
            [
                *entrypoint_argv,
                start,
                end,
                "--align",
                "1",
                "--top-speed",
                "0",
                "--warp-speed",
                "3",
                "--dock-time",
                "1",
                "--gate-time",
                "1",
                "--sde",
                str(sde_dir),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().splitlines() == [
            f"START: {start}",
            route,
            "DONE: 00:01",
        ]
