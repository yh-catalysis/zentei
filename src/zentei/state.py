"""Fold events into the current state, and ask what a broken premise affects."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from pydantic import ValidationError

from zentei.model import (
    GLOBAL,
    Decision,
    DecisionClosed,
    DecisionRevised,
    DecisionStatus,
    Dependency,
    Event,
    Merged,
    Premise,
    PremiseChecked,
    PremiseStatus,
    Retracted,
)


@dataclass
class PremiseState:
    premise: Premise
    status: PremiseStatus
    evidence: str | None = None


@dataclass
class DecisionState:
    decision: Decision
    status: DecisionStatus = "active"
    superseded_by: str | None = None


@dataclass
class State:
    premises: dict[str, PremiseState] = field(default_factory=dict)
    decisions: dict[str, DecisionState] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    retracted: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)

    def resolve(self, record_id: str) -> str:
        seen = set()
        while record_id in self.aliases and record_id not in seen:
            seen.add(record_id)
            record_id = self.aliases[record_id]
        return record_id

    def has(self, record_id: str) -> bool:
        record_id = self.resolve(record_id)
        return record_id in self.premises or record_id in self.decisions

    def taken(self, record_id: str) -> bool:
        return record_id in self.aliases or record_id in self.retracted or self.has(record_id)

    def dependencies(self, decision: Decision) -> list[Dependency]:
        return [dep.model_copy(update={"on": self.resolve(dep.on)}) for dep in decision.depends_on]


def problem(state: State, event: Event) -> str | None:
    """Why this event cannot be applied to this state, or None if it can."""
    p = event.payload()
    match p:
        case Premise() | Decision():
            if state.taken(p.id):
                return f"{p.id} already exists"
            if isinstance(p, Decision):
                missing = [d.on for d in p.depends_on if not state.has(d.on)]
                if missing:
                    return f"{p.id} depends on unknown records: {missing}"
        case PremiseChecked():
            if state.resolve(p.premise) not in state.premises:
                return f"unknown premise: {p.premise}"
        case DecisionRevised():
            target = state.decisions.get(state.resolve(p.decision))
            if target is None:
                return f"unknown decision: {p.decision}"
            try:
                revised = Decision.model_validate(target.decision.model_dump() | p.patch)
            except ValidationError as e:
                return f"revision of {p.decision} is invalid: {e.errors()[0]['msg']}"
            missing = [d.on for d in revised.depends_on if not state.has(d.on)]
            if missing:
                return f"{p.decision} would depend on unknown records: {missing}"
        case DecisionClosed():
            if state.resolve(p.decision) not in state.decisions:
                return f"unknown decision: {p.decision}"
            if p.superseded_by and state.resolve(p.superseded_by) not in state.decisions:
                return f"unknown decision: {p.superseded_by}"
        case Merged():
            if not (state.has(p.source) and state.has(p.into)):
                return f"merge needs two existing records: {p.source}, {p.into}"
            if state.resolve(p.source) == state.resolve(p.into):
                return f"{p.source} is already merged into {p.into}"
        case Retracted():
            if not state.has(p.id):
                return f"unknown record: {p.id}"
    return None


def apply(state: State, event: Event) -> None:
    p = event.payload()
    match p:
        case Premise():
            verified = p.as_of is not None or p.kind == "value"
            state.premises[p.id] = PremiseState(p, "holds" if verified else "unverified", p.evidence)
        case Decision():
            state.decisions[p.id] = DecisionState(p)
        case PremiseChecked():
            target = state.premises[state.resolve(p.premise)]
            target.status = p.result
            target.evidence = p.evidence or target.evidence
            if p.result == "holds":
                target.premise = target.premise.model_copy(update={"as_of": p.as_of})
        case DecisionRevised():
            target = state.decisions[state.resolve(p.decision)]
            target.decision = Decision.model_validate(target.decision.model_dump() | p.patch)
        case DecisionClosed():
            target = state.decisions[state.resolve(p.decision)]
            target.status = p.state
            target.superseded_by = state.resolve(p.superseded_by) if p.superseded_by else None
        case Merged():
            source, into = state.resolve(p.source), state.resolve(p.into)
            state.premises.pop(source, None)
            state.decisions.pop(source, None)
            state.aliases[source] = into
        case Retracted():
            record_id = state.resolve(p.id)
            state.premises.pop(record_id, None)
            state.decisions.pop(record_id, None)
            state.retracted.add(record_id)


def fold(events: Iterable[Event]) -> State:
    """Events that cannot be applied are skipped and reported, so one bad line never hides the rest."""
    state = State()
    for event in sorted(events, key=lambda e: (e.at, e.machine, e.id)):
        reason = problem(state, event)
        if reason:
            state.warnings.append(f"{event.id} ({event.type}): {reason}")
        else:
            apply(state, event)
    return state


@dataclass
class Finding:
    decision: Decision
    needs_review: list[str] = field(default_factory=list)
    weakened: list[str] = field(default_factory=list)
    revivable: dict[str, list[str]] = field(default_factory=dict)


def impact(state: State, assume_broken: Iterable[str] = ()) -> list[Finding]:
    """What the broken premises affect among the active decisions.

    A record no longer usable as a basis is a broken premise, a closed decision,
    or a decision that itself needs review. A retracted record was never a basis,
    so a dependency on it is ignored rather than reported.
    """
    unusable = {pid for pid, ps in state.premises.items() if ps.status == "broken"}
    unusable |= {state.resolve(i) for i in assume_broken}
    unusable |= {did for did, ds in state.decisions.items() if ds.status != "active"}

    active = {did: ds.decision for did, ds in state.decisions.items() if ds.status == "active"}
    findings = {did: Finding(d) for did, d in active.items()}

    changed = True
    while changed:
        changed = False
        for did, decision in active.items():
            f = findings[did]
            for dep in state.dependencies(decision):
                if dep.on not in unusable:
                    continue
                if dep.role == "rules_out":
                    bucket = f.revivable.setdefault(dep.alternative, [])
                elif dep.strength == "critical":
                    bucket = f.needs_review
                else:
                    bucket = f.weakened
                if dep.on not in bucket:
                    bucket.append(dep.on)
            if f.needs_review and did not in unusable:
                unusable.add(did)
                changed = True

    return [f for f in findings.values() if f.needs_review or f.weakened or f.revivable]


def in_scope(state: State, scope: str) -> tuple[list[PremiseState], list[DecisionState]]:
    """Records of a repository, plus the global premises its decisions rest on."""
    decisions = [ds for ds in state.decisions.values() if ds.decision.scope == scope]
    used = {dep.on for ds in decisions for dep in state.dependencies(ds.decision)}
    premises = [
        ps
        for pid, ps in state.premises.items()
        if ps.premise.scope == scope or (ps.premise.scope == GLOBAL and (pid in used or scope == GLOBAL))
    ]
    return premises, decisions
