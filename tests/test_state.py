import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from zentei import cli, store
from zentei.model import Decision, Event
from zentei.state import fold, impact, in_scope

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def ev(n: int, type: str, data: dict, machine: str = "a") -> Event:
    return Event(id=f"evt_{n:032x}", at=T0 + timedelta(minutes=n), machine=machine, type=type, data=data)


def premise(pid: str, **extra) -> dict:
    return {"id": pid, "statement": "s", "kind": "fact", "scope": "global", "as_of": "2026-01-01"} | extra


def decision(did: str, deps: list[dict], alternatives: list[dict] | None = None, scope: str = "o/r") -> dict:
    return {
        "id": did,
        "scope": scope,
        "title": "t",
        "chosen": "c",
        "depends_on": deps,
        "alternatives": alternatives or [],
    }


BASE = [
    ev(1, "premise.added", premise("pre_a")),
    ev(2, "premise.added", premise("pre_b")),
    ev(3, "premise.added", premise("pre_c", as_of=None)),
    ev(
        4,
        "decision.added",
        decision(
            "dec_x",
            [
                {"on": "pre_a", "strength": "critical"},
                {"on": "pre_b"},
                {"on": "pre_c", "role": "rules_out", "alternative": "other"},
            ],
            [{"option": "other", "reason": "r"}],
        ),
    ),
    ev(5, "decision.added", decision("dec_y", [{"on": "dec_x", "strength": "critical"}])),
]


def broken(n: int, pid: str) -> Event:
    return ev(n, "premise.checked", {"premise": pid, "result": "broken", "as_of": "2026-02-01"})


def by_id(findings):
    return {f.decision.id: f for f in findings}


def test_status_before_any_check_follows_as_of():
    state = fold(BASE)
    assert state.premises["pre_a"].status == "holds"
    assert state.premises["pre_c"].status == "unverified"
    assert impact(state) == []


def test_critical_break_propagates_to_dependent_decisions():
    found = by_id(impact(fold([*BASE, broken(6, "pre_a")])))
    assert found["dec_x"].needs_review == ["pre_a"]
    assert found["dec_y"].needs_review == ["dec_x"]


def test_contributing_break_only_weakens():
    found = by_id(impact(fold([*BASE, broken(6, "pre_b")])))
    assert found["dec_x"].weakened == ["pre_b"] and not found["dec_x"].needs_review
    assert "dec_y" not in found


def test_broken_reason_for_rejection_revives_the_alternative():
    found = by_id(impact(fold([*BASE, broken(6, "pre_c")])))
    assert found["dec_x"].revivable == {"other": ["pre_c"]}
    assert not found["dec_x"].needs_review


def test_assume_broken_does_not_need_an_event():
    assert by_id(impact(fold(BASE), ["pre_a"]))["dec_y"].needs_review == ["dec_x"]


def test_check_holds_restores_and_updates_as_of():
    restored = ev(7, "premise.checked", {"premise": "pre_a", "result": "holds", "as_of": "2026-03-01"})
    state = fold([*BASE, broken(6, "pre_a"), restored])
    assert impact(state) == []
    assert state.premises["pre_a"].premise.as_of.isoformat() == "2026-03-01"


def test_merge_redirects_dependencies():
    events = [*BASE, ev(6, "premise.added", premise("pre_a2")), ev(7, "merged", {"source": "pre_a", "into": "pre_a2"})]
    state = fold(events)
    assert "pre_a" not in state.premises
    assert by_id(impact(state, ["pre_a2"]))["dec_x"].needs_review == ["pre_a2"]
    assert by_id(impact(fold([*events, broken(8, "pre_a")])))["dec_x"].needs_review == ["pre_a2"]


def test_retracted_premise_prompts_nothing_and_its_id_stays_taken():
    state = fold([*BASE, ev(6, "retracted", {"id": "pre_a"}), ev(7, "premise.added", premise("pre_a"))])
    assert "pre_a" not in state.premises
    assert len(state.warnings) == 1
    assert impact(state) == []


