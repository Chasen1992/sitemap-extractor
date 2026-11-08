# sitemap-core

Guarded sitemap discovery, parsing and URL status probing. One shared package
behind the `sitemap-extractor` Apify Actor and the change-monitor product, so
the two ship the same parser, the same tests and the same output schema.

Built on [`ultimate-sitemap-parser`](https://github.com/GateNLP/ultimate-sitemap-parser),
which already covers robots.txt `Sitemap:` directives, well-known paths, every
sitemap flavour (XML / text / RSS / Atom / Google News / image), malformed-XML
salvage, index recursion and cycle protection. This package does **not** fork
it. Everything policy-related lives in our own layers around it.

## What is here

| module | responsibility |
| --- | --- |
| `normalize.py` | canonical URL form, dedup key, cross-domain policy |
| `ssrf.py` | DNS resolution + refusal of private / link-local / cloud-metadata ranges |
| `budget.py` | request, decompressed-byte, per-file and gzip-ratio budgets |
| `ratelimit.py` | global + per-host concurrency, per-host token bucket, `Crawl-delay` |
| `sniff.py` | content classification, guarded gunzip, gzip-bomb refusal |
| `transport.py` | the guarded `AbstractWebClient` the parser talks to |
| `discover.py` | sitemap discovery and depth / cycle / budget refusals |
| `parse.py` | parsed tree → output rows, verbatim `<lastmod>`, tolerant dates |
| `statuscheck.py` | async URL status probing, HEAD with GET fallback |
| `pipeline.py` | the end-to-end run and the failure taxonomy |
| `models.py` | the shared output schema |

## Policy, written down before the code

**Budgets govern work, not output rows.** `maxUrls` caps emitted rows and
nothing else. A sitemap index may legally reference 50,000 children that each
have to be fetched before the first `<url>` appears, so there are separate
budgets for total requests, total decompressed bytes, per-file size, and gzip
compression ratio. XML entity expansion is disabled.

**No single bad sample may kill a run.** Every sitemap file and every URL is its
own work unit with its own error boundary. Failures become rows with an
`errorType` from a closed enum. The exit codes are part of the contract, and
`pipeline.run()` is the only entry point that should be called from a host
program, because it is what turns an input error into one:

| exit | meaning |
| --- | --- |
| `0` | ran to completion, or stopped on budget with `truncated: true` |
| `2` | invalid input — fast-fail, clear message, never a data row |
| `3` | something unforeseen happened; whatever had been collected was flushed and the summary written anyway |

**Output is flushed in batches of 2,000 rows**, not one call per row and not one
call at the end. Per-row would be 50,000 network round trips; a single call
loses the whole run if it is killed at minute 29 of 30. Each batch has already
been status-probed when it is pushed, so what lands in the dataset is complete.

**Bodies the streaming parser stopped reading are recovered.** Expat stops at
the first invalid token, so one unescaped `&` in the third entry of a sitemap
used to throw away every entry after it. `parse.salvage_locs()` pulls the
`<loc>` values out of the rest of the body behind the parser's back, subject to
two rules: it is refused outright for any body carrying a DOCTYPE or ENTITY
declaration (that is the billion-laughs signature, and re-extracting after the
hardened parser has refused a file would undo the protection the refusal
provides), and it only runs at all when the parser demonstrably left entries
behind. Everything it recovers goes through the same normalisation and the same
cross-domain policy as a parsed row, and loses the dedup to whatever the real
parser found.

**Bodies are re-encoded to UTF-8 before parsing.** `ultimate-sitemap-parser`
decodes every response as UTF-8 with replacement characters before the XML
parser runs, so a sitemap whose XML prolog says `ISO-8859-1` arrives with every
non-ASCII path mangled. The transport reads the charset from the prolog (the
document outranks the `Content-Type` header, which CDNs get wrong more often
than generators lie about their own bytes), re-encodes, and rewrites the prolog
to match. Bodies already in UTF-8 pass through byte for byte.

**Status probing is HEAD first, falling back to GET** when the server refuses
HEAD (`405` / `501`) or when HEAD produced no status at all. `403` is not a
fallback trigger: it is usually a real answer, and re-asking with GET is how a
tool starts probing for a hole. A timeout is not a trigger either — GET to the
same host would time out too. Retries happen once, only for connection
failures, timeouts and transient 5xx. 4xx is never retried.

**Dedup happens before status checking**, saving 10-30% of requests. Fragments,
default ports, doubled slashes, dot segments and percent-escapes of unreserved
characters are duplicate noise and collapse. Scheme, host and trailing slash
are real differences and are preserved — collapsing them would report a live
URL as dead.

**Cross-domain `<loc>` entries are recorded and never probed.** A sitemap that
lists a CDN or partner host must not make us send requests to that third party.

## Compliance

Only publicly published sitemap files are read. URL probing checks status codes
and does not collect page bodies: the GET fallback streams a bounded head to
recognise a block page and closes the stream immediately.

- `robots.txt` `Crawl-delay` is parsed and applied, capped at 60 s per request
  so a misconfigured file cannot stall a run for a year.
- `429` and `Retry-After` are honoured, in both the delay-seconds and HTTP-date
  forms.
- A target that keeps answering `403` or serves a bot-challenge is recorded and
  left alone. **No bypass is attempted and no proxy rotation is performed** —
  automatic proxy rotation is the compliance smell this project refuses to
  copy.
- The User-Agent names the tool and its project page. **No browser
  impersonation.**
- `startUrls` are resolved and refused if they point at private, link-local or
  cloud-metadata ranges — including IPv4 addresses embedded in IPv6 forms such
  as `::ffff:169.254.169.254`, which naive checks allow through.

## Verifying it

`tests/` holds an adversarial fixture corpus: normal XML, missing namespaces,
arbitrary prefixes, malformed and truncated XML, a body that ends early against
its own `Content-Length`, empty bodies, WAF challenge pages, HTML masquerading
as a text sitemap, gzip, double gzip, a `.gz` that is not gzip, a compression
bomb, an XML entity-expansion bomb, 50,000 URLs in one file, index-of-index,
cyclic indexes, duplicate entries, IDNA hosts, an ISO-8859-1 body whose paths
really use that charset, the `<lastmod>` zoo, cross-domain entries and SSRF
lures.

Acceptance is a measured number: **≥98% of fixtures handled, and no single bad
sample may kill the run.** Hostile samples count as handled only when they
report the declared `errorType` and emit no garbage rows.

```
python tests/test_corpus.py
```

The most recent measured run is archived in `../tests-corpus-run.txt`, with the
verdict line preserved rather than paraphrased.

### Known limits, stated rather than hidden

- A body that is **not** UTF-8 and **declares no charset at all** — in its XML
  prolog or in `Content-Type` — is read as UTF-8 with replacement characters.
  Detecting an undeclared 8-bit encoding would mean guessing, and guessing wrong
  produces confidently wrong URLs instead of visibly damaged ones.
- Non-ASCII **host** names are IDNA-encoded for the network, but the emitted
  `url` field keeps whatever the sitemap wrote.
- The SSRF check resolves the host and refuses blocked ranges, then the request
  is made by the HTTP client, which resolves again. The classic DNS-rebinding
  window is therefore open. Closing it would mean pinning the connection to the
  verified address, which breaks TLS SNI handling for little practical gain at
  this threat model; the gap is recorded here rather than papered over.

## License

GPL-3.0-or-later. This is not a choice: `ultimate-sitemap-parser` is GPL-3.0
and this package is a derivative work of it. Any distribution must remain
GPL-3.0-or-later. See `NOTICE` for the third-party attribution.

If a future version needs to be closed source, it cannot use
`ultimate-sitemap-parser`, and the parser would have to be written from
scratch. That trade is deliberate and recorded here so it is not discovered
later under pressure.
