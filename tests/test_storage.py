import pytest

from rollout_ready.models import Case, Criterion, Suite
from rollout_ready.storage import Store, SuiteError, rubric_hash


def test_new_versions_get_the_next_number(suites_copy):
    store = Store(suites_copy)
    v = store.add_version("followup-email", "New prompt text", "Try something")
    assert v.id == "v5" and v.number == 5
    assert store.add_version("followup-email", "Another", "Again").id == "v6"


def test_versions_are_never_overwritten(suites_copy):
    store = Store(suites_copy)
    before = (suites_copy / "followup-email/versions/v4.md").read_text()
    store.add_version("followup-email", "x", "y")
    assert (suites_copy / "followup-email/versions/v4.md").read_text() == before


def test_version_numbering_skips_gaps_safely(suites_copy):
    store = Store(suites_copy)
    (suites_copy / "partner-outreach/versions/v7.md").write_text("---\nnote: n\ncreated: 2026-01-01\n---\nText\n")
    assert store.add_version("partner-outreach", "t", "n").id == "v8"


def test_version_front_matter_is_read(demo_store):
    v = demo_store.load_version("followup-email", "v3")
    assert v.title == "Under 120 words"
    assert v.note.startswith("Recaps run long.")
    assert "Keep it under 120 words. Busy buyers skim." in v.text
    assert not v.text.startswith("---")


def test_missing_rubric_field_gives_a_helpful_error(suites_copy):
    path = suites_copy / "partner-outreach/suite.yaml"
    path.write_text(path.read_text().replace("    one_looks_like: Reads like a mass email or a brochure, full of buzzwords.\n", ""))
    with pytest.raises(SuiteError) as err:
        Store(suites_copy).load_suite("partner-outreach")
    msg = str(err.value)
    assert "suite.yaml" in msg and "rubric.3.one_looks_like" in msg and "Field required" in msg


def test_bad_check_value_gives_a_helpful_error(suites_copy):
    path = suites_copy / "partner-outreach/suite.yaml"
    path.write_text(path.read_text().replace("value: 140", "value: lots"))
    with pytest.raises(SuiteError) as err:
        Store(suites_copy).load_suite("partner-outreach")
    assert "suite.yaml has a problem" in str(err.value) and "checks.0.value" in str(err.value)


def test_invalid_yaml_gives_a_helpful_error(suites_copy):
    (suites_copy / "partner-outreach/cases.yaml").write_text("- id: [unclosed\n")
    with pytest.raises(SuiteError, match="not valid YAML"):
        Store(suites_copy).load_cases("partner-outreach")


def test_case_missing_input_names_the_case(suites_copy):
    (suites_copy / "partner-outreach/cases.yaml").write_text("- id: a\n  name: A\n  input: hi\n- id: b\n  name: B\n")
    with pytest.raises(SuiteError, match="example 2.*input"):
        Store(suites_copy).load_cases("partner-outreach")


def test_duplicate_case_ids_are_rejected(suites_copy):
    (suites_copy / "partner-outreach/cases.yaml").write_text("- {id: a, name: A, input: x}\n- {id: a, name: B, input: y}\n")
    with pytest.raises(SuiteError, match="repeated: a"):
        Store(suites_copy).load_cases("partner-outreach")


def test_unknown_suite_or_version(demo_store):
    with pytest.raises(SuiteError, match="no prompt called"):
        demo_store.load_suite("../etc")
    with pytest.raises(SuiteError, match="no version"):
        demo_store.load_version("followup-email", "v99")


def test_em_dash_check_is_stored_as_the_real_character(demo_store):
    suite = demo_store.load_suite("followup-email")
    assert suite.checks[1].value == [chr(0x2014)]


def test_add_and_create(suites_copy):
    store = Store(suites_copy)
    case = store.add_case("partner-outreach", "New hard one", "Acme Test Co", "Some notes", "messy")
    assert case.id == "acme-test-co-new-hard-one" and case.kind == "messy"
    assert store.load_cases("partner-outreach")[-1] == case
    suite = Suite(name="Renewal risk summary", rubric=[Criterion(id="risk", name="Risk", five_looks_like="a", one_looks_like="b")])
    sid = store.create_suite(suite, [Case(id="c1", name="One", input="x")], "Prompt text")
    assert sid == "renewal-risk-summary"
    assert store.load_version(sid, "v1").text == "Prompt text"
    assert store.create_suite(suite, [Case(id="c1", name="One", input="x")], "Prompt") == "renewal-risk-summary-2"


def test_examples_default_to_everyday_and_reject_unknown_kinds(suites_copy):
    (suites_copy / "partner-outreach/cases.yaml").write_text("- {id: a, name: A, input: x}\n- {id: b, name: B, kind: odd, input: y}\n")
    with pytest.raises(SuiteError, match="example 2.*kind"):
        Store(suites_copy).load_cases("partner-outreach")
    assert Case(id="a", name="A", input="x").kind == "everyday"


def test_update_case_keeps_the_kind(suites_copy):
    store = Store(suites_copy)
    store.update_case("partner-outreach", "harlow-cold", "Cold", "Harlow Analytics", "New notes", "complicated")
    assert next(c for c in store.load_cases("partner-outreach") if c.id == "harlow-cold").kind == "complicated"


def test_marking_a_deal_breaker_makes_saved_reviews_stale(demo_store):
    """Live reuse goes by the review key, which includes deal-breaker flags (spec section 5)."""
    from rollout_ready.evidence import review_key, text_hash
    suite = demo_store.load_suite("followup-email")
    flipped = suite.model_copy(update={"rubric": [c.model_copy(update={"deal_breaker": not c.deal_breaker}) for c in suite.rubric]})
    key = dict(answer_hash=text_hash("An answer."), reviewer_model="claude-sonnet-5-5")
    assert review_key(rubric=flipped.rubric, **key) != review_key(rubric=suite.rubric, **key)
    # rubric_hash only checks older and recorded results, and still ignores the flag there.
    assert rubric_hash(flipped) == rubric_hash(suite)


def test_overrides_are_saved_as_events_by_evidence_fingerprint(suites_copy):
    from rollout_ready.evaluate import active_override_events, override_event
    store = Store(suites_copy)
    fp_a, fp_b = "a" * 64, "b" * 64
    store.append_override_event("followup-email", override_event(
        fp_a, "eval-1", "ridgeway-pricing", call="not_worse", result="same", reason="The pricing answer is in the PS."))
    store.append_override_event("followup-email", override_event(
        fp_b, "eval-2", "ridgeway-pricing", call="not_worse", result="same", reason="Other evidence."))
    assert (suites_copy / "followup-email/runs/override-events.json").exists()
    active = active_override_events(store.override_events("followup-email"), fp_a)
    assert list(active) == ["ridgeway-pricing"] and active["ridgeway-pricing"].reason == "The pricing answer is in the PS."
    assert active_override_events(store.override_events("followup-email"), "c" * 64) == {}
    store.append_override_event("followup-email", override_event(fp_a, "eval-1", "ridgeway-pricing", remove=True))
    assert active_override_events(store.override_events("followup-email"), fp_a) == {}
    assert list(active_override_events(store.override_events("followup-email"), fp_b)) == ["ridgeway-pricing"]
    assert [e.action for e in store.override_events("followup-email")] == ["set", "set", "remove"]
