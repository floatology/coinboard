#!/usr/bin/env python3
"""Keep big archive files in git as small block-range parts.

    parquet_parts.py split data/tokens/<token>   # whole files -> parts/<name>/<name>.<bucket>.parquet
    parquet_parts.py join  data/tokens/<token>   # parts -> whole files (what every script reads)

GitHub warns at 50 MB and refuses 100 MB, and the WALLET archive grows daily. Only
the parts are committed (the whole files are git-ignored). Parts are cut by
block // SPAN, so a finished range never changes and is not rewritten; only the
newest part changes each update. Scripts keep reading the whole file as before.
Run `join` after a fresh clone, then `track_tokens.py reprice --only WALLET` to rebuild the ledger, and `split` before committing.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

SPAN = 10_000_000
NAMES = ["swaps", "swaps_attributed", "transfers"]   # not the ledger: its USD columns reprice with ETH, so it is rebuilt (track_tokens.py reprice)


def split(d: Path) -> None:
    for n in NAMES:
        f = d / f"{n}.parquet"
        if not f.exists():
            continue
        t = pq.read_table(f)
        bucket = pc.divide(t["block"], pa.scalar(SPAN, pa.int64()))
        out = d / "parts" / n
        out.mkdir(parents=True, exist_ok=True)
        wrote = kept = 0
        for b in sorted(set(bucket.to_pylist())):
            part = t.filter(pc.equal(bucket, b))
            p = out / f"{n}.{b:03d}.parquet"
            if p.exists() and pq.read_table(p).equals(part):
                kept += 1
                continue
            pq.write_table(part, p, compression="zstd")
            wrote += 1
        print(f"{d.name}/{n}: {t.num_rows:,} rows, {wrote} parts written, {kept} unchanged")


def join(d: Path) -> None:
    for n in NAMES:
        ps = sorted((d / "parts" / n).glob(f"{n}.*.parquet"))
        if not ps:
            continue
        t = pa.concat_tables([pq.read_table(p) for p in ps]).sort_by([("block", "ascending"), ("log_index", "ascending")])
        pq.write_table(t, d / f"{n}.parquet", compression="zstd")
        print(f"{d.name}/{n}: {t.num_rows:,} rows from {len(ps)} parts")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["split", "join"])
    ap.add_argument("dir", type=Path)
    a = ap.parse_args()
    (split if a.cmd == "split" else join)(a.dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
