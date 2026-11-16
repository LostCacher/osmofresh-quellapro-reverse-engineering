#!/usr/bin/env python3
"""Create an evidence table from A/C/D dispense-sequence JSONL exports."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def load_frames(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [
            record for line in handle
            if (record := json.loads(line)).get("record_type") == "frame"
        ]


def field_meanings(frame: dict | None) -> str:
    if not frame:
        return ""
    meanings = [
        str(field.get("meaning", ""))
        for field in frame.get("known_fields", [])
        if str(field.get("meaning", "")).upper() != "UNKNOWN"
    ]
    return "; ".join(dict.fromkeys(meanings))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--a", type=Path, required=True, help="normal Display 150 ml JSONL")
    parser.add_argument("--c", type=Path, required=True, help="manual Mega 10 ml JSONL")
    parser.add_argument("--d", type=Path, required=True, help="replace 150->10 ml JSONL")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    captures = {name: load_frames(path) for name, path in (("A", args.a), ("C", args.c), ("D", args.d))}
    count = max(map(len, captures.values()), default=0)
    columns = (
        "Frame #", "Delta", "Direction", "Normal 150 ml", "Manual 10 ml",
        "Replace 150->10", "Meaning", "Likely required?",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for index in range(count):
            frames = {key: values[index] if index < len(values) else None for key, values in captures.items()}
            reference = frames["A"] or frames["C"] or frames["D"]
            delta = " | ".join(
                f"{key}:{frame['delta_us']}" for key, frame in frames.items() if frame
            )
            meaning = field_meanings(frames["A"]) or field_meanings(frames["D"]) or field_meanings(frames["C"])
            raws = {key: frame.get("raw", "") if frame else "" for key, frame in frames.items()}
            required = "REVIEW"
            if raws["A"] and raws["A"] == raws["D"] and raws["A"] != raws["C"]:
                required = "candidate: display-driven sequence"
            writer.writerow({
                "Frame #": index + 1,
                "Delta": delta,
                "Direction": "" if reference is None else reference.get("direction", ""),
                "Normal 150 ml": raws["A"],
                "Manual 10 ml": raws["C"],
                "Replace 150->10": raws["D"],
                "Meaning": meaning,
                "Likely required?": required,
            })


if __name__ == "__main__":
    main()
