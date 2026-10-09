#!/usr/bin/env python3
"""Fetch rows of a Hugging Face dataset split via the datasets-server /rows JSON API.

Stdlib only (no pyarrow / datasets needed). Optionally keeps only a seeded uniform
random sample of row indices. Any row whose cells come back truncated is re-fetched
on its own; if it is still truncated the script fails loudly rather than writing
partial text.

Usage:
  python3 -I scripts/fetch_hf_rows.py --dataset google/civil_comments --config default \
      --split test --out data/raw/civil_comments/source/test_sample.jsonl \
      --sample 20000 --seed 42
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = "https://datasets-server.huggingface.co/rows"


def get_page(dataset: str, config: str, split: str, offset: int, length: int, retries: int = 8) -> dict:
    qs = urllib.parse.urlencode(
        {"dataset": dataset, "config": config, "split": split, "offset": offset, "length": length}
    )
    url = f"{API}?{qs}"
    delay = 2.0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sundr-dataset-fetch/0.1"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            retry_after = e.headers.get("Retry-After") if e.headers else None
            e.close()
            print(f"  HTTP {e.code} at offset={offset} (attempt {attempt + 1}, retry-after={retry_after})",
                  file=sys.stderr, flush=True)
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                time.sleep(min(wait, 120))
                delay = min(delay * 2, 60)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            print(f"  network error at offset={offset}: {e!r} (attempt {attempt + 1})", file=sys.stderr, flush=True)
            if attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise
    raise RuntimeError("unreachable")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--config", default="default")
    ap.add_argument("--split", required=True)
    ap.add_argument("--out", required=True, help="output JSONL path")
    ap.add_argument("--page-size", type=int, default=100, help="rows per request (API max 100)")
    ap.add_argument("--sample", type=int, default=0, help="keep a seeded uniform sample of N rows (0 = all)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--cache-dir", default="", help="optional dir to cache kept rows per page (makes reruns resume)")
    args = ap.parse_args()
    cache = Path(args.cache_dir) if args.cache_dir else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)

    first = get_page(args.dataset, args.config, args.split, 0, 1)
    total = int(first["num_rows_total"])
    features = first["features"]
    print(f"{args.dataset}/{args.config}/{args.split}: {total} rows; features={[f['name'] for f in features]}",
          file=sys.stderr)

    if args.sample and args.sample < total:
        rng = random.Random(args.seed)
        wanted = set(rng.sample(range(total), args.sample))
    else:
        wanted = set(range(total))
    ps = args.page_size
    pages = sorted({i // ps for i in wanted})
    print(f"keeping {len(wanted)} rows from {len(pages)} pages", file=sys.stderr)

    def fetch(page: int) -> list[dict]:
        cpath = cache / f"page_{ps}_{page:07d}.json" if cache else None
        if cpath and cpath.exists():
            return json.loads(cpath.read_text(encoding="utf-8"))
        rows = fetch_uncached(page)
        if cpath:
            tmp = cpath.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            tmp.replace(cpath)
        return rows

    def fetch_uncached(page: int) -> list[dict]:
        offset = page * ps
        length = min(ps, total - offset)
        data = get_page(args.dataset, args.config, args.split, offset, length)
        out = []
        for r in data["rows"]:
            idx = r["row_idx"]
            if idx not in wanted:
                continue
            if r.get("truncated_cells"):
                single = get_page(args.dataset, args.config, args.split, idx, 1)["rows"][0]
                if single.get("truncated_cells"):
                    raise RuntimeError(f"row {idx} still truncated: {single['truncated_cells']}")
                r = single
            out.append({"row_idx": idx, **r["row"]})
        return out

    rows: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for chunk in ex.map(fetch, pages):
            rows.extend(chunk)
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(pages)} pages", file=sys.stderr, flush=True)

    rows.sort(key=lambda r: r["row_idx"])
    if len(rows) != len(wanted):
        sys.exit(f"expected {len(wanted)} rows, got {len(rows)}")
    with open(args.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} rows to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
