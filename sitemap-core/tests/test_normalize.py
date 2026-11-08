"""Unit tests for the cross-domain policy in ``normalize.is_same_site``.

This file exists because the fixture corpus cannot test this function. Every
corpus fixture is served from ``127.0.0.1``, so ``seed_host`` is always
``127.0.0.1`` and every ``<url>`` in a fixture is foreign to it. A corpus that
passes 28/28 can therefore still ship a policy that marks the target's own
sitemaps as ``cross_domain`` — which is exactly what happened: a run against
``https://www.apify.com`` returned 30/30 rows flagged cross-domain, because the
sitemap lists the apex while the seed was ``www``.

End-to-end coverage is not a substitute for testing the decision itself. These
are the cases the corpus structurally cannot reach.

Run standalone for a pass/fail line per case::

    python tests/test_normalize.py

or under pytest.
"""

from __future__ import annotations

import pathlib
import sys

_TESTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS_DIR))
sys.path.insert(0, str(_TESTS_DIR.parents[0]))

from sitemap_core.normalize import is_same_site  # noqa: E402

# (url, seed_host, include_subdomains, expected, why it matters)
CASES = [
    # --- the case that shipped as a bug ---------------------------------
    (
        "https://apify.com/pricing",
        "www.apify.com",
        False,
        True,
        "apex in the sitemap, www in the seed: the site's own rows",
    ),
    (
        "https://www.apify.com/pricing",
        "apify.com",
        False,
        True,
        "the same pair the other way round",
    ),
    (
        "https://example.com/page",
        "www.example.com",
        True,
        True,
        "subdomains on must not change the www/apex answer",
    ),
    # --- the flag still governs real subdomains -------------------------
    (
        "https://shop.example.com/page",
        "example.com",
        False,
        False,
        "a real subdomain is foreign when the flag is off",
    ),
    (
        "https://shop.example.com/page",
        "example.com",
        True,
        True,
        "and part of the site when it is on",
    ),
    (
        "https://blog.example.com/page",
        "www.example.com",
        False,
        False,
        "www is not the apex here, so the flag decides",
    ),
    (
        "https://sub.www.example.com/page",
        "example.com",
        True,
        True,
        "a nested label under the registered domain",
    ),
    # --- genuinely foreign hosts ---------------------------------------
    (
        "https://cdn.partner-network.example/asset",
        "main.example",
        True,
        False,
        "a partner host is foreign whatever the flag says",
    ),
    (
        "https://tracking.third-party-example.net/x",
        "example.com",
        True,
        False,
        "a third-party host is foreign whatever the flag says",
    ),
    (
        "https://example.net/page",
        "example.com",
        True,
        False,
        "different registrable domain",
    ),
    # --- multi-label public suffixes -----------------------------------
    (
        "https://www.example.co.uk/page",
        "example.co.uk",
        False,
        True,
        "www/apex under a two-label suffix",
    ),
    (
        "https://other.co.uk/page",
        "example.co.uk",
        True,
        False,
        "co.uk is a suffix, not a shared site — string matching gets this wrong",
    ),
    (
        "https://example.co.uk/page",
        "example.com",
        False,
        False,
        "same domain label, different suffix",
    ),
    # --- identical hosts -----------------------------------------------
    (
        "https://example.com/page",
        "example.com",
        False,
        True,
        "an exact host match is always the same site",
    ),
    # --- loopback, which is what the corpus uses -----------------------
    (
        "https://main.example/local-page",
        "127.0.0.1",
        True,
        False,
        "the corpus seed is loopback; every fixture row is foreign to it",
    ),
    (
        "https://127.0.0.1/page",
        "127.0.0.1",
        False,
        True,
        "loopback matches itself",
    ),
    # --- degenerate input ----------------------------------------------
    (
        "not-a-url",
        "example.com",
        False,
        False,
        "no host in the URL",
    ),
    (
        "https://example.com/page",
        "",
        False,
        False,
        "no seed host",
    ),
]


def test_is_same_site() -> None:
    for url, seed_host, include_subdomains, expected, why in CASES:
        got = is_same_site(url, seed_host, include_subdomains=include_subdomains)
        assert got is expected, (
            f"is_same_site({url!r}, {seed_host!r}, include_subdomains={include_subdomains}) "
            f"-> {got}, expected {expected}: {why}"
        )


def main() -> int:
    failures = 0
    for url, seed_host, include_subdomains, expected, why in CASES:
        try:
            got = is_same_site(url, seed_host, include_subdomains=include_subdomains)
            assert got is expected
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {exc}", flush=True)
        else:
            print(
                f"ok    {url:42s} seed={seed_host:18s} "
                f"subs={str(include_subdomains):5s} -> {got}",
                flush=True,
            )
    total = len(CASES)
    print(f"\n{total - failures}/{total} passed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
