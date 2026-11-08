# Evidence archive

The plan's acceptance line for "做得扎实" is a measured number, not a
description:

> 验收线：对整套语料 ≥98% 成功，且任何单个坏样本都不能让整次运行挂掉。
> 语料跑出来的数字截图存档 —— 这才是"做得扎实"的证据

## Adversarial fixture corpus

| | |
| --- | --- |
| Date measured | 2026-11-08 (first measured 2026-11-05, re-run after each fix) |
| Command | `python -u tests/test_corpus.py` (from `sitemap-core/`) |
| Raw output | [`tests-corpus-run.txt`](tests-corpus-run.txt) — captured to file, not retyped |
| Fixtures handled | **28 / 28 (100.0%)** |
| Acceptance line | ≥ 98.0% |
| Runs killed | **none** |
| Extra checks | all passed — no garbage rows, SSRF strict, no test switches |
| Verdict | **PASS** |

28 fixtures, each with its own row count and its own `errorType` where one is
expected — the run table in `tests-corpus-run.txt` is the authoritative list:

| | | |
| --- | --- | --- |
| `plain.xml` | normal urlset | 4 rows |
| `no_namespace.xml` | no XML namespace | 3 rows |
| `bad_prefix.xml` | arbitrary element prefix | 2 rows |
| `malformed.xml` | bare `&`, unclosed tag, stray close | 4 rows via the recovery net |
| `empty.xml` | empty body | `empty_body` |
| `truncated.xml` | cut mid-element | 3 rows |
| `truncated_body.xml` | body ends early against its own `Content-Length` | `truncated` |
| `html_challenge.xml` | WAF page served as a sitemap | `html_challenge` |
| `robots_html.txt` | HTML returned where robots.txt was expected | `not_xml` |
| `billion_laughs.xml` | XML entity-expansion bomb | `parse_error` |
| `news_image.xml` | image / news / xhtml extension namespaces | 2 rows |
| `bad_dates.xml` | the `<lastmod>` zoo | 12 rows, none dropped |
| `idna.xml` | non-ASCII host and path | 4 rows |
| `latin1.xml` | ISO-8859-1 body, non-ASCII paths | 3 rows, no mojibake |
| `dup_entries.xml` | duplicates across index and child layers | 5 rows, 15 deduped |
| `sitemap.txt` | plain text with `#` comments | 4 rows |
| `cross_domain.xml` | CDN / partner hosts | 4 rows, all marked not probed |
| `gzipped.xml.gz` | normal gzip | 4 rows |
| `double_gzip.xml.gz` | double gzip | 4 rows |
| `fake_gz.xml.gz` | `.gz` name, not gzip | `not_gzip` |
| `renamed.xml.gz` | `.gz` name, plain XML inside | 4 rows |
| `gzip_bomb.xml.gz` | compression bomb | `gzip_bomb` |
| `huge.xml` | 50,000 URLs in one file | 50,000 rows in 2.8 s |
| `index_of_index.xml` | WordPress three-level index | 5 rows |
| `index_mid.xml` | index of index at depth 2 | 6 rows |
| `cyclic_a.xml` | A → B → A, with a harvestable sibling | 2 rows incl. `/survivor` |
| `ssrf_lure.xml` | private / link-local / metadata targets | 5 rows, none probed |
| `status_probe.xml` | live status probing | 200 / 200 / 404 / 429 |

A hostile sample counts as **handled** only when it reports its declared
`errorType` and emits no garbage rows. It does not count as handled merely
because the run survived it.

## The defects this corpus did not catch

28/28 was passing when real bugs were found in the field. Worth writing down,
because this is the clearest available statement of what the number is worth.
Four different unreachable paths, all found outside the corpus. The
fourth is the worst of the four and is at the end of this section.

### 1. The cross-domain policy

The first run against a live site — `https://www.apify.com` — returned **30 rows
out of 30 marked `crossDomain: true` and `errorType: cross_domain`**. Nothing
was checked. The cause: the sitemap lists `https://apify.com/…` (the apex) while
the seed was `https://www.apify.com`, and `is_same_site` returned `False` for
that pair whenever `includeSubdomains` was off — which is the default. It
answered "not your site" about the target's own URLs.

