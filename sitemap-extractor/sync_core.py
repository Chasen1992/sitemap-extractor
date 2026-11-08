"""Refresh the vendored copy of ``sitemap_core`` inside this Actor.

Apify builds each Actor from its own directory, so the shared package is copied
in rather than linked. ``../sitemap-core/`` stays the single source of truth —
**never edit ``src/sitemap_core`` by hand**, run this instead.

    python sync_core.py           # copy the package in and stamp it
    python sync_core.py --check   # exit 1 if the copy has drifted from source

The check exists because a silently stale vendored copy is the one way the
"same parser, same tests, same output schema" promise can quietly stop being
true.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
SOURCE = HERE.parent / "sitemap-core" / "sitemap_core"
DEST = HERE / "src" / "sitemap_core"
MANIFEST = DEST / ".sync_manifest.json"
SKIP = {"__pycache__", ".sync_manifest.json", "py.typed"}


def _hashes(root: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.split("/", 1)[0] in SKIP or rel in SKIP:
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify only, do not copy")
    args = ap.parse_args()

    if not SOURCE.is_dir():
        print(f"source package not found: {SOURCE}", file=sys.stderr)
        return 2

    source_hashes = _hashes(SOURCE)

    if args.check:
        if not DEST.is_dir():
            print(f"vendored copy missing: {DEST}", file=sys.stderr)
            return 1
        try:
            stamped = json.loads(MANIFEST.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print("vendored copy has no manifest; run `python sync_core.py`", file=sys.stderr)
            return 1
        if stamped != source_hashes:
            changed = sorted(
                set(stamped) ^ set(source_hashes)
                | {k for k in source_hashes if stamped.get(k) != source_hashes[k]}
            )
            print("vendored copy is stale: " + ", ".join(changed[:10]), file=sys.stderr)
            return 1
        print(f"vendored copy is in sync ({len(source_hashes)} files)")
        return 0

    if DEST.exists():
        shutil.rmtree(DEST)
    shutil.copytree(SOURCE, DEST)
    for junk in DEST.rglob("__pycache__"):
        shutil.rmtree(junk, ignore_errors=True)
    MANIFEST.write_text(json.dumps(source_hashes, indent=2, sort_keys=True), encoding="utf-8")
    (DEST / "py.typed").write_text("", encoding="utf-8")
    print(f"synced {len(source_hashes)} files -> {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
