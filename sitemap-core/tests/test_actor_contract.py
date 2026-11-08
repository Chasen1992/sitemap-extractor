"""The Actor's published artifacts must agree with the code that backs them.

A store listing is a set of claims: this is how many inputs there are, this is
what the rows look like, this is the default you get if you type nothing. Every
one of those claims lives in a JSON file that cannot import a Python constant,
so every one of them is a copy that can go stale independently of the code.

Two went stale while this was being built, and both were found by hand rather
than by a test:

* ``maxUrls`` shipped as 10 000 in three places. Two were moved to 1 000 and
  ``src/main.py`` still fell back to 10 000 — so an API call that omitted the
  field got a forty-minute run while the README said "you get the same 1 000".
* ``actor.json``'s ``title`` was 65 characters. Apify's limit is 63, and the
  push was refused with ``Actor name must be at most 63 characters long``.

Neither is a bug in the parser. Both are the listing saying something the code
does not do, which is worse than a parser bug because nobody debugs the
listing. So this file compares the two, in both directions where it can.

Run standalone::

    python tests/test_actor_contract.py

or under pytest.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import sys
from typing import Any

_TESTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS_DIR))
sys.path.insert(0, str(_TESTS_DIR.parents[0]))

from sitemap_core.models import DEFAULT_MAX_URLS, ErrorType  # noqa: E402

# .../sitemap-core/tests -> .../apify-actors
_ACTORS_DIR = _TESTS_DIR.parents[1]
_ACTOR_DIR = _ACTORS_DIR / "sitemap-extractor"
_ACTOR_JSON = _ACTOR_DIR / ".actor" / "actor.json"
_INPUT_SCHEMA = _ACTOR_DIR / ".actor" / "INPUT_SCHEMA.json"
_DATASET_SCHEMA = _ACTOR_DIR / ".actor" / "dataset_schema.json"
_MAIN_PY = _ACTOR_DIR / "src" / "main.py"

# The Actor's boundary layer, so its pure helpers can be tested here.
# Appended rather than inserted: `sitemap_core` must keep resolving to
# sitemap-core/, the code under test, and not to the vendored copy that
# ships inside the Actor's build context.
sys.path.append(str(_ACTOR_DIR / "src"))

# Apify's own limits on an Actor's name/title and description. Over them the
# push is refused outright — measured, not remembered: a 65-character title was
# rejected on 2026-11-08, and the 302-character description that followed it
# was rejected the same way. Two round trips to learn two numbers.
APIFY_NAME_MAX = 63
APIFY_DESCRIPTION_MAX = 300

# Keywords Apify's input-schema validator refuses. Also measured, also during a
# build: `exclusiveMinimum` produced "Property schema.properties.<name>.
# exclusiveMinimum is not allowed." and the whole build went red. It is
# JSON-Schema draft-4 syntax; draft-6 made `exclusiveMinimum` numeric, and
# Apify accepts neither shape. `minimum` alone already says "> 0" when it is
# 0.01, which is what the field wanted.
FORBIDDEN_SCHEMA_KEYWORDS = ("exclusiveMinimum", "exclusiveMaximum", "id", "$schema")

# The prefilled run Apify re-tests every public Actor with daily must finish
# inside 5 minutes. Measured against a 24-sitemap / 3.69M-URL site, 200 rows
# with status checking on is 182 s — 39% under the gate. Raising this number
# is a bet against the network, not against the code.
PREFILL_MAX_SECONDS = 300
PREFILL_MEASURED_SECONDS = 182


def _load(path: pathlib.Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------------------
# 1. the default a user gets must be the default we say they get
# --------------------------------------------------------------------------


def test_the_schema_default_matches_the_code_default() -> None:
    """The input schema cannot import ``DEFAULT_MAX_URLS``, so it is a copy."""
    schema = _load(_INPUT_SCHEMA)
    default = schema["properties"]["maxUrls"]["default"]

    assert default == DEFAULT_MAX_URLS, (
        f"the input schema promises {default} rows by default and the code "
        f"delivers {DEFAULT_MAX_URLS}. The README quotes one of them and the "
        "user gets the other."
    )


def test_the_entry_point_does_not_carry_its_own_copy_of_the_default() -> None:
    """``src/main.py`` is the copy that went stale.

    Reading the source rather than importing it: ``main.py`` imports ``apify``,
    which is not installed here, and a test that needs a platform SDK to check
    a number is a test nobody runs.

    Asserted positively, not negatively. The first version of this test looked
    for a numeric literal and missed it — the fallback reads
    ``_as_int(actor_input.get("maxUrls"), 10_000)``, and the inner parentheses
    of ``.get(…)`` meant the "no literal here" pattern never matched at all.
    It passed on a file containing exactly the defect it was written for, and
    only failed because a second, blunter assertion happened to fire. Checking
    that the constant is named is the thing that actually pins the call site.
    """
    source = _MAIN_PY.read_text(encoding="utf-8")

    fallback_names_the_constant = re.search(
        r'max_urls\s*=\s*_as_int\(\s*actor_input\.get\(\s*"maxUrls"\s*\)\s*,\s*DEFAULT_MAX_URLS\s*\)',
        source,
    )
    assert fallback_names_the_constant is not None, (
        "src/main.py does not fall back to DEFAULT_MAX_URLS when maxUrls is "
        "absent from the input. This is the exact defect that shipped a "
        "10 000-row fallback beside a README promising 1 000: an API call that "
        "omits the field gets neither of the numbers anyone documented."
    )


def test_the_prefill_stays_inside_the_daily_test_gate() -> None:
    """Apify re-runs every public Actor daily on its prefilled input.

    The run has to succeed, produce non-empty output and finish in 5 minutes,
    or the Actor gets flagged — and a flag lowers the quality score, which is
    a search ranking factor. The prefill is therefore not a convenience value;
    it is a hard gate with a measured margin behind it.
    """
    schema = _load(_INPUT_SCHEMA)
    prefill = schema["properties"]["maxUrls"]["prefill"]

    assert prefill <= DEFAULT_MAX_URLS, (
        f"prefill {prefill} exceeds the default {DEFAULT_MAX_URLS}; the demo "
        "run would ask for more rows than the documented default"
    )
    assert PREFILL_MEASURED_SECONDS < PREFILL_MAX_SECONDS, (
        "the measurement this prefill was chosen from no longer fits the gate; "
        "re-measure before trusting the number"
    )


# --------------------------------------------------------------------------
# 2. the listing must not promise something the platform will refuse
# --------------------------------------------------------------------------


def test_the_actor_fields_fit_the_platform_limits() -> None:
    """Over these, the push is refused — nothing is built, nothing is published.

    Both were discovered by being refused rather than by reading the docs, which
    is the expensive way round. The limits are small enough to be worth a line
    each and fatal enough that a listing should never be one long sentence away
    from being un-pushable.
    """
    actor = _load(_ACTOR_JSON)
    limits = {
        "name": APIFY_NAME_MAX,
        "title": APIFY_NAME_MAX,
        "description": APIFY_DESCRIPTION_MAX,
    }
    for field, limit in limits.items():
        value = actor[field]
        assert len(value) <= limit, (
            f"actor.json {field} is {len(value)} characters, over Apify's "
            f"{limit}: {value!r}. The push is refused outright."
        )


def test_the_wiring_the_platform_reads_is_present() -> None:
    """Publishing needs input and output schemas and a README, or it is blocked."""
    actor = _load(_ACTOR_JSON)

    assert actor["input"] == "./INPUT_SCHEMA.json", actor["input"]
    assert actor["storages"]["dataset"] == "./dataset_schema.json", actor["storages"]
    readme = (_ACTOR_DIR / "README.md").resolve()
    assert readme.is_file(), f"actor.json points at {actor['readme']}, which is missing"
    assert len(readme.read_text(encoding="utf-8").split()) >= 300, (
        "the README is under 300 words; a listing this thin reads as abandoned"
    )


def test_the_input_schema_uses_no_keywords_the_platform_rejects() -> None:
    """A keyword Apify will not accept turns the whole build red.

    Nothing here runs locally against the platform's validator, so this is the
    cheap half of the check: catch the keywords known to be refused before
    spending a build on discovering them. It is not a substitute for the
    platform's own validation and should not be read as one.
    """
    schema = _load(_INPUT_SCHEMA)

    offenders: list[str] = []

    def walk(node: Any, where: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in FORBIDDEN_SCHEMA_KEYWORDS:
                    offenders.append(f"{where}.{key}")
                walk(value, f"{where}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{where}[{index}]")

    walk(schema, "$")
    assert not offenders, (
        f"the input schema uses keywords Apify's validator refuses: {offenders}. "
        "The build fails on these before any code runs."
    )


# --------------------------------------------------------------------------
# 3. the output schema must describe the rows the code actually emits
# --------------------------------------------------------------------------


def test_the_published_error_enum_is_the_code_error_enum() -> None:
    """Both directions.

    A value the code emits but the schema omits is silently un-filterable in
    the Console's Problems view. A value the schema lists but the code never
    emits is a promise nobody can keep. This is the one place where the
    published schema is allowed to be a copy, so it is checked exactly.
    """
    schema = _load(_DATASET_SCHEMA)
    published = set(schema["fields"]["properties"]["errorType"]["enum"])
    actual = {member.value for member in ErrorType}

    missing = actual - published
    assert not missing, (
        f"the output schema omits errorType values the code emits: {sorted(missing)}"
    )
    phantom = published - actual
    assert not phantom, (
        f"the output schema lists errorType values the code never emits: "
        f"{sorted(phantom)}"
    )


def test_the_output_schema_declares_the_fields_the_rows_carry() -> None:
    schema = _load(_DATASET_SCHEMA)
    declared = set(schema["fields"]["properties"])

    # Every field the code's UrlRecord produces must be declared, or the
    # Console drops it from the typed views without saying so.
    import sitemap_core.models as models

    record_fields = set(models.UrlRecord.__dataclass_fields__)
    undeclared = record_fields - declared
    assert not undeclared, (
        f"UrlRecord carries fields the output schema never declares: "
        f"{sorted(undeclared)}"
    )

    assert set(schema["fields"]["required"]) <= declared, (
        "the schema marks a field required that it does not declare"
    )


def test_the_output_schema_points_at_records_the_code_actually_writes() -> None:
    """The store listing will not publish without an output schema.

    It is a map of output IDs to URL templates, and a template is a promise
    about what exists at the other end: ``/records/SUMMARY`` is only a working
    link if something writes a record called ``SUMMARY``. The same defect class
    as the support links, which shipped pointing at a repository that was not
    there — a link nobody clicks is a bug nobody finds.

    Checked against the constants in ``src/main.py``, because that is where the
    writing happens. Reading the source rather than importing it, for the same
    reason as above.

    ``type`` is asserted because the first version of this file did not, and the
    build came back failed with ``must have required property 'type'``. The
    schema had been written from Apify's own *Output object definition* table,
    which lists ``title``, ``description`` and ``template`` and never mentions
    ``type`` — while every worked example in the same document sets
    ``"type": "string"``, and the validator requires it. The table is wrong and
    the validator is the specification. A guard built from a wrong table does
    not catch a defect built from the same wrong table.
    """
    actor = _load(_ACTOR_JSON)
    assert actor.get("output") == "./output_schema.json", (
        f"actor.json does not declare an output schema ({actor.get('output')!r}); "
        "the Store refuses to publish without one"
    )

    output_path = (_ACTOR_DIR / ".actor" / "output_schema.json").resolve()
    assert output_path.is_file(), f"actor.json points at {output_path}, which is missing"
    schema = _load(output_path)

    assert schema.get("actorOutputSchemaVersion") == 1, schema.get("actorOutputSchemaVersion")
    assert schema.get("title"), "the output schema needs a title"
    properties = schema.get("properties")
    assert isinstance(properties, dict) and properties, "the output schema has no outputs"

    source = _MAIN_PY.read_text(encoding="utf-8")
    written = set(re.findall(r'^[A-Z_]+_KEY\s*=\s*"([A-Z_]+)"\s*$', source, re.M))
    assert written, "could not find the record-key constants in src/main.py"

    promised: set[str] = set()
    uses_dataset = False
    for output_id, spec in properties.items():
        template = spec.get("template")
        assert template, f"output {output_id!r} has no template"
        assert spec.get("title"), f"output {output_id!r} has no title"

        # Every documented example uses "string" here whatever the template
        # points at, and the field is required. Asserted as the exact value, not
        # merely as present: the accepted set is not published anywhere, so the
        # evidence is the examples, and a value outside them is a guess.
        assert spec.get("type") == "string", (
            f"output {output_id!r} has type {spec.get('type')!r}. Apify's "
            "validator requires the field and all six worked examples in its "
            "output-schema doc set it to 'string'. The doc's field table omits "
            "it, which is how build 1.0.4 came back FAILED with \"must have "
            "required property 'type'\" after the schema had been written from "
            "that table."
        )

        match = re.search(r"\{\{\s*links\.apiDefaultKeyValueStoreUrl\s*\}\}/records/([A-Za-z0-9_-]+)", template)
        if match:
            promised.add(match.group(1))
        elif "links.apiDefaultDatasetUrl" in template:
            uses_dataset = True
        else:
            raise AssertionError(
                f"output {output_id!r} has a template neither the default dataset "
                f"nor a key-value record: {template!r}"
            )

    assert uses_dataset, (
        "no output links the default dataset, which is where Actor.push_data puts "
        "the rows — the run's actual result would be undiscoverable"
    )

    phantom = promised - written
    assert not phantom, (
        f"the output schema links key-value records nothing writes: {sorted(phantom)}. "
        f"src/main.py writes {sorted(written)}."
    )
    unlinked = written - promised
    assert not unlinked, (
        f"src/main.py writes records the output schema never links: {sorted(unlinked)}. "
        "They exist in the run but a user following the output tabs cannot find them."
    )


def test_the_cross_entry_is_countable_rather_than_merely_clickable() -> None:
    """A bare "open an issue" link cannot be counted.

    The plan asks two things of this artifact: a cross-entry that carries
    instrumentation, and a conversion figure from day one. A plain
    ``/issues/new`` link provides neither — a monitor request is
    indistinguishable from a bug report, and there is no denominator to divide
    by. That is the difference between "we linked to the next product" and "we
    can measure whether anyone followed".

    The marker goes in the **title**, because GitHub applies ``title`` whoever
    opens the issue, while ``labels`` is silently dropped for openers without
    push access. Both are asked for, so that one surviving is enough. The body
    is a question rather than an answer: a pre-filled sentence would inflate
    the count with clicks and hand back our wording instead of the user's, and
    the monitoring product's listing is supposed to be written in real users'
    own words.

    The store README carries a second copy of this link and drifts away from it
    on its own — the same defect class as ``maxUrls``, which shipped with three
    values in three files. Both copies are read and compared.

    The constants are pulled out of ``src/main.py`` with ``ast`` rather than
    restated here: a test that repeats the marker is a third copy, and this file
    already reads source it cannot import.
    """
    tree = ast.parse(_MAIN_PY.read_text(encoding="utf-8"))
    wanted = {"SUPPORT_URL", "MONITOR_REQUEST_TITLE", "MONITOR_REQUEST_LABEL"}
    values: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in wanted:
                    values[target.id] = ast.literal_eval(node.value)

    url = values.get("SUPPORT_URL", "")
    title = values.get("MONITOR_REQUEST_TITLE", "")
    label = values.get("MONITOR_REQUEST_LABEL", "")
    assert url and title and label, (
        f"src/main.py does not define the cross-entry constants; found {sorted(values)}"
    )

    assert url.startswith("https://github.com/Chasen1992/sitemap-extractor/issues/new"), (
        f"the cross-entry points somewhere other than the repository's new-issue "
        f"page: {url}"
    )
    assert f"title={title.replace(' ', '+')}" in url, (
        f"the cross-entry does not carry the countable title marker "
        f"{title!r}: {url}. Without it a monitor request cannot be told from a "
        "bug report, and the conversion figure the plan asks for cannot exist."
    )
    assert f"labels={label}" in url, (
        f"the cross-entry does not ask for the {label!r} label: {url}"
    )
    assert "body=" in url, f"the cross-entry has no prompt body: {url}"

    readme = (_ACTOR_DIR / "README.md").read_text(encoding="utf-8")
    links = re.findall(r"\((https://github\.com/[^()\s]*issues/new[^()\s]*)\)", readme)
    assert links, "the store README has no cross-entry link at all"
    uncountable = [link for link in links if link != url]
    assert not uncountable, (
        f"the store README points at the bare new-issue page where the countable "
        f"URL belongs: {uncountable}. Expected exactly {url!r}. A link nobody "
        "counts is a signal nobody has, which is how the monitor ends up being "
        "built on guesses."
    )


# --------------------------------------------------------------------------


def test_every_emitted_row_is_a_row_the_platform_will_store() -> None:
    """What the code emits has to be what the platform agrees to store.

    ``UrlRecord.to_dict`` used ``asdict``, which keeps every ``None``, so each
    row carried ``"statusCode": null`` and ``"errorType": null``. The dataset
    schema declares ``statusCode`` as ``integer`` and ``errorType`` as a
    26-value enum, and a JSON Schema ``type`` does not admit ``null`` — so
    ``Actor.push_data`` answered ``Schema validation failed`` for **every**
    batch. All three live runs reported 200 URLs behind an empty dataset, and
    each exited 0. The summary counted what the parser produced; nothing
    counted what the dataset accepted.

    The schema is validated server-side on push, which is the only reason this
    was loud at all. Had it not been, the columns would simply have been
    missing and the loss would still be silent. So the check is here as well as
    there: the boundary this file covers is "a row the code produces is a row
    the schema accepts", and it is checked before anything is shipped.

    The rows below are built from ``UrlRecord``'s own defaults rather than from
    the parser, because the defect was in the serialization and not in the
    parsing — an untouched ``UrlRecord`` already carried nine nulls. This
    exercises no network and does not claim to.

    The README's worked example is put through the same checker. That is the
    third defect of this shape in this project: the documentation asserting
    something the validator refuses. It is also the version users meet first —
    a documented row the platform would reject teaches the wrong shape to
    everyone who copies it.

    No ``jsonschema`` dependency. The subset of JSON Schema this file uses is
    ``type``, ``enum``, ``required`` and ``properties``, and a package is not
    worth four keywords; what matters is that every value a row carries is
    checked against the type and the enum declared for that key.
    """
    schema = _load(_DATASET_SCHEMA)
    fields = schema["fields"]
    declared = fields["properties"]
    required = set(fields.get("required", ()))

    def kind_of(value: Any) -> str:
        # ``bool`` before ``int``: ``True`` is an int in Python, and "integer"
        # is not what a JSON Schema ``boolean`` accepts.
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "number"
        if isinstance(value, str):
            return "string"
        return type(value).__name__

    def check(row: dict[str, Any], what: str) -> None:
        missing = required - set(row)
        assert not missing, f"{what} omits required fields: {sorted(missing)}"

        for key, value in row.items():
            spec = declared.get(key)
            assert spec is not None, (
                f"{what} carries {key!r}, which the dataset schema does not "
                "declare. Undeclared fields are dropped without a word, so the "
                "row would look complete and be missing data."
            )
            assert value is not None, (
                f"{what} carries {key!r} as null. A JSON Schema type does not "
                "admit null: the platform rejects the item and every row in "
                "the batch goes with it. That is the defect that emptied all "
                "three live runs."
            )
            want = spec.get("type")
            got = kind_of(value)
            type_ok = got in ("integer", "number") if want == "number" else got == want
            assert type_ok, (
                f"{what} carries {key!r} as {got}, but the dataset schema "
                f"declares it {want!r}. The push is refused, and so is every "
                "other row in the same batch."
            )
            enum = spec.get("enum")
            if enum is not None:
                assert value in enum, (
                    f"{what} carries {key!r} = {value!r}, which is not one of "
                    f"the {len(enum)} declared values."
                )

    import sitemap_core.models as models

    plain = models.UrlRecord(
        url="https://example.com/a", normalizedUrl="https://example.com/a"
    ).to_dict()
    # lastmodRaw is deliberately unparseable: a date that will not parse must
    # leave lastmodIso absent rather than null or a fabricated value.
    populated = models.UrlRecord(
        url="https://example.com/b",
        normalizedUrl="https://example.com/b",
        sourceSitemap="https://example.com/sitemap.xml",
        depth=1,
        checked=True,
        statusCode=200,
        contentType="text/html",
        lastmodRaw="2023-13-45",
        lastmodIso=None,
        priority=0.5,
        changeFrequency="daily",
        hasImages=True,
        alternateCount=2,
    ).to_dict()
    failed = models.UrlRecord(
        url="https://example.com/c",
        normalizedUrl="https://example.com/c",
        checked=True,
        errorType=ErrorType.REDIRECT_LOOP,
        errorMessage="too many redirects",
    ).to_dict()

    for row, what in (
        (plain, "an untouched UrlRecord"),
        (populated, "a fully populated UrlRecord"),
        (failed, "a failed UrlRecord"),
    ):
        check(row, what)

    assert "errorType" not in plain, (
        "an untouched UrlRecord now carries errorType. Absent is how a plain "
        "success says 'no problem', and a row that sets it is a row the "
        "Problems view will list."
    )

    readme = (_ACTOR_DIR / "README.md").read_text(encoding="utf-8")
    example = None
    for block in re.findall(r"```json\n(.*?)```", readme, re.DOTALL):
        if '"normalizedUrl"' in block:
            example = json.loads(block)
            break
    assert example is not None, (
        "the store README no longer contains a worked example of a dataset "
        "row. It is where a user first learns what the columns mean, and "
        "nothing else in this file reads it."
    )
    check(example, "the row the README shows")


def test_a_run_that_delivered_nothing_does_not_report_success() -> None:
    """Zero rows in the dataset is two different stories; only one is success.

    Three live runs each reported "Succeeded 200 URLs from 24/24 sitemaps"
    behind an empty dataset and exited 0. Every batch had been refused by the
    platform's schema check and the failure was logged and dropped. The fix is
    not only the serialization — it is refusing to let "the dataset is empty"
    be read as fine.

    The hard part is the pair this function has to keep apart. A run cut at
    `maxTotalChargeUsd` **also** ends with zero rows and a summary full of
    counted URLs, and that one is correct behaviour: the user asked for the
    cut, `SUMMARY` says so, and the exit stays 0. Same observable state,
    opposite verdict. So the verdict is a function, and it is tested here
    directly rather than through `main`, which would need the Apify SDK's
    runtime to reach.

    Case 6 is the one that would have shipped a new bug on the way to fixing
    the old one: a run that is *both* cut on purpose *and* has refused batches
    must still be a failure, because "we stopped on purpose" is not an account
    of rows that were lost by accident.
    """
    import main as actor_main

    verdict = actor_main._delivery_verdict

    def case(
        *,
        produced: int,
        delivered: int,
        lost: int = 0,
        lost_batches: int = 0,
        cut_on_purpose: bool = False,
        want_failed: bool,
        want_fatal: bool = False,
        want_truncate_reason: str | None = None,
        label: str,
    ) -> None:
        got = verdict(
            produced=produced,
            delivered=delivered,
            lost=lost,
            lost_batches=lost_batches,
            cut_on_purpose=cut_on_purpose,
        )
        assert got.failed is want_failed, f"{label}: failed={got.failed}"
        assert got.fatal is want_fatal, f"{label}: fatal={got.fatal}"
        assert got.truncate_reason == want_truncate_reason, (
            f"{label}: truncate_reason={got.truncate_reason!r}"
        )
        assert (got.reason is None) is (not want_failed), (
            f"{label}: a verdict that failed must say why, and one that did "
            f"not must stay silent; got {got.reason!r}"
        )

    case(
        label="1 everything delivered",
        produced=200, delivered=200,
        want_failed=False,
    )
    case(
        label="2 a site with no sitemap",
        produced=0, delivered=0,
        want_failed=False,
    )
    case(
        label="3 every batch refused",
        produced=200, delivered=0, lost=200, lost_batches=4,
        want_failed=True, want_fatal=True,
    )
    case(
        label="4 cut at the charge limit, nothing pushed",
        produced=200, delivered=0, lost=0, lost_batches=0,
        cut_on_purpose=True,
        want_failed=False,
    )
    case(
        label="5 some batches refused, the rest delivered",
        produced=200, delivered=199, lost=1, lost_batches=1,
        want_failed=True, want_fatal=False,
        want_truncate_reason="push_failed",
    )
    case(
        label="6 cut on purpose AND some batches refused",
        produced=200, delivered=100, lost=20, lost_batches=1,
        cut_on_purpose=True,
        want_failed=True, want_fatal=False,
        want_truncate_reason="push_failed",
    )
    case(
        label="7 cut at the charge limit after a partial delivery",
        produced=200, delivered=100, lost=0, lost_batches=0,
        cut_on_purpose=True,
        want_failed=False,
    )
    case(
        label="8 nothing arrived and nothing explained it",
        produced=200, delivered=0, lost=0, lost_batches=0,
        want_failed=True, want_fatal=True,
    )

    total = verdict(
        produced=200, delivered=0, lost=200, lost_batches=4, cut_on_purpose=False
    )
    assert "200" in (total.reason or ""), (
        "the total-loss message has to name how many rows were lost, or the "
        "user is left to find the number in the summary themselves"
    )
    assert "failure, not a truncation" in (total.reason or ""), (
        "the one sentence that separates this from a budget cut has to be in "
        "the message, because the status line is all most people read"
    )


CASES = [
    test_the_schema_default_matches_the_code_default,
    test_the_entry_point_does_not_carry_its_own_copy_of_the_default,
    test_the_prefill_stays_inside_the_daily_test_gate,
    test_the_actor_fields_fit_the_platform_limits,
    test_the_wiring_the_platform_reads_is_present,
    test_the_input_schema_uses_no_keywords_the_platform_rejects,
    test_the_published_error_enum_is_the_code_error_enum,
    test_the_output_schema_declares_the_fields_the_rows_carry,
    test_the_output_schema_points_at_records_the_code_actually_writes,
    test_the_cross_entry_is_countable_rather_than_merely_clickable,
    test_every_emitted_row_is_a_row_the_platform_will_store,
    test_a_run_that_delivered_nothing_does_not_report_success,
]


def main() -> int:
    failures = 0
    for case in CASES:
        try:
            case()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {case.__name__}: {exc}", flush=True)
        except Exception as exc:  # a crash is a failure too
            failures += 1
            print(f"FAIL  {case.__name__}: {type(exc).__name__}: {exc}", flush=True)
        else:
            print(f"ok    {case.__name__}", flush=True)
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