The corpus could not have caught it and still cannot. Every fixture is served
from `127.0.0.1`, so `seed_host` is always `127.0.0.1` and every `<url>` in
every fixture is genuinely foreign to it. The policy function is exercised only
along one branch.

So end-to-end coverage is not a substitute for testing the decision itself:

| | |
| --- | --- |
| Fixed in | `sitemap_core/normalize.py`, `is_same_site` |
| Rule now | `www.example.com` and `example.com` are one site regardless of `includeSubdomains`; that flag governs real subdomains only |
| Test added | `tests/test_normalize.py` — 18 cases, including every branch the corpus structurally cannot reach |
| Result | **18 / 18**, re-run after the fix |
| Corpus after fix | **28 / 28 (100.0%)**, `runs killed: none` — unchanged |
| Live re-test | `www.apify.com`, 30 rows, **`crossDomain` 0, `errorBreakdown` empty** |

### 2. The discovery path

Every fixture is seeded with a direct sitemap URL (`/fixtures/plain.xml`), so
the walk takes the `direct` branch. `sitemap_tree_for_homepage` — the branch a
user hits by typing a **domain** — is never called by the corpus, and the
synthetic tree it builds is never described by it. Six defects lived there.

| # | What the run reported | What was true |
| --- | --- | --- |
| 1 | `errorType: no_sitemap` for any site listing its sitemap in robots.txt at a non-standard path | The site publishes a sitemap. **robots.txt was discarded before USP read it** |
| 2 | `go.dev` → `ok: true, kind: "sitemapindex"`, `1/1 sitemaps succeeded` | The site publishes no sitemap at all. robots.txt lists none; every well-known path 404s |
| 3 | `www.apify.com` → `pagesFound: 922814` on the website node **and** on the index beneath it | 922,814 URLs, counted once per level of index above them |
| 4 | A site publishing one sitemap index reported **two** | robots.txt and a well-known path name the same file; USP spells one of them with a doubled slash, so it was recorded twice |
| 5 | `robots.txt` answering HTML → *"HTML document returned where **a sitemap** was expected"* | The request was for `robots.txt`. No sitemap was ever expected there; the tool made the request itself |
| 6 | No sitemap found → *"robots.txt lists none and no well-known sitemap path responded"* | On a soft-404 site, **neither clause is true**: robots.txt is never read, and the paths do respond, with HTML |

Defect 1 is the one that matters. The sniffer classifies every fetched body,
and it called `User-agent: *` *"neither XML, a text sitemap, nor recognizable
HTML"*. So discovery only ever found sitemaps sitting at a well-known path.
The tool worked whenever a site used `/sitemap.xml` and silently reported
**"this site has no sitemap"** whenever it used robots.txt — the primary way
it is done. It was not a cosmetic number; it was a wrong answer about the
site, and half the self-test below was measuring it.

Defects 2–4 changed a number the user reads; none of them changed a single URL
in the output of a run that found anything. Defects 5–6 changed no number at
all — they changed **what the tool claimed to have seen**.

| | |
| --- | --- |
| Fixed in | `sniff.py` (recognise robots.txt, and check it before the challenge sweep; name the thing the request was for), `models.py` (`NO_SITEMAP`, `SYNTHETIC_KINDS`), `discover.py` (`_collect_sitemap_records`, `_mark_empty_discovery`, `_empty_discovery_reason`), `pipeline.py` (count only real sitemap files) |
| Rule now | robots.txt is readable content and is checked before the block-page markers it can legitimately contain; discovery scaffolding is labelled `website`/`robots` and never counted as a sitemap file; `pagesFound` counts the file, not the subtree; one record per file, identified by `normalize_url`; **a message may only assert what was actually fetched and what actually came back** |
| Test added | `tests/test_discovery.py` — 18 cases against a local server with a **homepage** seed, the branch no fixture reaches |
| Result | **18 / 18**, and the corpus re-run afterwards: **28 / 28 (100.0%)**, `runs killed: none` — `fake_gz.xml.gz` still `not_gzip`, `html_challenge.xml` still caught |
| Regression proof | Restoring the two old wordings turns **8** of the 18 red, each naming the defect; the sitemap-path case stays green, so it is not "any change fails" |
| Live re-test | `go.dev` → one record, `kind: website`, `ok: false`, `errorType: no_sitemap`, `sitemapsAttempted 0`, `errorBreakdown {"no_sitemap": 1}` — and a sitemap named **only** in robots.txt is now found. `chinafilm.gov.cn` → `robots.txt could not be read (HTML document returned where robots.txt was expected); 8 candidate sitemap URL(s) tried, none served a sitemap (most common outcome: not_xml)` |

