# Sitemap Extractor

**A sitemap parser that returns a clean URL list — not a web crawler wearing a sitemap costume.**

Give it a domain or an XML sitemap URL and it finds the site's sitemap, follows any `<sitemapindex>` files, and returns every URL the site publishes as structured rows. Tick one box and it probes each URL's HTTP **status codes** too.

## Introduction

Most tools called "sitemap extractor" are general-purpose crawlers with a sitemap-shaped front door. They hand you thirty knobs, ask you to write JavaScript, require a proxy, and fail about one run in five.

This one does exactly one job: **read the sitemap files a website has already published, and tell you what is in them.**

- **Sitemap discovery.** Point at `example.com` and it reads `robots.txt`, then tries the usual paths — `/sitemap.xml`, `/sitemap_index.xml`, `/wp-sitemap.xml`, and the rest — until it finds one.
- **Sitemap index support.** `<sitemapindex>` files are followed recursively. WordPress publishes an index of indexes; depth 3 covers it.
- **Every sitemap flavour.** XML urlsets, sitemap indexes, Google News and image extensions, plain-text sitemap lists, RSS and Atom. Missing XML namespaces and arbitrary tag prefixes are handled, because old plugins produce both. Malformed XML is salvaged rather than thrown away: a sitemap with one bare `&` in it still yields the other 399 URLs.
- **Status codes, done properly.** `HEAD` first, falling back to `GET` only where a server actually refuses `HEAD` (405/501). Page bodies are never read or stored — this is a status check, not a content scraper.
- **`<lastmod>` kept verbatim.** Real sitemaps contain `2023`, `2023-04`, `+0800` without a colon, RFC822, and dates that do not exist. The raw value is always preserved, with a best-effort ISO parse alongside it. A date that will not parse never drops the row.
- **Typed failures.** Every non-success row carries an `errorType` from a fixed enum (`http_error`, `timeout`, `dns`, `blocked`, `html_challenge`, `truncated`, `skipped_budget`, …) so you can filter and count them. Free-text errors cannot be filtered, which is why tools that only ship free-text errors are impossible to monitor.

Nine inputs. That is deliberate: the competitor's thirty knobs are mostly ways to configure a crawler you did not want.

**What it does not do.** It does not render JavaScript. It does not follow links inside pages. It does not rotate proxies, spoof a browser, or attempt to get past a block — if a target answers `403` or serves a bot-challenge page, the run records it and moves on. It does not read page content, touch logins or paywalls, or collect personal data. It only reads sitemap files that are already public and checks the status of URLs the site itself published.

## Tutorial

### 1. The simplest run

Set **Start URLs** to a domain and press **Start**:

```
startUrls: [{ "url": "https://www.apify.com" }]
```

Output is one row per URL found in the site's sitemap.

### 2. You already have a sitemap URL

Paste it directly. It is used as the seed instead of being discovered:

```
startUrls: [{ "url": "https://www.apify.com/sitemap.xml" }]
```

### 3. Turn on status codes

**Check status codes** is on by default. Every row then gains `statusCode` and `contentType`. Turn it off to get the URL list alone — the run becomes much faster and much cheaper, because the status probe is where nearly all of the requests go.

### 4. Bound the work, not just the output

Two separate limits, and the difference matters:

| Input | Bounds | Default |
|---|---|---|
| **Max URLs** (`maxUrls`) | how many rows come back | 1 000 |
| **Max total requests** (`maxRequestsTotal`) | how much work the run may do | 20 000 |

A single sitemap index can legally reference 50 000 child sitemaps, and the status check then roughly doubles the request count. `maxUrls` alone would not stop that. When the request budget runs out, the run ends cleanly: `truncated: true` in the summary, the untouched URLs marked `skipped_budget`. It never fails silently and never pretends to be complete.

**Max total charge (USD)** is a third guard, this one on your bill: the run stops pushing rows before it charges more than you asked.

### 5. Reading the output

Each row in the dataset is one URL:

```json
{
  "url": "https://example.com/products/42",
  "normalizedUrl": "https://example.com/products/42",
  "sourceSitemap": "https://example.com/sitemap_products_1.xml",
  "depth": 1,
  "checked": true,
  "statusCode": 200,
  "contentType": "text/html",
  "lastmodRaw": "2024-11-02T08:00:00+0000",
  "lastmodIso": "2024-11-02T08:00:00+00:00",
  "errorType": null,
  "errorMessage": null
}
```

**Some rows are listed but never checked.** A sitemap often publishes URLs on a CDN or a partner host. Those are returned with `crossDomain: true` and `errorType: "cross_domain"`, and are never requested. Fetching a third party the site merely referenced is not what you asked for. `www.example.com` and `example.com` count as one site and are never marked this way; real subdomains are governed by **Include subdomains**.

The **Problems** view in the Output tab shows only rows that did not come back clean. Use it first; it is usually the fastest way to find out whether a run is worth reading.

Three records also land in the key-value store:

- `SUMMARY` — the run report: sitemaps attempted and succeeded, URLs discovered and deduped, requests and bytes spent against budget, `truncated`, `truncateReason`, and a breakdown of every `errorType`. Written even when the run is cut short or fails.
- `SITEMAPS` — one record per sitemap file attempted, including the ones that failed and why.
- `NEXT_STEP` — see Support below.

