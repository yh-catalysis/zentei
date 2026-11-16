from datetime import date

from zentei import harvest as hv
from zentei.state import fold


def output(dep_on="pre_r-a", alternative=None):
    return {
        "premises": [
            {
                "id": "pre_r-a",
                "statement": "s",
                "breaks_when": None,
                "kind": "fact",
                "is_global": False,
                "verified": True,
                "stated_by_user": True,
            },
            {
                "id": "pre_b",
                "statement": "s",
                "breaks_when": "x",
                "kind": "fact",
                "is_global": True,
                "verified": False,
                "stated_by_user": False,
            },
        ],
        "decisions": [
            {
                "id": "dec_r-x",
                "title": "t",
                "chosen": "c",
                "alternatives": [],
                "decided_on": None,
                "section": "3",
                "depends_on": [{"on": dep_on, "role": "supports", "alternative": alternative, "strength": "critical"}],
            },
        ],
        "questions": [{"about": "pre_r-a", "question": "q"}, {"about": "pre_unknown", "question": "q"}],
    }


def source(tmp_path):
    path = tmp_path / "doc.md"
    path.write_text("# doc", encoding="utf-8")
    return hv.Source(path, "o/r", "doc", "o/r:doc.md", date(2026, 1, 2))


def test_output_becomes_events_with_dates_scope_and_origin(tmp_path):
    result = hv.to_events(output(), source(tmp_path), fold([]), "m")
    assert result.errors == []
    a, b, x = (e.data for e in result.events)
    assert (a["scope"], a["as_of"], b["scope"], "as_of" in b) == ("o/r", "2026-01-02", "global", False)
    assert (x["decided_on"], x["sources"]) == ("2026-01-02", [{"kind": "doc", "ref": "o/r:doc.md#3"}])
    assert result.events[0].origin.speaker == "user" and result.events[1].origin.speaker is None
    assert [q["about"] for q in result.questions] == ["pre_r-a"]


def test_the_only_stated_reason_is_critical(tmp_path):
    out = output()
    out["decisions"][0]["depends_on"][0]["strength"] = "contributing"
    result = hv.to_events(out, source(tmp_path), fold([]), "m")
    assert result.events[2].data["depends_on"][0]["strength"] == "critical"


def test_errors_are_fed_back_and_the_whole_output_is_retried(tmp_path):
    prompts = []

    def run(prompt):
        prompts.append(prompt)
        return output(dep_on="pre_missing") if len(prompts) == 1 else output()

    result = hv.extract(source(tmp_path), fold([]), run, "m")
    assert result.errors == [] and len(result.events) == 3
    assert "pre_missing" in prompts[1] and "pre_missing" not in prompts[0]


def test_existing_records_are_offered_and_can_be_referenced(tmp_path):
    src = source(tmp_path)
    state = fold(hv.to_events(output(), src, fold([]), "m").events)
    assert "pre_r-a: s" in hv.build_prompt(src, state, [])
    again = {"premises": [], "questions": [], "decisions": [output()["decisions"][0] | {"id": "dec_r-y"}]}
    assert hv.to_events(again, src, state, "m").errors == []
    assert hv.to_events(output(), src, state, "m").errors


def test_ledger_skips_unchanged_files(tmp_path, monkeypatch):
    monkeypatch.setenv("ZENTEI_MACHINE", "m")
    src = source(tmp_path)
    ledger = hv.Ledger(tmp_path / "data")
    assert ledger.is_new(src)
    ledger.record(src, [{"ref": src.ref, "about": "pre_r-a", "question": "q"}])
    assert not hv.Ledger(tmp_path / "data").is_new(src)
    src.path.write_text("changed", encoding="utf-8")
    assert hv.Ledger(tmp_path / "data").is_new(src)
