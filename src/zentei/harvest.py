"""Extract premises and decisions from a document or an agent memory file with a language model."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from zentei import state as st
from zentei import store
from zentei.model import GLOBAL, Event

Runner = Callable[[str], dict[str, Any]]

NULLABLE = {"type": ["string", "null"]}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["premises", "decisions", "questions"],
    "properties": {
        "premises": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "statement", "breaks_when", "kind", "is_global", "verified", "stated_by_user"],
                "properties": {
                    "id": {"type": "string"},
                    "statement": {"type": "string"},
                    "breaks_when": NULLABLE,
                    "kind": {"enum": ["fact", "measurement", "situation", "expectation", "value"]},
                    "is_global": {"type": "boolean"},
                    "verified": {"type": "boolean"},
                    "stated_by_user": {"type": "boolean"},
                },
            },
        },
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "title", "chosen", "alternatives", "decided_on", "section", "depends_on"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "chosen": {"type": "string"},
                    "alternatives": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["option", "reason"],
                            "properties": {"option": {"type": "string"}, "reason": {"type": "string"}},
                        },
                    },
                    "decided_on": NULLABLE,
                    "section": NULLABLE,
                    "depends_on": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["on", "role", "alternative", "strength"],
                            "properties": {
                                "on": {"type": "string"},
                                "role": {"enum": ["supports", "rules_out"]},
                                "alternative": NULLABLE,
                                "strength": {"enum": ["critical", "contributing"]},
                            },
                        },
                    },
                },
            },
        },
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["about", "question"],
                "properties": {"about": {"type": "string"}, "question": {"type": "string"}},
            },
        },
    },
}

INSTRUCTIONS = """\
下の文書から、「判断」と、その判断が依拠している「前提」を抜き出す。

# 判断
- 決めたこと1つを1件にする。文書1本を1件にしない
- 理由が書かれている判断だけを抜き出す。理由の無い仕様の列挙、作業の記録、手順は抜き出さない
- `alternatives` は、文書に書かれている、選ばなかった案とその理由
- `decided_on` は、文書に決めた日が書かれていれば YYYY-MM-DD。無ければ null
- `section` は、文書の中の節の番号や見出し。無ければ null

# 前提
- 判断の根拠になっている事柄を、1件に1つ、1文で書く。数量が書かれていれば数量で書く
- 判断の言い換えや、判断そのものを前提にしない
- `kind`:
  - fact: 外の世界の事実（製品の仕様、料金、制度）
  - measurement: 自分で測った値（件数、時間、費用）
  - situation: 自分の側の状況（利用者、体制、予算、使い方）
  - expectation: 将来の見込み、経験則
  - value: 方針や価値観
- `is_global`: 製品の仕様のように、ほかのリポジトリの判断でも使える前提なら true
- `verified`: 文書が確かめた事実として書いていれば true。「たしか」「はず」「見込み」のように確かめていなければ false
- `stated_by_user`: 利用者本人の発言や指示として書かれていれば true
- `breaks_when`: 何を観測したら崩れたとするか。前提の反対がはっきりしていれば書く（「X に機能 Y が無い」なら「X に機能 Y が出た」）。数量の境目だけは、文書に書かれていなければ自分で作らず null にする。value は null
- 「〜になったら見直す」という条件は、「今は成り立っている前提」に直して前提にする

# 依存
- `on` は前提の id、または別の判断の id
- `role`: 選んだ案の根拠なら supports。ある案を却下した根拠なら rules_out とし、`alternative` にその案の `option` をそのまま書く。supports のとき `alternative` は null
- `strength`: 文書が「これが変わったら見直す」と書いているか、理由がそれ1つしか無いときだけ critical。それ以外は contributing。推測で critical にしない

# id
- 前提は `pre_`、判断は `dec_` で始め、英小文字・数字・ハイフンで中身を表す短い名前にする
- 「既存の記録」に同じ中身の前提や判断があれば、新しく作らず、その id を `on` に書く。既存の id を `premises` や `decisions` に入れ直さない
- global でない記録の id には、指定された接頭辞を付ける

# 質問
- 本人にしか答えられず、答えで記録が変わるものだけを、文書1本につき2件まで。無ければ空にする
- supports が2つ以上あって critical が1つも無い判断には、「どれが崩れたら決め直しますか」を、前提を挙げて聞く
- 数量の境目が書かれていない measurement が判断の根拠になっていれば、境目を聞く
- `about` は対象の id

