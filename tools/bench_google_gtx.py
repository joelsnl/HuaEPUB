#!/usr/bin/env python3
"""
Benchmark the unofficial Google Translate endpoint HuaEPUB uses.

Theory to test: Google slowed / rate-limited translate.googleapis.com
(client=gtx). If an older HuaEPUB feels just as slow, this script should
show high latency, 429s, timeouts, or empty bodies — independent of the app.

Uses the same URL, params, User-Agent, GET/POST split, and timeouts as
core/translator.py. Does not download novels.

Examples:
  python tools/bench_google_gtx.py
  python tools/bench_google_gtx.py --n 80 --workers 50
  python tools/bench_google_gtx.py --n 40 --packed
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

ENDPOINT = "https://translate.googleapis.com/translate_a/single"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TIMEOUT = (5, 15)  # connect, read — same as HuaEPUB

# Typical web-novel paragraph (~40–70 Chinese chars).
SAMPLE = (
    "大长老看着远处的山门，神识微微一动，便察觉到有人正在接近。"
    "他沉声道：来者何人，报上名来。"
)

# ~4k chars, similar to HuaEPUB's packed Google request.
PACKED = (SAMPLE + "\n") * 55


def _prefer_ipv4() -> None:
    if sys.platform not in ("win32", "darwin"):
        return
    try:
        import urllib3.util.connection as conn

        conn.HAS_IPV6 = False
    except Exception:
        pass


def one_call(session: requests.Session, text: str) -> dict[str, Any]:
    params = {
        "client": "gtx",
        "sl": "zh-CN",
        "tl": "en",
        "dt": "t",
        "dj": "1",
        "q": text,
    }
    headers = {"User-Agent": USER_AGENT}
    t0 = time.perf_counter()
    status = 0
    err = ""
    translated = ""
    try:
        if len(text) <= 1800:
            resp = session.get(
                ENDPOINT, params=params, headers=headers, timeout=TIMEOUT
            )
        else:
            resp = session.post(
                ENDPOINT, data=params, headers=headers, timeout=TIMEOUT
            )
        status = resp.status_code
        resp.raise_for_status()
        data = resp.json()
        translated = "".join(
            s.get("trans", "")
            for s in data.get("sentences", [])
            if "trans" in s
        )
        if not translated.strip():
            err = "empty translation"
    except requests.Timeout:
        err = "timeout"
    except requests.HTTPError as exc:
        err = f"http {status or '?'} {exc}"
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    ms = (time.perf_counter() - t0) * 1000
    return {
        "ms": ms,
        "ok": bool(translated.strip()) and not err,
        "status": status,
        "err": err,
        "chars_out": len(translated),
    }


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    frac = k - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def summarize(label: str, rows: list[dict[str, Any]], n_segments: int) -> None:
    ok = [r for r in rows if r["ok"]]
    bad = [r for r in rows if not r["ok"]]
    times = [r["ms"] for r in rows]
    print()
    print(f"=== {label} ===")
    print(f"calls: {len(rows)}   ok: {len(ok)}   fail: {len(bad)}")
    if times:
        print(
            f"latency ms  min={min(times):.0f}  p50={percentile(times, 50):.0f}  "
            f"p95={percentile(times, 95):.0f}  max={max(times):.0f}  "
            f"mean={statistics.fmean(times):.0f}"
        )
    statuses: dict[int, int] = {}
    errors: dict[str, int] = {}
    for r in rows:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
        if r["err"]:
            key = r["err"].split(":")[0][:40]
            errors[key] = errors.get(key, 0) + 1
    print("status codes:", dict(sorted(statuses.items())))
    if errors:
        print("errors:", errors)
    if ok:
        avg_s = statistics.fmean(r["ms"] for r in ok) / 1000.0
        print(
            f"if every paragraph took p50={percentile([r['ms'] for r in ok], 50)/1000:.2f}s "
            f"and you had {n_segments} uncached paragraphs as 1-call-each: "
            f"~{n_segments * (percentile([r['ms'] for r in ok], 50) / 1000) / 60:.1f} min "
            f"sequential, or divide by your worker count (and then add retries)."
        )
        print(
            f"mean ok call {avg_s:.2f}s → 55k single-paragraph calls @ 200 workers "
            f"(perfect scaling, no 429s) ≈ {55000 * avg_s / 200 / 60:.1f} min. "
            f"Real Google runs are slower once they throttle."
        )
    print("sample errors:")
    for r in bad[:5]:
        print(f"  {r['ms']:.0f}ms  status={r['status']}  {r['err']}")


def run_serial(session: requests.Session, text: str, n: int) -> list[dict[str, Any]]:
    rows = []
    for i in range(n):
        row = one_call(session, text)
        rows.append(row)
        mark = "ok" if row["ok"] else "FAIL"
        print(f"  serial {i + 1}/{n}  {row['ms']:.0f}ms  {mark}  {row['err']}")
    return rows


def run_parallel(
    session_factory,
    text: str,
    n: int,
    workers: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def job(_i: int) -> dict[str, Any]:
        session = session_factory()
        try:
            return one_call(session, text)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(job, i) for i in range(n)]
        done = 0
        for fut in as_completed(futs):
            row = fut.result()
            rows.append(row)
            done += 1
            mark = "ok" if row["ok"] else "FAIL"
            print(f"  parallel {done}/{n}  {row['ms']:.0f}ms  {mark}  {row['err']}")
    return rows


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=30, help="calls per phase")
    parser.add_argument(
        "--workers",
        type=int,
        default=50,
        help="concurrent calls in the parallel phase (HuaEPUB default is 200)",
    )
    parser.add_argument(
        "--packed",
        action="store_true",
        help="send ~4k-char blobs (like the new packer) instead of one paragraph",
    )
    parser.add_argument(
        "--no-parallel",
        action="store_true",
        help="only run serial calls",
    )
    parser.add_argument(
        "--project",
        type=int,
        default=55000,
        help="uncached paragraph count used in the time estimate",
    )
    args = parser.parse_args()
    _prefer_ipv4()
    text = PACKED if args.packed else SAMPLE
    print("HuaEPUB Google gtx bench")
    print(f"  endpoint: {ENDPOINT}")
    print(f"  payload:  {len(text)} chars  packed={args.packed}")
    print(f"  timeout:  connect={TIMEOUT[0]}s read={TIMEOUT[1]}s")
    print()
    print("Phase 1 — serial (baseline latency, little rate-limit pressure)")
    session = make_session()
    try:
        serial = run_serial(session, text, args.n)
    finally:
        session.close()
    summarize("serial", serial, args.project)

    if not args.no_parallel:
        print()
        print(f"Phase 2 — {args.workers} workers (HuaEPUB-like pressure)")
        parallel = run_parallel(make_session, text, args.n, args.workers)
        summarize(f"parallel x{args.workers}", parallel, args.project)

        s_p50 = percentile([r["ms"] for r in serial if r["ok"]], 50)
        p_p50 = percentile([r["ms"] for r in parallel if r["ok"]], 50)
        s_fail = sum(1 for r in serial if not r["ok"])
        p_fail = sum(1 for r in parallel if not r["ok"])
        print()
        print("=== theory check ===")
        if p_fail > s_fail or (p_p50 and s_p50 and p_p50 > s_p50 * 1.5):
            print(
                "Parallel is slower or failier than serial. That matches "
                "Google throttling the free gtx endpoint, not an HuaEPUB bug."
            )
        elif serial and sum(1 for r in serial if r["ok"]) and s_p50 > 800:
            print(
                f"Even serial p50 is {s_p50:.0f}ms. The endpoint itself is "
                "slow now (used to be a few hundred ms when 1000-chapter "
                "runs finished in ~1.5h)."
            )
        else:
            print(
                "This short sample looks healthy. If a full novel is still "
                "hours, the throttle may only kick in after thousands of calls. "
                "Re-run with --n 200 --workers 200."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