The pattern is the point. Three of the first four were visible in a live run's
own output — a `no_sitemap` verdict printed next to `sitemaps 1/1`, a page
count that appeared twice — and were found by **reading the output**, not by a
test. The fourth was found by distrusting a `no_sitemap` verdict and probing
the site by hand. Defects 5 and 6 were found the same way: the log said
*"HTML document returned where a sitemap was expected"* about a `robots.txt`
request, and *"no well-known sitemap path responded"* about paths that had
answered. **Both sentences were about the tool's own requests, and both were
false.** Neither can be found by running more fixtures either — the corpus
cannot reach this code — and neither can be found by trusting the message,
which is the only thing a user has.

### 3. The summary's account of itself

The corpus drives `collect_rows` on one fixture file at a time. `RunSummary` is
never built, so nothing in the suite ever reads `truncated`. A run stopped by
`maxUrls` reported this:

```
ok: true          truncated: false          urlsEmitted: 200
notes: ["stopped emitting at maxUrls=200; 3691056 more were found"]
```

The flag contradicted the note in the same object. `ok: true, truncated: false`
is the summary's way of saying *this is the complete list*, and it said so about
a run that had left 3.69 million URLs behind. It is the same class of defect as
the page count above: a number the reader trusts was wrong, and the correct
answer was sitting one field below it.

It matters more than the other two because this is the **demo output** — the
prefilled run Apify re-tests every public Actor with, and the first thing a new
user sees.

| | |
| --- | --- |
| Fixed in | `pipeline.py` (capped runs set `truncated`/`truncateReason`), `models.py` (field meaning) |
| Rule now | `truncated` means *the rows you have are not the whole answer*. Both a spent budget and `maxUrls` set it; `truncateReason` says which — `requests`/`bytes`/`seconds` or `max_urls`. A budget reason wins when both apply, because it leaves rows that were never checked at all rather than merely fewer rows |
| Test added | `tests/test_summary.py` — 5 cases |
| Result | **5 / 5**, and the corpus re-run afterwards: **28 / 28 (100.0%)**, `runs killed: none` |
| Live re-test | same run now reports `truncated: true`, `truncateReason: "max_urls"`, and the status message reads `200 URLs from 24/24 sitemaps (truncated)` |

**The suite caught a mistake of mine while this was being fixed.** I first
solved the testability problem by threading a `permit_networks` switch through
`pipeline.extract`. The corpus's extra checks refused the run:

```
extra checks : 1 problem(s)
   - pipeline.extract exposes test-only switch 'permit_networks'
VERDICT: FAIL
```

The guard is named and deliberate — `test_corpus.py` asserts `extract` carries
none of `permit_networks`, `permit`, `allow_private`, `ssrf_off` — and it is
right. `extract` is the Actor-facing boundary; a switch that turns off the SSRF
check there is one careless parameter away from aiming a public tool at the
host's own metadata service. The transport may take a permit list (the tests
use one) and the pipeline must not forward it. The fix was reverted and the
test injects the allowance at the transport instead, where it already lives.
A guard that only ever passes is decoration; this one failed on its author.

### 4. The rows were counted before they were delivered

The worst one, and the only one a user found. A run against a live site
reported **"Succeeded 200 URLs from 24/24 sitemaps"** and the Output tab was
empty. Not nearly empty — `itemCount: 0`. Checked afterwards, **all three
platform runs**, builds 1.0.2, 1.0.3 and 1.0.5, had delivered nothing, and
each had exited 0.

Three failures stacked. Any one of them alone would have been survivable.

