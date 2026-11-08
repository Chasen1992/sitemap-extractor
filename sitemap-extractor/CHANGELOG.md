# Changelog

## 1.0.8 — 2026-11-08

Two error messages claimed more than the tool had observed, and both were
found by one run against a site whose every missing path 302s to its own
error page.

- A `robots.txt` fetch that came back HTML was reported as "HTML document
  returned where **a sitemap** was expected". No sitemap was expected there —
  the request was for `robots.txt`, which this tool made. The message now
  names what was actually being looked for.
- A site with no sitemap was reported as "robots.txt lists none and no
  well-known sitemap path responded". On a soft-404 site neither clause is
  true: `robots.txt` is never read, and the paths do respond, with HTML. The
  message is now built from what was fetched and what came back —
  `robots.txt could not be read (…)`, `robots.txt named no sitemap`, or
  `robots.txt was not consulted` — plus how many candidate sitemap URLs were
  tried and the most common outcome among them.

No behaviour changed: a run that found no sitemap still reports `no_sitemap`
and still exits 0. Only what the log and `SUMMARY` say about it changed, so
that a reader can tell what was checked from what was assumed.

## 1.0.7 — 2026-11-08

**Every row was being counted as delivered and then rejected on arrival, so
the dataset was empty on every run while the summary claimed success.**

`Actor.push_data` validates each row against the dataset schema, and a
declared `type` — `integer`, `string`, `number` — does not admit `null`. Rows
carried `null` for fields that did not apply (`statusCode` on a URL that was
never probed, `lastmodIso` on an entry with no `<lastmod>`, and so on), so
`Schema validation failed` for every batch and nothing landed. Fields that do
not apply are now **absent** rather than `null`, which the schema's `required`
list allows.

## 1.0 — 2026-11-08

First release.

- Discovers sitemaps from a domain (robots.txt, `/sitemap.xml`, `/sitemap_index.xml`, `/wp-sitemap.xml` and the other usual paths) or takes a sitemap URL directly as the seed.
- Follows `<sitemapindex>` recursively, with a depth limit and cycle protection.
- Parses XML sitemaps, Google News and image extensions, plain-text sitemap lists, and RSS/Atom feeds. Malformed XML is salvaged rather than discarded.
- Optional per-URL status codes via `HEAD`, falling back to `GET` only where a server refuses `HEAD`.
- Work budgets: total requests, decompressed bytes, per-file size, gzip ratio. Budget exhaustion ends the run with `truncated: true` rather than failing or silently cutting it short.
- **`truncated` means the rows you have are not the whole answer.** A spent budget *and* reaching `maxUrls` both set it, and `truncateReason` says which. A run that stopped short is never reported as complete.
- Per-host rate limiting, `robots.txt` `Crawl-delay`, `429`/`Retry-After` honoured.
- SSRF refusal after DNS resolution: private, link-local and cloud-metadata address ranges are never requested.
- Fixed output schema with a typed `errorType` enum.

Nine inputs. The plan is to keep it that way.
