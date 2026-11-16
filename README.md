# zentei

判断ごとに「依拠した前提」を記録し、前提が崩れたときに、見直しが要る判断を引けるようにするツールです。

記録は人が手で書きません。コーディングエージェントとの会話と、既存の意思決定ドキュメントから抜き出します。

まだ設計の段階で、動くものはありません。

## 開発

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## ライセンス

MIT