**The serialization.** `UrlRecord.to_dict` returned `asdict(self)`, which
keeps every field, `None` ones included, so each row carried nine nulls:
`"statusCode": null`, `"errorType": null`, `"contentType": null` and the
rest. `dataset_schema.json` declares `statusCode` as `integer` and
`errorType` as a 26-value enum, and a JSON Schema `type` does not admit
`null`. The platform validates on push — which is the only reason this was
loud at all — and answered `Schema validation failed` for every batch.

**The swallowed exception.** `_drain` caught that, logged it, and carried
on. The log held `pushing a batch to the dataset failed` and a traceback;
the run ended `Exiting Actor ({"exit_code": 0})`. A failure that is logged
and then discarded is a failure that is not a failure.

**The status message.** It was built from `summary.urlsEmitted`, which
counts what the parser produced. The count of what the dataset accepted was
in the same summary — `rowsPushed: 0` — and nothing read it. The run told
the user 200; the truth was 0; both numbers were in the record and only the
wrong one was displayed.

That third one is the general lesson and it is worth more than the fix. A
field that reports the truth changes nothing unless something acts on it.
The counter that would have caught this had been written, was correct, and
was ignored.

Why the corpus missed it: the fixtures are checked as Python objects. They
never become JSON, and they never cross the platform boundary. The corpus
tests the parser. This defect was in the last mile, and the last mile had
no tests at all.

And the schema's own descriptions said *"Null when the URL was not checked"*
over a field declared `"type": "integer"`. Third defect in this project of
exactly that shape — the documentation asserting something the validator
refuses. The first two cost a build and a publishing gate. This one cost
three runs' worth of data.

Writing the fix produced the next bug, which is why this entry has two
guards rather than one. A run cut at `maxTotalChargeUsd` **also** ends
with an empty dataset and a summary full of counted URLs. It is the same
observable state as the failure, and the first draft of the delivery
check judged it a failure too. Reporting correct behaviour as a crash is
not a smaller error than the reverse, so the verdict became a function
that keeps the pair apart, and the second case pins that pair from both
sides. It was verified to fail on three mutations, the first of them
precisely this confusion — caught here rather than in a user's run.

| | |
| --- | --- |
| Found by | the user, reading a live run's Output tab |
| Fixed in | `models.py` (`to_dict` omits `None`, both record types), `src/main.py` (delivery check, honest status message, push counters), `dataset_schema.json` ("Absent when…" where the type cannot admit null), `README.md` (the worked example showed nulls too) |
| Rule now | a row omits a field that does not apply, never sends null; a run that produced rows and delivered none exits non-zero; the status message reports what was delivered, and shows both numbers when they differ |
| Test added | `tests/test_actor_contract.py` — two cases. `test_every_emitted_row_is_a_row_the_platform_will_store` validates emitted rows **and the README's own worked example** against the declared types and enums. `test_a_run_that_delivered_nothing_does_not_report_success` pins the verdict on what the dataset accepted |
| Verified | the first guard fails on five independent mutations — nulls from `to_dict`, nulls in the README example, `statusCode` declared `string`, `redirect_loop` dropped from the enum, an undeclared field in the README. The second fails on three: a charge-limit cut misread as a failure, refused batches treated as fine, and a total loss no longer called fatal. Each passes once reverted |
| Result | **12 / 12**, and the full suite on both line-ending forms: 5/5 + 9/9 + 18/18 + 12/12 + 28/28, `runs killed: none` |

## The run Apify re-tests every day

Apify re-runs every public Actor daily **on its prefilled input**. The run must
succeed, produce non-empty output, and finish **within 5 minutes**, or the Actor
gets flagged. That makes the prefill a hard gate rather than a nicety — and the
prefill I had shipped would have failed it on day one.

Measured, not estimated. Same shipped entry point, `src/main.py`:

| input | wall clock | vs 300 s | rows | rows with an error |
| --- | --- | --- | --- | --- |
| `maxUrls: 10000` (the shipped default) | **killed at 900 s, never finished** | **over 3x** | — | — |
| `www.apify.com`, `maxUrls: 500` | 256 s | 1.2x | 500 | 2 |
| `nodejs.org`, `maxUrls: 500` | 147 s | 2.0x | 500 | **321** |
| `www.djangoproject.com`, `maxUrls: 500` | 496 s | **over** | 500 | 81 |
| `www.apify.com`, `maxUrls: 300` | 216 s | 1.4x | 300 | 2 |
| **`www.apify.com`, `maxUrls: 200`** | **182 s** | **1.6x** | 200 | **2** |

