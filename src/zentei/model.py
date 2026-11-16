"""Records and events, as defined in docs/01_Data_Model_Decision.md."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

GLOBAL = "global"

PremiseId = Annotated[str, StringConstraints(pattern=r"^pre_[a-z0-9]+(-[a-z0-9]+)*$")]
DecisionId = Annotated[str, StringConstraints(pattern=r"^dec_[a-z0-9]+(-[a-z0-9]+)*$")]
RecordId = Annotated[str, StringConstraints(pattern=r"^(pre|dec)_[a-z0-9]+(-[a-z0-9]+)*$")]
Scope = Annotated[str, StringConstraints(pattern=r"^(global|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

Kind = Literal["fact", "measurement", "situation", "expectation", "value"]
CheckResult = Literal["holds", "broken"]
PremiseStatus = Literal["holds", "broken", "unverified"]
DecisionStatus = Literal["active", "superseded", "withdrawn"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Premise(Record):
    id: PremiseId
    statement: Text
    breaks_when: Text | None = None
    kind: Kind
    scope: Scope
    as_of: date | None = None
    evidence: Text | None = None


class Alternative(Record):
    option: Text
    reason: Text


class Dependency(Record):
    on: RecordId
    role: Literal["supports", "rules_out"] = "supports"
    alternative: Text | None = None
    strength: Literal["critical", "contributing"] = "contributing"

    @model_validator(mode="after")
    def _alternative_matches_role(self) -> Dependency:
        if (self.role == "rules_out") != (self.alternative is not None):
            raise ValueError("alternative is required for rules_out, and only for rules_out")
        return self


class Source(Record):
    kind: Literal["session", "doc", "memory", "issue", "pr"]
    ref: Text


class Decision(Record):
    id: DecisionId
    scope: Scope
    title: Text
    chosen: Text
    alternatives: list[Alternative] = []
    decided_on: date | None = None
    sources: list[Source] = []
    depends_on: list[Dependency] = []
    review_on: date | None = None

    @model_validator(mode="after")
    def _dependencies_are_consistent(self) -> Decision:
        options = {a.option for a in self.alternatives}
        for dep in self.depends_on:
            if dep.on == self.id:
                raise ValueError("a decision cannot depend on itself")
            if dep.alternative is not None and dep.alternative not in options:
                raise ValueError(f"rules_out names an unknown alternative: {dep.alternative!r}")
        return self


class Origin(Record):
    kind: Literal["session", "doc", "memory", "manual"]
    ref: Text | None = None
    speaker: Literal["user", "assistant"] | None = None
    extractor: Text | None = None


class PremiseChecked(Record):
    premise: PremiseId
    result: CheckResult
    as_of: date
    evidence: Text | None = None


class DecisionRevised(Record):
    decision: DecisionId
    patch: dict[str, Any]

    @model_validator(mode="after")
    def _patch_is_allowed(self) -> DecisionRevised:
        fixed = {"id"} & self.patch.keys()
        unknown = self.patch.keys() - Decision.model_fields.keys()
        if fixed or unknown or not self.patch:
            raise ValueError(f"patch must change known fields other than id: {sorted(fixed | unknown)}")
        return self


class DecisionClosed(Record):
    decision: DecisionId
    state: Literal["superseded", "withdrawn"]
    superseded_by: DecisionId | None = None

    @model_validator(mode="after")
    def _successor_matches_state(self) -> DecisionClosed:
        if (self.state == "superseded") != (self.superseded_by is not None):
            raise ValueError("superseded_by is required for superseded, and only for superseded")
        return self


class Merged(Record):
    source: RecordId
    into: RecordId

    @model_validator(mode="after")
    def _same_kind(self) -> Merged:
        if self.source == self.into or self.source[:4] != self.into[:4]:
            raise ValueError("merge needs two different records of the same kind")
        return self


class Retracted(Record):
    id: RecordId
    reason: Text | None = None


PAYLOADS: dict[str, type[Record]] = {
    "premise.added": Premise,
    "decision.added": Decision,
    "premise.checked": PremiseChecked,
    "decision.revised": DecisionRevised,
    "decision.closed": DecisionClosed,
    "merged": Merged,
    "retracted": Retracted,
}


class Event(Record):
    id: Annotated[str, StringConstraints(pattern=r"^evt_[0-9a-f]{32}$")]
    at: datetime
    machine: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+$")]
    type: str
    origin: Origin = Field(default_factory=lambda: Origin(kind="manual"))
    data: dict[str, Any]

    @model_validator(mode="after")
    def _data_matches_type(self) -> Event:
        if self.type not in PAYLOADS:
            raise ValueError(f"unknown event type: {self.type}")
        if self.at.tzinfo is None:
            raise ValueError("at must carry a timezone")
        self.payload()
        return self

    def payload(self) -> Record:
        return PAYLOADS[self.type].model_validate(self.data)
