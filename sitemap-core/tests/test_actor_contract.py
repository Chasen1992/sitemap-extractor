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


# --------------------------------------------------------------------------


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
