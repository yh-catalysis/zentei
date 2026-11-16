"""Command line: take in events, check premises, and ask what is affected."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from zentei import state as st
from zentei import store
from zentei.model import Event

STATUS_LABEL = {"holds": "成立", "broken": "崩れた", "unverified": "未確認"}


def _load(root: Path) -> tuple[list[Event], st.State]:
    events = store.read_events(root)
    return events, st.fold(events)


def _read_items(path: str) -> list[dict]:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    text = text.strip()
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def cmd_apply(args: argparse.Namespace) -> int:
    """Validate the whole batch against the current state before writing any of it."""
    root = store.data_dir()
    _, state = _load(root)
    accepted, errors = [], []
    for index, item in enumerate(_read_items(args.file), 1):
        try:
            event = store.new_event(item["type"], item["data"], item.get("origin"))
        except (KeyError, TypeError, ValidationError) as e:
            detail = e.errors()[0] if isinstance(e, ValidationError) else {"msg": repr(e), "loc": ()}
            where = ".".join(str(x) for x in detail["loc"])
            errors.append(f"{index}: {where}: {detail['msg']}" if where else f"{index}: {detail['msg']}")
            continue
        reason = st.problem(state, event)
        if reason:
            errors.append(f"{index}: {reason}")
            continue
        st.apply(state, event)
        accepted.append(event)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        print(f"{len(errors)} 件に問題があるため、何も書き込んでいません", file=sys.stderr)
        return 1
    if not args.dry_run:
        store.append_events(root, accepted)
    print(f"{len(accepted)} 件を{'確認しました（書き込みなし）' if args.dry_run else '書き込みました'}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    root = store.data_dir()
    _, state = _load(root)
    data = {"premise": args.premise, "result": args.result, "as_of": (args.as_of or date.today()).isoformat()}
    if args.evidence:
        data["evidence"] = args.evidence
    event = store.new_event("premise.checked", data)
    if reason := st.problem(state, event):
        print(reason, file=sys.stderr)
        return 1
    store.append_events(root, [event])
    st.apply(state, event)
    _print_findings(state, st.impact(state))
    return 0


def _premise_line(ps: st.PremiseState) -> str:
    p = ps.premise
    line = f"- [{STATUS_LABEL[ps.status]}] `{p.id}` ({p.kind}) {p.statement}"
    if p.as_of:
        line += f"（{p.as_of} 時点）"
    if p.breaks_when:
        line += f" / 崩れる条件: {p.breaks_when}"
    return line


def _print_findings(state: st.State, findings: list[st.Finding]) -> None:
    def statement(record_id: str) -> str:
        if record_id in state.premises:
            return f"`{record_id}` {state.premises[record_id].premise.statement}"
        if record_id in state.decisions:
            return f"`{record_id}` {state.decisions[record_id].decision.title}"
        return f"`{record_id}`"

    if not findings:
        print("影響を受ける判断はありません")
        return
    sections = [
        ("見直しが要る判断", lambda f: f.needs_review),
        ("根拠が減った判断", lambda f: f.weakened and not f.needs_review),
        ("却下した案が復活しうる判断", lambda f: f.revivable),
    ]
    for title, pick in sections:
        chosen = [f for f in findings if pick(f)]
        if not chosen:
            continue
        print(f"## {title}\n")
        for f in chosen:
            d = f.decision
            print(f"- `{d.id}` [{d.scope}] {d.title}: {d.chosen}")
            if title.startswith("却下"):
                for option, causes in f.revivable.items():
                    print(f"  - 復活しうる案: {option}")
                    for cause in causes:
                        print(f"    - 却下の根拠だった: {statement(cause)}")
            else:
                for cause in f.needs_review if title.startswith("見直し") else f.weakened:
                    print(f"  - {statement(cause)}")
        print()


def cmd_impact(args: argparse.Namespace) -> int:
    _, state = _load(store.data_dir())
    unknown = [p for p in args.premise if not state.has(p)]
    if unknown:
        print(f"unknown records: {unknown}", file=sys.stderr)
        return 1
    _print_findings(state, st.impact(state, args.premise))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    _, state = _load(store.data_dir())
    premises = [ps for ps in state.premises.values() if not args.scope or ps.premise.scope == args.scope]
    decisions = [ds for ds in state.decisions.values() if not args.scope or ds.decision.scope == args.scope]
    print(f"## 前提 {len(premises)} 件\n")
    for ps in sorted(premises, key=lambda ps: (ps.premise.scope, ps.premise.id)):
        print(f"{_premise_line(ps)} [{ps.premise.scope}]")
    print(f"\n## 判断 {len(decisions)} 件\n")
    for ds in sorted(decisions, key=lambda ds: (ds.decision.scope, ds.decision.id)):
        d = ds.decision
        print(f"- [{ds.status}] `{d.id}` [{d.scope}] {d.title}: {d.chosen}（依存 {len(d.depends_on)} 件）")
    for warning in state.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    """A summary for a coding agent to read at the start of work in one repository."""
    _, state = _load(store.data_dir())
    premises, decisions = st.in_scope(state, args.scope)
    if not premises and not decisions:
        return 0
    findings = [f for f in st.impact(state) if f.decision.scope == args.scope]
    print(f"# このリポジトリの判断が依拠している前提（{args.scope}）\n")
    print("作業中に、下の前提と食い違う情報に気づいたら、利用者に知らせること。\n")
    if findings:
        _print_findings(state, findings)
    print("## 前提\n")
    order = {"broken": 0, "unverified": 1, "holds": 2}
    for ps in sorted(premises, key=lambda ps: (order[ps.status], ps.premise.id)):
        print(_premise_line(ps))
    active = [ds.decision for ds in decisions if ds.status == "active"]
    print("\n## 判断\n")
    for d in sorted(active, key=lambda d: d.id):
        print(f"- `{d.id}` {d.title}: {d.chosen}")
        for dep in state.dependencies(d):
            mark = "却下の根拠" if dep.role == "rules_out" else ("要" if dep.strength == "critical" else "根拠")
            suffix = f"（案: {dep.alternative}）" if dep.alternative else ""
            print(f"  - {mark}: `{dep.on}`{suffix}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zentei", description="判断が依拠する前提を記録し、影響を照会する")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("apply", help="イベントを検証して取り込む（JSON の配列、または JSON Lines）")
    p.add_argument("file", help="ファイルのパス。- で標準入力")
    p.add_argument("--dry-run", action="store_true", help="検証だけ行い、書き込まない")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("check", help="前提を確かめた結果を記録する")
    p.add_argument("premise")
    p.add_argument("result", choices=["holds", "broken"])
    p.add_argument("--as-of", type=date.fromisoformat)
    p.add_argument("--evidence")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("impact", help="崩れた前提の影響を出す。前提を指定すると、崩れたと仮定して出す")
    p.add_argument("premise", nargs="*")
    p.set_defaults(func=cmd_impact)

    p = sub.add_parser("list", help="前提と判断の一覧")
    p.add_argument("--scope")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("context", help="エージェントに読ませる、リポジトリごとの要約")
    p.add_argument("--scope", required=True, help="owner/name")
    p.set_defaults(func=cmd_context)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
