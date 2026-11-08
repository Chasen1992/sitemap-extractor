"""Fast spot-check of key fixtures. Not the corpus: just enough to see shape."""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from make_fixtures import FIXTURES_DIR, write_fixtures  # noqa: E402
from test_corpus import FixtureServer, run_fixture  # noqa: E402


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    t0 = time.time()
    write_fixtures("http://127.0.0.1:8765")
    print(f"fixtures written in {time.time() - t0:.2f}s", flush=True)

    import tldextract

    t0 = time.time()
    ext = tldextract.TLDExtract(suffix_list_urls=())
    print(f"tldextract: {ext('a.example.co.uk')} in {time.time() - t0:.3f}s", flush=True)

    cases = [
        ("plain.xml", False),
        ("html_challenge.xml", False),
        ("bad_dates.xml", False),
        ("gzipped.xml.gz", False),
        ("double_gzip.xml.gz", False),
        ("fake_gz.xml.gz", False),
        ("billion_laughs.xml", False),
        ("dup_entries.xml", False),
        ("idna.xml", False),
        ("status_probe.xml", True),
    ]

    with FixtureServer(FIXTURES_DIR) as server:
        base = f"http://127.0.0.1:{server.port}"
        for name, check_status in cases:
            t0 = time.time()
            result = run_fixture(name, base, check_status=check_status)
            print(
                f"{name:22s} rows={result.rows:6d} {result.elapsed:5.2f}s "
                f"killed={result.killed_run} :: {result.detail[:120]}",
                flush=True,
            )
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