Two of those three targets are unusable as a demo, and the reasons are the
target's, not the tool's — worth recording because each looks like a bug until
you check:

* **`nodejs.org` — 321 of 500 rows are 404.** Its sitemap still lists Node 0.10
  release posts (`/en/blog/release/v010.0`, `v010.1`, …) that are gone. The
  tool correctly reported the target's own dead URLs. As a demo it reads as
  "this tool is broken", so it is the wrong target.
* **`www.djangoproject.com` — 496 s, 81 rows rate-limited.** It returned 429s
  and the tool backed off and reported `rate_limited` rather than calling them
  dead links. Correct behaviour, and slow for exactly that reason.

So the prefill is `www.apify.com` with `maxUrls: 200` — clean output (99%
success), a target that cannot vanish, and 118 s of margin against the gate.
`maxUrls` carries `prefill: 200` in the input schema and its description now
states the ~0.3 s-per-URL cost. The shipped **default** came down from 10 000
to **1 000** on the same reasoning, measured rather than guessed:

| rows | wall clock | vs the 300 s gate |
| --- | --- | --- |
| 200 | 182 s | fits |
| 500 | 256 s | fits |
| 1 000 | 418 s | over — but the default is not the prefill |
| 10 000 | killed at 900 s, unfinished | hopeless |

The default is what a user gets if they press Start and change nothing. At
10 000 that was a run nobody would finish; at 1 000 it is about seven minutes,
which is a deliberate request rather than an accident. The prefill stays at 200
because the daily gate is 300 s and the margin has to survive the network, not
just the code.

## What the number does and does not prove

**It proves** the parser survives the specific failure modes real sitemaps
actually have, and that none of them takes the run down. That is the claim the
listing makes, and this is the measurement behind it.

**It does not prove** correctness against arbitrary input, nor that the tool is
a good fit for any particular user's site. A corpus is a floor, not a
certificate.

**It does not prove the policy is right.** Both sections above are the
demonstration: 28/28 while the cross-domain policy was marking the target's
own sitemaps as foreign, and 28/28 while discovery reported a sitemap-less site
as a successful empty sitemap index. Anything a fixture cannot reach by
construction has to be tested directly — `tests/test_normalize.py` and
`tests/test_discovery.py` exist for that one reason.

**It does not prove the reporting is right, or that discovery is complete.**
The corpus checks that hostile files are survived and typed. Eight defects
were live while it reported 28/28: one wrong verdict on which URLs were ours,
four wrong numbers about what discovery found, one wrong claim that the output
was complete, and two claims about what the tool had observed that it had
never observed. One of them made the tool say "this site has no sitemap" about
sites that publish one; two of them made it describe its own requests
incorrectly.

## Ten real domains

`selftest_10.py` runs the shipped entry point — `src/main.py`, the same file
the Docker image runs — against ten public technical sites and reports two
numbers honestly: how many succeeded, and how long it took. Full output in
`selftest-report.txt`.

```
domains run        : 10
succeeded          : 5  (50.0%)
  breakdown        : ok=5, truncated=0, no_sitemap=5, sitemap_unreadable=0, failed=0
total elapsed      : 274.5s
per domain, mean   : 27.5s
```

**Read 50% carefully. It is not a tool failure rate.** `failed=0` is the tool's
number. The other five are five sites that publish no sitemap — verified by
hand, not assumed: robots.txt on python.org, react.dev and mozilla.org carries
no `Sitemap:` line, and kernel.org serves no robots.txt at all. Reporting them
as anything else would be the lie defect 2 above made.

