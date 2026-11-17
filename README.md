# zentei

判断ごとに「依拠した前提」を記録し、前提が崩れたときに、見直しが要る判断を引けるようにするツールです。

記録は人が手で書きません。コーディングエージェントとの会話と、既存の意思決定ドキュメントから抜き出します。

記録の形は [docs/01_Data_Model_Decision.md](docs/01_Data_Model_Decision.md)、抜き出し方は [docs/02_Extraction_Decision.md](docs/02_Extraction_Decision.md) にあります。会話の履歴から抜き出す部分は、まだありません。

## 使い方

```bash
zentei apply events.json      # イベントを検証して取り込む
zentei list                   # 前提と判断の一覧
zentei impact pre_some-fact   # この前提が崩れたら、どの判断の見直しが要るか
zentei check pre_some-fact broken --evidence "…"
zentei find 言葉 …            # 言葉を含む前提と判断を、リポジトリをまたいで探す
zentei context                # エージェントに読ませる、作業中のリポジトリの要約
```

記録は `~/.local/share/zentei`（`ZENTEI_DATA` で変更可）の `events/<マシン名>/` に追記されます。

## 開発

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## ライセンス

MIT
