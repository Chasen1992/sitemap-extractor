# Sitemap Extractor

**Read the sitemap files a website has already published, and tell you what is in them.**

A sitemap parser — not a web crawler with a sitemap-shaped front door. Point it at a domain or an XML sitemap URL, and it finds the site's sitemap, follows any `<sitemapindex>` files, and returns every URL the site publishes as structured rows. Tick one box and it probes each URL's HTTP status code too.

- **Runs on Apify** → [apify.com/Chasen/sitemap-extractor](https://apify.com/Chasen/sitemap-extractor)
- **Something wrong** → [open an issue](https://github.com/Chasen1992/sitemap-extractor/issues/new). Include the run's `SUMMARY` record if you have one — that record is designed to make bug reports self-diagnosing.

## What is in here

| | |
|---|---|
| [`sitemap-extractor/`](./sitemap-extractor) | the Actor: Docker build, input and output schemas, README, changelog |
| [`sitemap-core/`](./sitemap-core) | the shared parser package and its adversarial test corpus |
| [`EVIDENCE.md`](./EVIDENCE.md) | measured results — and the defects the tests did **not** catch |

One parser, one test corpus, one output schema, shared with the change monitor being built on top of it. Deliberate: the monitoring product consumes a parser already exercised in production rather than a rewritten copy.

## Running the tests

```
cd sitemap-core
python tests/test_corpus.py          # 28 hostile sitemap fixtures
python tests/test_normalize.py       # the cross-domain policy
python tests/test_discovery.py       # the discovery path no fixture reaches
python tests/test_summary.py         # the run summary's account of itself
python tests/test_actor_contract.py  # the store listing vs the code
```

The fixtures are checked in, so this runs as-is. `tests/make_fixtures.py` regenerates all 28 on demand, including a 50 000-URL file and several deliberately broken `.gz` payloads.

Current state: **28/28 fixtures**, plus **18/18** normalize, **9/9** discovery, **12/12** actor contract and **5/5** summary on the direct suites, no run killed. `EVIDENCE.md` records what that number does and does not prove — including the six defects that were live while the corpus reported 28/28, and the two test guards that passed on the exact defect each was written to catch.

## What it does not do

It does not render JavaScript. It does not follow links inside pages. It does not rotate proxies, spoof a browser, or attempt to get past a block — if a target answers `403` or serves a bot-challenge page, the run records it and moves on. It does not read page content, touch logins or paywalls, or collect personal data. It reads sitemap files that are already public, and checks the status of URLs the site itself published.

## Licence

**GPL-3.0-or-later.** See [`LICENSE`](./LICENSE) and [`sitemap-extractor/NOTICE`](./sitemap-extractor/NOTICE) for third-party attribution.

This is a derivative work of [`ultimate-sitemap-parser`](https://github.com/GateNLP/ultimate-sitemap-parser) (GPL-3.0), used unmodified for sitemap discovery, tolerant parsing, index recursion and cycle protection. Because of that, any distribution of this code must remain GPL-3.0-or-later. The complete corresponding source is published here and with the Actor.