| domain | result | rows | sitemaps | URLs found | secs |
| --- | --- | --- | --- | --- | --- |
| www.python.org | no_sitemap | 0 | 0/0 | 0 | 20.1 |
| docs.python.org | ok | 7 | 1/1 | 35 | 10.0 |
| www.djangoproject.com | ok | 200 | 1/1 | 4,092 | 14.5 |
| nodejs.org | ok | 200 | 2/2 | 8,620 | 16.4 |
| go.dev | no_sitemap | 0 | 0/0 | 0 | 17.2 |
| react.dev | no_sitemap | 0 | 0/0 | 0 | 13.1 |
| developer.mozilla.org | ok | 200 | 11/11 | 269,964 | 30.4 |
| www.mozilla.org | no_sitemap | 0 | 0/0 | 0 | 23.8 |
| www.kernel.org | no_sitemap | 0 | 0/0 | 0 | 9.9 |
| www.apify.com | ok | 200 | 24/24 | 3,691,256 | 118.9 |

`rows` is capped at 200 by the run parameters; `URLs found` is what the
sitemaps actually listed.

**The same ten domains, run before and after the robots.txt fix** — this is the
measurement that shows defect 1 was not cosmetic:

| domain | sitemaps before | after | URLs found before | after |
| --- | --- | --- | --- | --- |
| nodejs.org | 1/1 | **2/2** | 6,680 | **8,620** |
| docs.python.org | 1/1 | 1/1 | 28 | **35** |
| developer.mozilla.org | 11/11 | 11/11 | 202,473 | **269,964** |
| www.apify.com | 23/23 | **24/24** | 2,768,442 | **3,691,256** |

The "before" run already had the duplicate-record fix of defect 4 applied;
only defect 1 differs between the two columns. Sitemaps that only robots.txt
names were invisible before — that is what the 28/28 corpus was passing
through the whole time.

## The listing must not contradict the code

A store listing is a set of claims: how many inputs there are, what the rows
look like, which default you get if you type nothing. Every claim lives in a
JSON file that **cannot import a Python constant**, so every claim is a copy
that can go stale on its own. Three did, and all three were found by hand.

| What drifted | What it meant |
| --- | --- |
| `maxUrls` default was `10 000` in `INPUT_SCHEMA.json`, in `pipeline.extract` and in `src/main.py` | The first two moved to 1 000 and `main.py` still fell back to 10 000 — so an API call omitting the field got a 40-minute run while the README said *"you get the same 1 000"* |
| `actor.json` `title` was 65 characters | Apify's limit is 63. The push was refused: `Actor name must be at most 63 characters long` |
| `actor.json` `description` was 302 characters | Apify's limit is 300. Refused the same way, one round trip later |

So the default is now one constant, `DEFAULT_MAX_URLS`, and the copies that
cannot use it are pinned by `tests/test_actor_contract.py` (12 cases) — which
also asserts the published `errorType` enum equals the code's enum **in both
directions**, because an omitted value is silently un-filterable in the
Console's Problems view and a listed value nobody emits is a promise nobody
can keep.

A fourth came from the platform rather than from the code, and was not drift
but absence: the Store refused to publish with *"Add 'Output' schema(s) to the
source code and rebuild the Actor before publishing."* `actor.json` had a
`storages.dataset` schema and that is **not** the same thing — an output
schema is the `output` key, a map of output IDs to URL templates, and it is a
publishing prerequisite. It now exists as `.actor/output_schema.json`, and it
is pinned by the ninth case for the same reason as the support links: a
template is a promise about what exists at the other end, and
`/records/SUMMARY` is only a working link if something writes a record called
`SUMMARY`. Verified to fail both ways — naming a record nothing writes, and
omitting a record the code does write.

That schema was then refused by the platform's own validator. Apify's *Output
object definition* table lists three fields per output — `title`,
`description`, `template` — and the file was written from it. Build **1.0.4
came back FAILED** after 0.6 s with `must have required property 'type'`. The
field is not in that table. Every worked example in the same document sets
`"type": "string"`, and the validator requires it: the table is wrong and the
validator is the specification. Build 1.0.5 added `type` to all four outputs
and succeeded.

Third time in this project that a guard built from a specification missed the
defect produced from the same specification. So the ninth case asserts `type`
is `"string"` on every output — the exact value, not merely present, because
the accepted set is published nowhere and a value outside the six documented
examples would be a guess. Verified to fail both ways: delete `type`, or set it
to `"object"`, and the case reports which output and why.