文章は文書と同じ言語で書く。抜き出すものが無ければ、空の配列を返す。
"""


@dataclass
class Source:
    path: Path
    scope: str
    kind: str  # "doc" or "memory"
    ref: str
    dated: date | None = None

    @property
    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


@dataclass
class Result:
    events: list[Event] = field(default_factory=list)
    questions: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def claude_runner(model: str) -> Runner:
    def run(prompt: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as empty:
            done = subprocess.run(
                ["claude", "-p", "--model", model, "--output-format", "json", "--no-session-persistence"]
                + ["--tools", "", "--setting-sources", "", "--strict-mcp-config"]
                + ["--system-prompt", "指示どおりに、文書に書かれていることだけを JSON で返す。"]
                + ["--json-schema", json.dumps(SCHEMA)],
                input=prompt,
                capture_output=True,
                text=True,
                cwd=empty,
                timeout=600,
            )
        if done.returncode != 0:
            raise RuntimeError(done.stderr.strip() or done.stdout.strip()[:500])
        output = json.loads(done.stdout).get("structured_output")
        if not isinstance(output, dict):
            raise RuntimeError("the model returned no structured output")
        return output

    return run


def prefix_for(scope: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", scope.split("/")[-1].lower()).strip("-")


def build_prompt(source: Source, state: st.State, feedback: list[str]) -> str:
    known_premises = [
        f"- {pid}: {ps.premise.statement}"
        for pid, ps in sorted(state.premises.items())
        if ps.premise.scope in (source.scope, GLOBAL)
    ]
    known_decisions = [
        f"- {did}: {ds.decision.title} → {ds.decision.chosen}"
        for did, ds in sorted(state.decisions.items())
        if ds.decision.scope == source.scope
    ]
    parts = [
        INSTRUCTIONS,
        f"# この文書\n- リポジトリ: {source.scope}\n- 種類: {'エージェントのメモリ' if source.kind == 'memory' else '意思決定ドキュメント'}"
        f"\n- global でない記録の id の接頭辞: pre_{prefix_for(source.scope)}- / dec_{prefix_for(source.scope)}-",
        "# 既存の記録\n" + ("\n".join(known_premises + known_decisions) or "（なし）"),
    ]
    if feedback:
        parts.append("# 前回の出力の誤り（直して、全体を出し直す）\n" + "\n".join(f"- {e}" for e in feedback))
    parts.append(f"# 文書\n\n{source.text}")
    return "\n\n".join(parts)


def _date(value: str | None) -> str | None:
    try:
        return date.fromisoformat(value).isoformat() if value else None
    except ValueError:
        return None


def _dependencies_first(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order decisions so that one that another in the same document rests on comes first.

    A document may state a conclusion before the decisions it builds on. A cycle keeps its
    original order, and is then reported as a dependency on an unknown record.
    """
    pending = {d["id"]: d for d in decisions}
    ordered: list[dict[str, Any]] = []
    while pending:
        ready = [d for d in pending.values() if not any(dep["on"] in pending for dep in d["depends_on"])]
        for d in ready or list(pending.values()):
            ordered.append(pending.pop(d["id"]))
    return ordered


