"""Build per-benchmark data files under data/bench/ from data/raw/.

Usage: python -I scripts/prepare_bench.py <repo root>
"""

import json
import sys
from pathlib import Path


def rows(path: Path):
    with path.open() as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def write(path: Path, items) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w") as f:
        for item in items:
            f.write(json.dumps({"id": item["id"], "input": item["input"], "label": item["label"]}) + "\n")
            n += 1
    return n


def main(root: Path) -> None:
    raw, out = root / "data/raw", root / "data/bench"
    counts = {}
    # OpenPhish rows are licensed for academic research only; leave them out of publishable runs.
    counts["phishing"] = write(out / "phishing/normalized.jsonl", (
        r for r in rows(raw / "jev_phishing_bench/normalized.jsonl")
        if r.get("meta", {}).get("datasource") not in ("openphish", "openphish_live")))
    counts["banking77"] = write(out / "banking77/normalized.jsonl", rows(raw / "banking77/normalized.jsonl"))
    counts["civil_comments"] = write(out / "civil_comments/normalized.jsonl", rows(raw / "civil_comments/normalized.jsonl"))
    counts["mt_bench"] = write(out / "mt_bench/normalized.jsonl", rows(raw / "mt_bench_human_judgments/normalized.jsonl"))
    for task in ("overruling", "cuad_audit_rights", "definition_classification"):
        counts[f"legal_{task}"] = write(out / f"legal_{task}/normalized.jsonl", (
            r for r in rows(raw / "legalbench/normalized.jsonl")
            if r["meta"]["task"] == task and r["meta"]["split"] == "test"))

    labels = json.loads((raw / "banking77/meta.json").read_text())["label_set"]
    task = root / "bench/banking77/task.yaml"
    lines = [
        "name: banking77",
        "description: >-",
        "  You route customer messages for a digital bank. Pick the single intent that best matches what the",
        "  customer wants.",
        "labels:",
    ] + [f"- {json.dumps(label)}" for label in labels]
    task.write_text("\n".join(lines) + "\n")
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
