"""Data shapes for suite files and saved results."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    five_looks_like: str = Field(min_length=1)
    one_looks_like: str = Field(min_length=1)


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


class Suite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = ""
    name: str = Field(min_length=1)
    description: str = ""
    who_uses_it: str = ""
    input_label: str = "What someone on the team would paste in"
    in_use: str = ""  # the version the team uses now; defaults to the newest
    model: str = "claude-sonnet-5-5"
    repeats: int = Field(default=1, ge=1, le=5)
    threshold: float = Field(default=0.5, gt=0, le=4)
    rubric: list[Criterion] = Field(min_length=1)
    checks: list[Check] = []

    @field_validator("rubric")
    @classmethod
    def _unique_ids(cls, rubric: list[Criterion]) -> list[Criterion]:
        ids = [c.id for c in rubric]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError("each rubric criterion needs its own id; repeated: " + ", ".join(dupes))
        return rubric


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    label: str = ""
    input: str = Field(min_length=1)


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