def to_events(output: dict[str, Any], source: Source, state: st.State, extractor: str) -> Result:
    """Turn model output into events, checked one by one against a copy of the state."""
    result = Result()
    fallback = source.dated.isoformat() if source.dated else None
    scratch = st.fold([])
    scratch.premises, scratch.decisions = dict(state.premises), dict(state.decisions)
    scratch.aliases, scratch.retracted = dict(state.aliases), set(state.retracted)

    def add(type: str, data: dict[str, Any], speaker: str | None) -> None:
        origin = {"kind": source.kind, "ref": source.ref, "extractor": extractor}
        if speaker:
            origin["speaker"] = speaker
        try:
            event = store.new_event(type, {k: v for k, v in data.items() if v not in (None, "")}, origin)
        except ValidationError as e:
            detail = e.errors()[0]
            result.errors.append(f"{data.get('id')}: {'.'.join(map(str, detail['loc']))}: {detail['msg']}")
            return
        if reason := st.problem(scratch, event):
            result.errors.append(reason)
            return
        st.apply(scratch, event)
        result.events.append(event)

    for p in output.get("premises", []):
        data = {
            "id": p["id"],
            "statement": p["statement"],
            "breaks_when": p["breaks_when"],
            "kind": p["kind"],
            "scope": GLOBAL if p["is_global"] else source.scope,
            "as_of": fallback if p["verified"] else None,
        }
        add("premise.added", data, "user" if p["stated_by_user"] else None)
    for d in _dependencies_first(output.get("decisions", [])):
        section = f"#{d['section']}" if d["section"] else ""
        data = {
            "id": d["id"],
            "scope": source.scope,
            "title": d["title"],
            "chosen": d["chosen"],
            "alternatives": d["alternatives"],
            "decided_on": _date(d["decided_on"]) or fallback,
            "sources": [{"kind": source.kind, "ref": source.ref + section}],
            "depends_on": [{k: v for k, v in dep.items() if v is not None} for dep in d["depends_on"]],
        }
        supports = [dep for dep in data["depends_on"] if dep["role"] == "supports"]
        if len(supports) == 1:
            supports[0]["strength"] = "critical"  # the only stated reason: without it the decision has none
        add("decision.added", data, None)
    known = {e.data["id"] for e in result.events}
    for q in output.get("questions", []):
        if q["about"] in known or scratch.has(q["about"]):
            result.questions.append({"ref": source.ref, "about": q["about"], "question": q["question"]})
    return result


def extract(source: Source, state: st.State, run: Runner, extractor: str, attempts: int = 2) -> Result:
    result = Result()
    for _ in range(attempts):
        output = run(build_prompt(source, state, result.errors))
        result = to_events(output, source, state, extractor)
        if not result.errors:
            break
    return result


def frontmatter_date(text: str) -> date | None:
    match = re.search(r"^\s*modified:\s*['\"]?(\d{4}-\d{2}-\d{2})", text, re.MULTILINE)
    return date.fromisoformat(match.group(1)) if match else None


def git_added_date(path: Path) -> date | None:
    done = subprocess.run(
        ["git", "log", "--diff-filter=A", "--follow", "--format=%as", "--", path.name],
        capture_output=True,
        text=True,
        cwd=path.parent,
    )
    lines = done.stdout.split()
    return date.fromisoformat(lines[-1]) if done.returncode == 0 and lines else None


def git_relative(path: Path) -> str:
    done = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, cwd=path.parent)
    top = Path(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else path.parent
    return str(path.resolve().relative_to(top.resolve()))


def make_source(path: Path, scope: str, kind: str, dated: date | None) -> Source:
    if kind == "memory":
        return Source(
            path, scope, kind, f"{scope}:memory/{path.stem}", dated or frontmatter_date(path.read_text("utf-8"))
        )
    return Source(path, scope, kind, f"{scope}:{git_relative(path)}", dated or git_added_date(path))


class Ledger:
    """What this machine has already read, and the questions still open. Kept per machine, like events."""

    def __init__(self, root: Path) -> None:
        self.seen_path = root / "harvest" / f"{store.machine_name()}.json"
        self.questions_path = root / "questions" / f"{store.machine_name()}.jsonl"
        self.seen: dict[str, str] = json.loads(self.seen_path.read_text("utf-8")) if self.seen_path.exists() else {}

    def is_new(self, source: Source) -> bool:
        return self.seen.get(source.ref) != source.digest

    def record(self, source: Source, questions: list[dict[str, str]]) -> None:
        if self.seen_path.exists():  # another run on this machine may have written since we loaded
            self.seen = json.loads(self.seen_path.read_text("utf-8")) | self.seen
        self.seen[source.ref] = source.digest
        self.seen_path.parent.mkdir(parents=True, exist_ok=True)
        self.seen_path.write_text(json.dumps(self.seen, ensure_ascii=False, indent=1, sort_keys=True) + "\n", "utf-8")
        if questions:
            self.questions_path.parent.mkdir(parents=True, exist_ok=True)
            with self.questions_path.open("a", encoding="utf-8") as f:
                for q in questions:
                    f.write(json.dumps({"at": datetime.now().astimezone().isoformat(), **q}, ensure_ascii=False) + "\n")
