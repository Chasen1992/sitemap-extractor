# Changelog

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
