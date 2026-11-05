"""Data shapes for suite files and saved results."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    five_looks_like: str = Field(min_length=1)
    one_looks_like: str = Field(min_length=1)
    deal_breaker: bool = False  # something the output must never get wrong


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["max_words", "must_not_contain", "must_contain_any"]
    value: int | list[str]
    label: str = ""

    @model_validator(mode="after")
    def _value_matches_type(self) -> "Check":
        if self.type == "max_words":
            if not isinstance(self.value, int) or self.value < 1:
                raise ValueError("a max_words check needs a whole number above 0 as its value")
        else:
            if not isinstance(self.value, list) or not self.value:
                raise ValueError(f"a {self.type} check needs a list of words or phrases as its value")
        if not self.label:
            self.label = default_check_label(self)
        return self


def default_check_label(check: Check) -> str:
    if check.type == "max_words":
        return f"Under {check.value} words"
    if check.type == "must_not_contain":
        return "Does not contain: " + ", ".join(_show(v) for v in check.value)
    return "Mentions one of: " + ", ".join(check.value)


def _show(phrase: str) -> str:
    return "em dashes" if phrase == "\u2014" else phrase


InstructionType = Literal["prompt", "project", "gpt", "skill", "agent"]

# What each type is called, and what a test covers and doesn't (spec section 3).
TYPE_LABELS: dict[str, str] = {
    "prompt": "Prompt or snippet",
    "project": "Claude Project",
    "gpt": "GPT or Gem instructions",
    "skill": "Skill",
    "agent": "Agent instructions",
}
SCOPE_NOTES: dict[str, tuple[str, str]] = {
    "prompt": ("The prompt as your team uses it, on each saved example.",
               "Inputs you haven't saved as examples. Real use may differ."),
    "project": ("The Project's instructions, plus any key facts pasted in with them.",
                "The Project's uploaded files. Paste the facts it relies on into the instructions instead."),
    "gpt": ("The instructions, run on Claude.",
            "How it behaves on the other company's model, which can differ."),
    "skill": ("The skill's written instructions, on each saved example.",
              "When the skill gets picked up, and any files or scripts bundled with it."),
    "agent": ("The agent's instructions and the replies it writes.",
              "Its tools and actions, like sending email or updating records."),
}


class Suite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = ""
    name: str = Field(min_length=1)
    description: str = ""
    type: InstructionType = "prompt"
    team: str = ""  # Sales, Marketing, Partnerships ...
    who_uses_it: str = ""
    owner_name: str = ""  # self-reported, not verified
    owner_team: str = ""
    where_text: str = ""  # where the team uses it, in words
    where_url: str = ""  # optional link to it
    input_label: str = "What someone on the team would paste in"
    in_use: str = ""  # only for sets with no release history (before V3); otherwise derived from history
    model: str = "claude-sonnet-5-5"
    repeats: int = Field(default=1, ge=1, le=5)
    threshold: float = Field(default=0.5, gt=0, le=4)
    rubric: list[Criterion] = Field(min_length=1)
    checks: list[Check] = []

    @field_validator("where_url")
    @classmethod
    def _web_address(cls, url: str) -> str:
        url = url.strip()
        if url and not re.fullmatch(r"https?://[^\s/$.?#][^\s]*", url):
            raise ValueError("where_url should be a full web address starting with https://, or left empty")
        return url

    @property
    def type_label(self) -> str:
        return TYPE_LABELS[self.type]

    @property
    def scope_line(self) -> str:
        """Shown under every result: what was tested and what wasn't."""
        return SCOPE_NOTES[self.type][1]

    @field_validator("rubric")
    @classmethod
    def _unique_ids(cls, rubric: list[Criterion]) -> list[Criterion]:
        ids = [c.id for c in rubric]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError("each quality in what good looks like needs its own id; repeated: " + ", ".join(dupes))
        return rubric


CaseKind = Literal["everyday", "messy", "complicated", "broke_before"]

# What each kind is called in the app, and a one-line hint for the Add example form.
CASE_KINDS: dict[str, tuple[str, str]] = {
    "everyday": ("Everyday", "A typical input, the kind the team pastes in most days."),
    "messy": ("Messy or incomplete", "Rough notes, missing details, or things out of order."),
    "complicated": ("Complicated", "Several people, several asks, or details that are easy to mix up."),
    "broke_before": ("Broke before", "An input where a real output went wrong in the past."),
}


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    label: str = ""
    kind: CaseKind = "everyday"
    input: str = Field(min_length=1)

    @property
    def kind_label(self) -> str:
        return CASE_KINDS[self.kind][0]


class Version(BaseModel):
    id: str  # "v3"
    number: int
    title: str = ""  # short name, optional; new versions from the browser have only a note
    note: str
    created: str
    text: str

    @property
    def full_name(self) -> str:
        return f"Version {self.number}"

    @property
    def summary(self) -> str:
        """What the version select shows after the id."""
        return self.title or self.note


class Score(BaseModel):
    score: int = Field(ge=1, le=5)
    reason: str


class Run(BaseModel):
    """One output for one case, and the judge's scores for it."""

    output: str
    scores: dict[str, Score]
    input_tokens: int = 0
    output_tokens: int = 0
    judge_input_tokens: int = 0
    judge_output_tokens: int = 0


class CaseResult(BaseModel):
    model: str = ""
    rubric_hash: str = ""
    input_hash: str = ""
    runs: list[Run]


class VersionResults(BaseModel):
    version: str
    model: str = ""
    judge_model: str = ""
    ran_at: str = ""
    source: Literal["recorded", "live"] = "live"
    cases: dict[str, CaseResult] = {}


class Override(BaseModel):
    """A person's call on one example for one pair of versions. Never changes stored scores."""

    call: Literal["not_worse", "not_better", "other"]
    result: Literal["worse", "better", "same"]
    reason: str = Field(min_length=1, max_length=200)
    created: str = ""
