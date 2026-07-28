#!/usr/bin/env python3
"""Refresh the UBC dataset.

    python update.py                  # update everything
    python update.py courses events   # update just these groups
    python update.py --list           # show what is available
    python update.py --skip people    # everything except one group

Each collector writes into data/<group>/ and the run rebuilds data/manifest.json.
A group that fails is reported and recorded, but does not stop the rest.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback

from collectors import CAMPUS_CHOICES, DATA_DIR, REGISTRY, Http, Output, set_campus, utcnow

MANIFEST = DATA_DIR / "manifest.json"


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def load_manifest() -> dict:
    if not MANIFEST.exists():
        return {}
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="update.py",
        description="Gather public UBC institutional data into ./data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(
            f"  {name:<12} {cls.title}" for name, cls in REGISTRY.items()
        ),
    )
    parser.add_argument("groups", nargs="*", help="groups to update (default: all)")
    parser.add_argument("--list", action="store_true", help="list groups and exit")
    parser.add_argument("--skip", action="append", default=[], metavar="GROUP",
                        help="skip a group (repeatable)")
    parser.add_argument("--campus", choices=CAMPUS_CHOICES, default="vancouver",
                        help="which campus to collect (default: vancouver)")
    parser.add_argument("--workers", type=int, default=8,
                        help="parallel requests per collector (default: 8)")
    parser.add_argument("--timeout", type=int, default=60, help="per-request timeout in seconds")
    parser.add_argument("--min-interval", type=float, default=0.0, metavar="SECONDS",
                        help="minimum gap between requests, to go easy on the servers")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    if args.list:
        print("Available groups:\n")
        for name, cls in REGISTRY.items():
            print(f"  {name:<12} {cls.title}")
            print(f"  {'':<12} {cls.description}\n")
        return 0

    unknown = [name for name in args.groups + args.skip if name not in REGISTRY]
    if unknown:
        print(f"unknown group(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"available: {', '.join(REGISTRY)}", file=sys.stderr)
        return 2

    selected = args.groups or list(REGISTRY)
    selected = [name for name in selected if name not in args.skip]

    set_campus(args.campus)

    http = Http(timeout=args.timeout, min_interval=args.min_interval, workers=args.workers)
    manifest = load_manifest()
    groups = manifest.get("groups", {})

    print(f"Updating {len(selected)} group(s) into {DATA_DIR}  [campus: {args.campus}]\n")
    failures = 0

    for position, name in enumerate(selected, start=1):
        collector = REGISTRY[name]()
        out = Output(collector.folder)
        started = time.monotonic()
        print(f"[{position}/{len(selected)}] {name} -- {collector.title}", flush=True)

        try:
            collector.collect(http, out)
        except Exception as error:
            failures += 1
            elapsed = time.monotonic() - started
            print(f"    FAILED after {elapsed:.1f}s: {type(error).__name__}: {error}\n",
                  file=sys.stderr)
            traceback.print_exc(limit=3, file=sys.stderr)
            groups[name] = {
                "title": collector.title,
                "status": "failed",
                "error": f"{type(error).__name__}: {error}",
                "updated_at": utcnow(),
                **({"datasets": groups.get(name, {}).get("datasets", [])}),
            }
            continue

        elapsed = time.monotonic() - started
        records = sum(d.records or 0 for d in out.datasets)
        size = sum(d.bytes for d in out.datasets)
        # Safe only because we got here without an exception: a partial run must
        # never delete the previous good data.
        removed = out.prune()
        print(f"    {len(out.datasets)} file(s), {records:,} records, "
              f"{human_bytes(size)} in {elapsed:.1f}s", flush=True)
        if removed:
            print(f"    removed {len(removed)} stale file(s): "
                  f"{', '.join(r.split('/')[-1] for r in removed[:6])}"
                  f"{' ...' if len(removed) > 6 else ''}", flush=True)
        print(flush=True)

        groups[name] = {
            "title": collector.title,
            "description": collector.description,
            "sources": list(collector.sources),
            "folder": collector.folder,
            "status": "ok",
            "updated_at": utcnow(),
            "duration_seconds": round(elapsed, 1),
            "records": records,
            "bytes": size,
            "removed_stale": removed,
            "datasets": [vars(dataset) for dataset in out.datasets],
        }

    manifest = {
        "name": "UBC open data",
        "description": "Public institutional data about UBC, gathered from official sources.",
        "generated_at": utcnow(),
        "campus": args.campus,
        "groups": groups,
    }
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    total_records = sum(g.get("records", 0) for g in groups.values())
    total_bytes = sum(g.get("bytes", 0) for g in groups.values())
    print(f"Dataset totals: {total_records:,} records, {human_bytes(total_bytes)}")
    print(f"Manifest: {MANIFEST}")

    if failures:
        print(f"\n{failures} group(s) failed.", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