### 6. Timings, stated honestly

Requests to any single host are paced at about 4 per second with at most 4 in flight, and `robots.txt` `Crawl-delay` is honoured if the site sets one. That cap is a compliance choice, and it is what determines the runtime — not CPU.

Two things add up: **reading the sitemaps**, which depends entirely on how many the site publishes, and **checking status**, which costs about 0.3 s per URL against a single host.

Measured on one site publishing 24 sitemap files with 3.69 million URLs listed in them, status checking **on**, on the code in this repository:

| rows returned | wall clock |
| --- | --- |
| 200 | 3 min |
| 500 | 4 min |
| 1 000 | 7 min |
| 10 000 | did not finish in 15 min — don't |

Reading those 24 sitemaps was about 2 minutes of every one of those runs, paid whether you ask for 200 rows or 10 000. A site publishing two small sitemap files is far quicker: 500 rows with status codes took 2½ minutes.

Three things worth knowing before you raise the number:

- **The run is paced, not parallelised.** 50 000 URLs on one host is a multi-hour job. That is the target's willingness to be asked, not this Actor being slow.
- **Some sites rate-limit, and that is reported honestly.** One target returned 429s; the tool backed off and marked those rows `rate_limited` rather than calling them dead links. Correct behaviour, and it takes longer.
- **`SUMMARY.truncated` is `true` whenever `maxUrls` is why you did not get every URL**, and `truncateReason` says so. The default is 1 000 rows and the example run in the console does 200. Raise `maxUrls` when you want more — but check `truncated` before concluding a site is small.

### 7. Running it from the API

```bash
curl -X POST \
  "https://api.apify.com/v2/acts/YOUR_USERNAME~sitemap-extractor/runs?token=YOUR_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "startUrls": [{ "url": "https://example.com" }],
    "checkStatusCodes": true,
    "maxUrls": 1000
  }'
```

Omit `maxUrls` and you get the same 1 000. Pass a bigger number when you want more rows — see the timings above first.

## Pricing

**This Actor is currently free.** A price will be added after it has been in the Store long enough for people to have tried it — and the price will be published in this section before it takes effect, not sprung on anyone.

The intended model is **pay per event**, charged one item per row returned (`apify-default-dataset-item`), at the low end of what sitemap extractors charge. When that lands:

- **You are charged for rows returned, not for time or compute.** A run that finds 200 URLs costs the same as any other 200-URL run.
- **A run that finds nothing costs nothing.** There is no minimum charge and no per-run fee.
- **`maxTotalChargeUsd` bounds the bill.** Set it and the run stops before exceeding it. The dataset is cut, not lost — `SUMMARY` reports `rowsDroppedForChargeLimit` so you can see exactly what was cut and why.
- **Budget exhaustion never overcharges.** When the request budget runs out, the run stops cleanly and reports it. You are not charged for work that did not happen.

Turning **Check status codes** off is the single biggest cost lever, because status probing accounts for the overwhelming majority of requests.

## Support

**Open an issue** at [github.com/apify-actors/sitemap-extractor/issues](https://github.com/apify-actors/sitemap-extractor/issues) — include the run URL and the `SUMMARY` record if you have one. That record is designed to make bug reports self-diagnosing.

**Want these URLs watched for changes?** The thing people most often want next is not another extraction but a *monitor*: run this same sitemap parser on a schedule and tell them what appeared, disappeared, or changed status. That is being built. If it is something you would use, [say so](https://github.com/apify-actors/sitemap-extractor/issues/new) — the requests decide what gets built first. Every run writes an offer for this to the `NEXT_STEP` key-value record, which is how that decision gets made on evidence rather than guesswork.

### Known limits, stated rather than hidden

- A sitemap served in a legacy 8-bit charset that declares no charset at all is read as UTF-8 and its non-ASCII paths may show replacement characters. Declared charsets (XML prolog or `Content-Type`) are honoured, including ISO-8859-1.
- Hostnames in non-ASCII scripts are reported as the sitemap wrote them; the rows are not rewritten to punycode.
- A bot-challenge served as a plain `200` to a `HEAD` request is reported as `200`, because detecting it needs body bytes and `HEAD` has none. We do not spend a second request on every URL to guard an uncommon case.

### Licence and attribution

This Actor is **free and open source software, licensed under GPL-3.0-or-later**. See [`LICENSE`](./LICENSE) for the terms and [`NOTICE`](./NOTICE) for third-party attribution.

It is a derivative work of [`ultimate-sitemap-parser`](https://github.com/GateNLP/ultimate-sitemap-parser) (GPL-3.0), which is used unmodified and provides sitemap discovery, tolerant parsing, index recursion and cycle protection. Because of that, any distribution of this Actor must remain GPL-3.0-or-later. The complete corresponding source is published with the Actor.

### Not affiliated

This is an independent product. It is not affiliated with, endorsed by, or connected to any other sitemap extractor, sitemap parser, or scraping tool on Apify Store or elsewhere.