def test_closed_decision_puts_dependents_up_for_review():
    events = [
        *BASE,
        ev(6, "decision.added", decision("dec_z", [])),
        ev(7, "decision.closed", {"decision": "dec_x", "state": "superseded", "superseded_by": "dec_z"}),
    ]
    found = by_id(impact(fold(events)))
    assert "dec_x" not in found
    assert found["dec_y"].needs_review == ["dec_x"]


def test_revision_replaces_fields_and_is_validated():
    ok = ev(6, "decision.revised", {"decision": "dec_x", "patch": {"depends_on": [{"on": "pre_b"}]}})
    bad = ev(7, "decision.revised", {"decision": "dec_x", "patch": {"depends_on": [{"on": "pre_missing"}]}})
    state = fold([*BASE, ok, bad])
    assert [d.on for d in state.decisions["dec_x"].decision.depends_on] == ["pre_b"]
    assert len(state.warnings) == 1


def test_duplicate_id_from_another_machine_keeps_the_first():
    state = fold([*BASE, ev(6, "premise.added", premise("pre_a", statement="later"), machine="b")])
    assert state.premises["pre_a"].premise.statement == "s"
    assert len(state.warnings) == 1


def test_order_is_by_time_not_by_input_order():
    assert fold(list(reversed(BASE))).warnings == []


def test_in_scope_includes_only_the_global_premises_in_use():
    state = fold([*BASE, ev(6, "premise.added", premise("pre_unused"))])
    premises, decisions = in_scope(state, "o/r")
    assert {ps.premise.id for ps in premises} == {"pre_a", "pre_b", "pre_c"}
    assert {ds.decision.id for ds in decisions} == {"dec_x", "dec_y"}


@pytest.mark.parametrize(
    "deps",
    [
        [{"on": "pre_a", "role": "rules_out"}],
        [{"on": "pre_a", "alternative": "other"}],
        [{"on": "pre_a", "role": "rules_out", "alternative": "not listed"}],
        [{"on": "dec_x"}],
    ],
)
def test_inconsistent_dependencies_are_rejected(deps):
    with pytest.raises(ValidationError):
        Decision.model_validate(decision("dec_x", deps, [{"option": "other", "reason": "r"}]))


def test_event_rejects_unknown_fields_and_types():
    with pytest.raises(ValidationError):
        ev(1, "premise.added", premise("pre_a", confidence=0.8))
    with pytest.raises(ValidationError):
        ev(1, "premise.removed", {"id": "pre_a"})


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("ZENTEI_DATA", str(tmp_path))
    monkeypatch.setenv("ZENTEI_MACHINE", "test host")
    return tmp_path


def write(path, items):
    path.write_text(json.dumps(items), encoding="utf-8")
    return str(path)


def test_apply_writes_per_machine_and_reads_back(data, capsys):
    items = [{"type": e.type, "data": e.data} for e in BASE]
    assert cli.main(["apply", write(data / "in.json", items)]) == 0
    files = list(data.glob("events/*/*.jsonl"))
    assert [f.parent.name for f in files] == ["test-host"]
    assert len(store.read_events(data)) == len(BASE)
    assert cli.main(["check", "pre_a", "broken"]) == 0
    assert "dec_y" in capsys.readouterr().out
    assert cli.main(["context", "--scope", "o/r"]) == 0
    assert "pre_a" in capsys.readouterr().out


def test_apply_writes_nothing_when_any_item_is_invalid(data, capsys):
    items = [
        {"type": "premise.added", "data": premise("pre_a")},
        {"type": "decision.added", "data": decision("dec_x", [{"on": "pre_missing"}])},
    ]
    assert cli.main(["apply", write(data / "in.json", items)]) == 1
    assert "pre_missing" in capsys.readouterr().err
    assert store.read_events(data) == []


def test_apply_dry_run_writes_nothing(data):
    assert (
        cli.main(["apply", "--dry-run", write(data / "in.json", [{"type": "premise.added", "data": premise("pre_a")}])])
        == 0
    )
    assert store.read_events(data) == []