**The first version of its `main.py` guard did not work.** It looked for a
numeric literal in the fallback and missed it entirely: the call reads
`_as_int(actor_input.get("maxUrls"), 10_000)`, and the inner parentheses of
`.get(…)` meant the "no literal here" pattern never matched. The test passed on
a file containing exactly the defect it was written for, and only failed
because a second, blunter assertion happened to fire in the same function.

Rewritten as a positive assertion — *the fallback must name the constant* — and
verified in both directions: it passes on the shipped code, and it fails on a
copy with `DEFAULT_MAX_URLS` replaced by `10_000` at the call site while the
constant is still imported. That is the second guard in this project to fail on
its own author while passing on broken code. The first is documented above,
under the SSRF switch. A guard is worth what it does when the code is wrong,
not what it prints when the code is right.

| | |
| --- | --- |
| Fixed in | `models.py` (`DEFAULT_MAX_URLS`), `parse.py`, `pipeline.py`, `src/main.py`, `actor.json` |
| Rule now | one constant for the shipped default; the schema copy, the platform field limits and the published error enum are asserted against the code, not trusted |
| Test added | `tests/test_actor_contract.py` — 8 cases at the time; 12 today — the ninth being the output-schema case
described above, the tenth the countable cross-entry link, and the
eleventh and twelfth the dataset-row and delivery cases in section 4 |
| Result | **8 / 8**, and the full suite re-run afterwards: 5/5 + 9/9 + 18/18 + 28/28, `runs killed: none` |

## Reproducing

```
cd sitemap-core
python -u tests/test_corpus.py         # the 28-fixture corpus
python -u tests/test_normalize.py      # the cross-domain policy cases
python -u tests/test_discovery.py      # the discovery path no fixture reaches
python -u tests/test_summary.py        # the run summary's account of itself
python -u tests/test_actor_contract.py # the listing vs the code
```

Fixtures are generated by `tests/make_fixtures.py` and served over a local HTTP
server, so the run exercises the real transport — budgets, sniffing, gzip
guards, SSRF policy and rate limiting — rather than calling the parser on
strings.

## The repository reproduces the numbers

A number nobody can reproduce is a claim. Two things were checked before this
repository was published, and both by running the suites rather than by
reasoning about the files.

**Line endings.** The working copies on the machine the work was done on are
CRLF; commits normalise to LF. Both forms were run in full, on the same code:

| tree | result |
| --- | --- |
| CRLF working copies | 28/28 (100.0%), runs killed: none, plus 5/5 + 9/9 + 18/18 + 12/12 |
| LF form of the same code, read from the index blobs | 28/28 (100.0%), runs killed: none, plus 5/5 + 9/9 + 18/18 + 12/12 |

The LF row is worded that way because an *LF checkout* is not something this
repository can produce. `git checkout-index` under the checked-in
`* text=auto` wrote 82 of 90 files with CRLF even with `core.autocrlf=false` —
`text=auto` defers to `core.eol`, and `core.eol` is `native` on Windows. Only
reading the blobs with `git cat-file blob` bypasses the smudge filter; that
export was 90 files, 0 containing CRLF, with all 5 `.gz` payloads byte-identical
to the working copies.

So line endings are not load-bearing for any fixture. Only the `.gz` payloads
are pinned as binary, because their bytes are the thing under test.

**What round-trips, and what does not.** The `.gz` payloads come back
byte-for-byte. The text fixtures do not, and are not supposed to: a fixture
that is 470 bytes with CRLF is 463 bytes with LF, and that is the whole
difference. It was checked with `git hash-object` and a fresh checkout rather
than assumed from the attribute file. The two vendored copies of the parsing
core do carry the same blob hash, and `sync_core.py` agrees —
`vendored copy is in sync (12 files)`.

Worth recording because the first attempt at this got it backwards. The
original `.gitattributes` pinned `-text` on the fixtures to protect them from
line-ending conversion, which would have committed the CRLF copies — the exact
opposite of the portable form — and its comment claimed the other
byte-sensitive fixtures were "already classified as binary" by git. The add
warnings said otherwise. Neither claim survived contact with `git check-attr`.
