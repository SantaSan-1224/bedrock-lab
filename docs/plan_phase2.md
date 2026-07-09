# 自宅用 Bedrock GenAI環境 構築計画（Phase 2: RAG）

> Phase 1（素のモデル利用 + IAM・監査・コストの配線）の上に、自分のデータで RAG を構築する。
> 本書は計画／要件。実装は Claude Code で行う。2026-07-09 作成・合意。

## 0. 目的・ユースケース

- **目的**: 企業で最も一般的な RAG ユースケースである「運用ナレッジ検索」の構図を、個人の安全なデータで再現する。managed（Knowledge Bases）と自前実装の両方を体験し、RAG の構成要素（チャンキング → 埋め込み → ベクトル検索 → プロンプト合成 → 出典付き回答)を理解する。
- **ユースケース**: 個人版・運用ナレッジボット。自分の公開記事・技術検証メモ・bedrock-lab の構築記録に対して「Bedrock で AccessDenied が出たときの対処は？」のように質問し、出典付きの回答を得る。
- **位置づけ**: Phase 1 の環境（IAM 最小権限 / invocation logging / Budgets）を土台に拡張。現在環境は destroy 済みのため、Phase 2 の apply で Phase 1 分も同時に再構築する。

## 1. スコープ

**In:**
- Knowledge Bases + S3 Vectors の構築（Terraform）
- データ投入（S3 原本バケット → KB 同期）
- CLI（chat.py）の RAG モード拡張（出典表示付き）
- 自前ミニ RAG（KB を経由せず S3 Vectors を直接叩く）による内部構造の理解

**Out（将来 or 対象外）:**
- データソースの自動同期（EventBridge 等）※チャット UI は M6（おまけ）に昇格
- Guardrails・プロンプトキャッシュ（Phase 4）
- 本業の顧客情報・PJ 資料の投入（**禁止事項**。個人アカウントに置くのは公開情報と個人メモのみ）

## 2. 確定要件

| 項目 | 決定 | 補足 |
|---|---|---|
| リージョン | 東京（ap-northeast-1） | S3 Vectors の東京提供を実機確認済み（2026-07-09）。国内所在方針は Phase 1 から継続 |
| 投入データ | ①公開記事（Qiita/Zenn/note）②技術検証メモ ③bedrock-lab のドキュメント・構築記録 | Markdown 中心、初期数万字規模。顧客情報・本業資料は投入禁止 |
| ベクトルストア | **S3 Vectors** | 2025-12 GA。従量課金で固定費ゼロ。OpenSearch Serverless の固定費問題（月数百ドル）を回避 |
| 埋め込みモデル | Titan Text Embeddings V2 でスタート | 東京提供確認済み。日本語検索品質に不満が出たら Cohere Embed Multilingual と比較検証（任意タスク） |
| 検索・生成 | 3 方式を段階的に体験 | ① RetrieveAndGenerate（フルマネージド）② Retrieve + Converse（検索だけ KB、合成は自前）③ 自前ミニ RAG（埋め込みから S3 Vectors query まで自前） |
| 生成モデル | `jp.anthropic.claude-sonnet-4-6` | Phase 1 と同じ。国内完結 |
| IAM | Phase 1 の最小権限方針を踏襲 | KB 用サービスロール（S3 読取 / 埋め込みモデル呼出 / S3 Vectors 読書き）を新設。利用者ロールに `bedrock:Retrieve` / `bedrock:RetrieveAndGenerate` を追加 |
| 監査 | Phase 1 の invocation logging を継続 | 埋め込み・生成呼び出しの両方が記録される。KB 同期ジョブのログは CloudWatch |
| コスト | Budgets $10/月を継続 | 固定費ゼロ構成。埋め込み初期投入は 1 円未満、質問 1 回数円（生成モデル分） |
| IaC | Terraform | `aws_bedrockagent_knowledge_base` の S3 Vectors 対応は provider v6.27.0 で確認済み |

## 3. 構成（テキスト図 — 実装後に draw.io 化して README へ）

```
[原本: S3 バケット (記事 .md / メモ)]
        |
        | KB データソース同期 (チャンキング → 埋め込み)
        v
[Bedrock Knowledge Base] --埋め込み--> [Titan Embeddings V2 / 東京]
        |
        +--> [S3 Vectors: vector bucket + index / 東京]

[chat.py 拡張]
   ├─ ① RetrieveAndGenerate     … フルマネージド (出典付き回答)
   ├─ ② Retrieve + Converse     … 検索は KB、プロンプト合成は自前
   └─ ③ 自前ミニ RAG            … 埋め込み生成 → S3 Vectors query → Converse
横断: invocation logging / CloudTrail / Budgets (Phase 1 資産を再構築)
```

## 4. 実装前に確認すること

1. S3 Vectors の vector bucket / index 自体の Terraform ネイティブリソース対応（KB 側の参照は対応済み。バケット側が未対応なら CLI 作成 + ARN 参照で代替）
2. RetrieveAndGenerate で inference profile（`jp.`）を modelArn に指定できるか
3. 投入する記事・メモの棚卸し（Kazumaさん: 対象ファイルの選定。公開記事は export 方法も）
4. KB のチャンキング戦略の初期値（デフォルト 300 トークン / セマンティックチャンキング等の選択肢を M2 で比較してよい）

## 5. 実装タスク分解

| マイルストーン | 内容 |
|---|---|
| M1 | Phase 1 環境の再 apply + Terraform 拡張（原本 S3 / S3 Vectors / KB / KB 用サービスロール / 利用者ポリシー拡張） |
| M2 | データ投入 → KB 同期 → CLI で Retrieve の生応答を確認（チャンクの中身を見る） |
| M3 | chat.py に RAG モード追加（`--rag` で RetrieveAndGenerate、出典表示、通常モードとの切替) |
| M4 | 自前ミニ RAG 実装（S3 Vectors 直叩き）。①〜③の検索品質・レイテンシ・コストの比較メモ作成 |
| M5 | README 更新（構成図 draw.io 化）、`phase2` タグ、（任意）Qiita/Zenn 記事化の判断 |
| M6（おまけ） | 簡易チャット UI（Streamlit、ローカル起動・追加リソースなし）。「フロントから入力→応答」の実務感を体験する（2026-07-09 M3 実験後の Kazumaさん要望で追加） |

## 6. 学習チェックポイント

- RAG の構成要素を managed / 自前の両方で説明できる
- KB のデータソース同期モデル（S3 → 同期ジョブ → ベクトルインデックス）と失敗時の調べ方
- S3 Vectors の API 体系（vector bucket / index / PutVectors / QueryVectors）
- チャンキング戦略と検索品質の関係（実データで体感）
- 出典付き回答（citation）の仕組みと、ハルシネーション抑制としての意味
- 埋め込みモデルの選定観点（日本語品質・次元数・コスト）

## 7. Phase 3 以降への接続

- **Agents（Phase 3）**: KB は Agent の知識源としてそのまま接続できる。RetrieveAndGenerate の理解が Agent 統合の土台になる
- **コスト最適化（Phase 4）**: RAG はコンテキストが長くなるため、プロンプトキャッシュの効果検証素材として最適
