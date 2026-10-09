#!/usr/bin/env python3
"""Minimal stdlib-only Parquet reader for FLAT schemas -> JSONL (optionally a seeded row sample).

Supports: required/optional top-level primitive columns (no nesting/repetition); codecs UNCOMPRESSED,
SNAPPY (pure-Python decoder), GZIP, ZSTD (Python 3.14 compression.zstd); data pages v1 and v2;
encodings PLAIN, PLAIN_DICTIONARY / RLE_DICTIONARY, RLE (levels). Types: BOOLEAN, INT32, INT64,
FLOAT, DOUBLE, BYTE_ARRAY (UTF-8 strings). Anything else raises instead of guessing.

The input file is parsed as data only. Nothing in it is executed.

Usage:
  python3 -I scripts/parquet_flat_to_jsonl.py IN.parquet OUT.jsonl [--sample N --seed S]
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import struct
import sys

# ----------------------------------------------------------------------------- thrift compact protocol


class Thrift:
    def __init__(self, buf: bytes, pos: int = 0):
        self.b = buf
        self.p = pos

    def byte(self) -> int:
        v = self.b[self.p]
        self.p += 1
        return v

    def varint(self) -> int:
        shift = result = 0
        while True:
            x = self.byte()
            result |= (x & 0x7F) << shift
            if not x & 0x80:
                return result
            shift += 7

    def zigzag(self) -> int:
        n = self.varint()
        return (n >> 1) ^ -(n & 1)

    def binary(self) -> bytes:
        n = self.varint()
        v = self.b[self.p : self.p + n]
        if len(v) != n:
            raise ValueError("truncated binary")
        self.p += n
        return v

    def value(self, t: int):
        if t == 1:
            return True
        if t == 2:
            return False
        if t == 3:
            v = self.byte()
            return v - 256 if v > 127 else v
        if t in (4, 5, 6):
            return self.zigzag()
        if t == 7:
            v = struct.unpack_from("<d", self.b, self.p)[0]
            self.p += 8
            return v
        if t == 8:
            return self.binary()
        if t in (9, 10):
            h = self.byte()
            size, et = h >> 4, h & 0x0F
            if size == 15:
                size = self.varint()
            if et in (1, 2):  # bools inside lists are one byte each
                return [self.byte() == 1 for _ in range(size)]
            return [self.value(et) for _ in range(size)]
        if t == 11:
            size = self.varint()
            if size == 0:
                return {}
            kv = self.byte()
            return {self.value(kv >> 4): self.value(kv & 0x0F) for _ in range(size)}
        if t == 12:
            return self.struct()
        raise ValueError(f"unknown thrift type {t}")

    def struct(self) -> dict:
        out: dict[int, object] = {}
        fid = 0
        while True:
            h = self.byte()
            if h == 0:
                return out
            t, delta = h & 0x0F, h >> 4
            fid = fid + delta if delta else self.zigzag()
            out[fid] = self.value(t)


# ----------------------------------------------------------------------------- codecs


def snappy_decompress(src: bytes) -> bytes:
    t = Thrift(src)
    n = t.varint()
    p = t.p
    out = bytearray()
    ln = len(src)
    while p < ln:
        tag = src[p]
        p += 1
        kind = tag & 3
        if kind == 0:
            size = tag >> 2
            if size >= 60:
                nb = size - 59
                size = int.from_bytes(src[p : p + nb], "little")
                p += nb
            size += 1
            out += src[p : p + size]
            p += size
            continue
        if kind == 1:
            length = ((tag >> 2) & 7) + 4
            offset = ((tag >> 5) << 8) | src[p]
            p += 1
        elif kind == 2:
            length = (tag >> 2) + 1
            offset = int.from_bytes(src[p : p + 2], "little")
            p += 2
        else:
            length = (tag >> 2) + 1
            offset = int.from_bytes(src[p : p + 4], "little")
            p += 4
        if offset == 0 or offset > len(out):
            raise ValueError("bad snappy offset")
        start = len(out) - offset
        if offset >= length:
            out += out[start : start + length]
        else:
            for i in range(length):
                out.append(out[start + i])
    if len(out) != n:
        raise ValueError(f"snappy length mismatch {len(out)} != {n}")
    return bytes(out)


def decompress(codec: int, data: bytes, size: int) -> bytes:
    if codec == 0:
        return data
    if codec == 1:
        out = snappy_decompress(data)
    elif codec == 2:
        out = gzip.decompress(data)
    elif codec == 6:
        from compression import zstd  # Python >= 3.14

        out = zstd.decompress(data)
    else:
        raise ValueError(f"unsupported codec {codec}")
    if len(out) != size:
        raise ValueError("decompressed size mismatch")
    return out


# ----------------------------------------------------------------------------- encodings


def rle_hybrid(buf: bytes, pos: int, end: int, bw: int, count: int) -> list[int]:
    out: list[int] = []
    t = Thrift(buf, pos)
    nbytes = (bw + 7) // 8
    mask = (1 << bw) - 1
    while len(out) < count and t.p < end:
        h = t.varint()
        if h & 1:
            groups = h >> 1
            for _ in range(groups):  # each group = 8 values packed in bw bytes, LSB first
                chunk = int.from_bytes(buf[t.p : t.p + bw], "little")
                t.p += bw
                for i in range(8):
                    out.append((chunk >> (i * bw)) & mask if bw else 0)
        else:
            run = h >> 1
            v = int.from_bytes(buf[t.p : t.p + nbytes], "little") if nbytes else 0
            t.p += nbytes
            out.extend([v] * run)
    if len(out) < count:
        raise ValueError("RLE stream ended early")
    return out[:count]


def _f32_short(v: float) -> float:
    if v != v or v in (float("inf"), float("-inf")):
        return v
    for k in range(1, 10):
        c = float(f"{v:.{k}g}")
        if struct.unpack("<f", struct.pack("<f", c))[0] == v:
            return c
    return v


def plain(ptype: int, buf: bytes, pos: int, count: int) -> list:
    if ptype == 6:  # BYTE_ARRAY
        out = []
        for _ in range(count):
            (n,) = struct.unpack_from("<I", buf, pos)
            pos += 4
            out.append(buf[pos : pos + n].decode("utf-8"))
            pos += n
        return out
    if ptype == 4:  # FLOAT: emit shortest decimal that round-trips the float32 value
        return [_f32_short(v) for v in struct.unpack_from(f"<{count}f", buf, pos)]
    fmt = {1: "i", 2: "q", 5: "d"}.get(ptype)
    if fmt:
        return list(struct.unpack_from(f"<{count}{fmt}", buf, pos))
    if ptype == 0:  # BOOLEAN, bit-packed LSB first
        return [bool((buf[pos + i // 8] >> (i % 8)) & 1) for i in range(count)]
    raise ValueError(f"unsupported physical type {ptype}")


# ----------------------------------------------------------------------------- reader


def read_parquet(path: str) -> tuple[list[str], dict[str, list]]:
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"PAR1" or data[-4:] != b"PAR1":
        raise ValueError("not a parquet file")
    (mlen,) = struct.unpack_from("<I", data, len(data) - 8)
    fmd = Thrift(data, len(data) - 8 - mlen).struct()
    schema = fmd[2]
    root, leaves = schema[0], schema[1:]
    if root.get(5) != len(leaves) or any(5 in s for s in leaves):
        raise ValueError("nested schema not supported")
    names = [s[4].decode() for s in leaves]
    for s in leaves:
        if s.get(3) == 2:
            raise ValueError("repeated columns not supported")
    max_def = {s[4].decode(): (1 if s.get(3, 0) == 1 else 0) for s in leaves}
    ptypes = {s[4].decode(): s[1] for s in leaves}
    cols: dict[str, list] = {n: [] for n in names}
    num_rows = fmd[3]
    for rg in fmd[4]:
        rg_rows = rg[3]
        for cc in rg[1]:
            md = cc[3]
            name = b".".join(md[3]).decode()
            codec, ptype = md[4], md[1]
            start = md.get(11) or md[9]
            if md.get(11) is not None and md[9] < md[11]:
                start = md[9]
            end = start + md[7]
            pos = start
            dictionary = None
            got = 0
            while got < rg_rows and pos < end:
                th = Thrift(data, pos)
                ph = th.struct()
                pos = th.p
                ptype_page, usize, csize = ph[1], ph[2], ph[3]
                raw = data[pos : pos + csize]
                pos += csize
                if ptype_page == 2:  # dictionary page
                    dh = ph[7]
                    page = decompress(codec, raw, usize)
                    dictionary = plain(ptype, page, 0, dh[1])
                    continue
                if ptype_page == 0:  # data page v1
                    dh = ph[5]
                    nvals, enc = dh[1], dh[2]
                    page = decompress(codec, raw, usize)
                    p = 0
                    if max_def[name]:
                        (dl_len,) = struct.unpack_from("<I", page, 0)
                        defs = rle_hybrid(page, 4, 4 + dl_len, 1, nvals)
                        p = 4 + dl_len
                    else:
                        defs = [1] * nvals
                elif ptype_page == 3:  # data page v2
                    dh = ph[8]
                    nvals, enc = dh[1], dh[4]
                    dl_len, rl_len = dh[5], dh[6]
                    if rl_len:
                        raise ValueError("repetition levels not supported")
                    levels = raw[: dl_len + rl_len]
                    body = raw[dl_len + rl_len :]
                    if dh.get(7, True):
                        body = decompress(codec, body, usize - dl_len - rl_len)
                    defs = rle_hybrid(levels, rl_len, rl_len + dl_len, 1, nvals) if max_def[name] else [1] * nvals
                    page, p = body, 0
                else:
                    continue  # index pages etc.
                n_present = sum(1 for d in defs if d)
                if enc in (2, 8):
                    bw = page[p]
                    idx = rle_hybrid(page, p + 1, len(page), bw, n_present)
                    vals = [dictionary[i] for i in idx]
                elif enc == 0:
                    vals = plain(ptype, page, p, n_present)
                else:
                    raise ValueError(f"unsupported encoding {enc} in column {name}")
                it = iter(vals)
                cols[name].extend(next(it) if d else None for d in defs)
                got += nvals
            if got != rg_rows:
                raise ValueError(f"column {name}: got {got} values, expected {rg_rows}")
    for n in names:
        if len(cols[n]) != num_rows:
            raise ValueError(f"column {n} has {len(cols[n])} rows, expected {num_rows}")
    return names, cols


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inp")
    ap.add_argument("out")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    names, cols = read_parquet(args.inp)
    total = len(cols[names[0]])
    if args.sample and args.sample < total:
        idx = sorted(random.Random(args.seed).sample(range(total), args.sample))
    else:
        idx = range(total)
    with open(args.out, "w", encoding="utf-8") as f:
        for i in idx:
            f.write(json.dumps({"row_idx": i, **{n: cols[n][i] for n in names}}, ensure_ascii=False) + "\n")
    print(f"{args.inp}: {total} rows, columns={names}; wrote {len(idx)} rows to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
