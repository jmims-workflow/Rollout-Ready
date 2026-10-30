import pytest

from rollout_ready.models import Case, Criterion, Suite
from rollout_ready.storage import Store, SuiteError


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
    (suites_copy / "ticket-summary/versions/v7.md").write_text("---\nnote: n\ncreated: 2026-01-01\n---\nText\n")
    assert store.add_version("ticket-summary", "t", "n").id == "v8"


def test_version_front_matter_is_read(demo_store):
    v = demo_store.load_version("followup-email", "v3")
    assert v.title == "Under 120 words"
    assert v.note.startswith("Recaps run long.")
    assert "Keep it under 120 words. Busy buyers skim." in v.text
    assert not v.text.startswith("---")


def test_missing_rubric_field_gives_a_helpful_error(suites_copy):
    path = suites_copy / "ticket-summary/suite.yaml"
    path.write_text(path.read_text().replace("    one_looks_like: No steps, or steps an engineer could not follow.\n", ""))
    with pytest.raises(SuiteError) as err:
        Store(suites_copy).load_suite("ticket-summary")
    msg = str(err.value)
    assert "suite.yaml" in msg and "rubric.1.one_looks_like" in msg and "Field required" in msg


def test_bad_check_value_gives_a_helpful_error(suites_copy):
    path = suites_copy / "ticket-summary/suite.yaml"
    path.write_text(path.read_text().replace("value: 120", "value: lots"))
    with pytest.raises(SuiteError) as err:
        Store(suites_copy).load_suite("ticket-summary")
    assert "suite.yaml has a problem" in str(err.value) and "checks.0.value" in str(err.value)


def test_invalid_yaml_gives_a_helpful_error(suites_copy):
    (suites_copy / "ticket-summary/cases.yaml").write_text("- id: [unclosed\n")
    with pytest.raises(SuiteError, match="not valid YAML"):
        Store(suites_copy).load_cases("ticket-summary")


def test_case_missing_input_names_the_case(suites_copy):
    (suites_copy / "ticket-summary/cases.yaml").write_text("- id: a\n  name: A\n  input: hi\n- id: b\n  name: B\n")
    with pytest.raises(SuiteError, match="test case 2.*input"):
        Store(suites_copy).load_cases("ticket-summary")


def test_duplicate_case_ids_are_rejected(suites_copy):
    (suites_copy / "ticket-summary/cases.yaml").write_text("- {id: a, name: A, input: x}\n- {id: a, name: B, input: y}\n")
    with pytest.raises(SuiteError, match="repeated: a"):
        Store(suites_copy).load_cases("ticket-summary")


def test_unknown_suite_or_version(demo_store):
    with pytest.raises(SuiteError, match="no prompt suite"):
        demo_store.load_suite("../etc")
    with pytest.raises(SuiteError, match="no version"):
        demo_store.load_version("followup-email", "v99")


def test_em_dash_check_is_stored_as_the_real_character(demo_store):
    suite = demo_store.load_suite("followup-email")
    assert suite.checks[1].value == [chr(0x2014)]


def test_add_and_create(suites_copy):
    store = Store(suites_copy)
    case = store.add_case("ticket-summary", "New hard one", "Acme Test Co", "Some thread")
    assert case.id == "acme-test-co-new-hard-one"
    assert store.load_cases("ticket-summary")[-1] == case
    suite = Suite(name="Renewal risk summary", rubric=[Criterion(id="risk", name="Risk", five_looks_like="a", one_looks_like="b")])
    sid = store.create_suite(suite, [Case(id="c1", name="One", input="x")], "Prompt text")
    assert sid == "renewal-risk-summary"
    assert store.load_version(sid, "v1").text == "Prompt text"
    assert store.create_suite(suite, [Case(id="c1", name="One", input="x")], "Prompt") == "renewal-risk-summary-2"
