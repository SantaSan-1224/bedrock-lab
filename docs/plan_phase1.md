# 自宅用 Bedrock GenAI環境 構築計画（Phase 1）

> 本書は「計画／要件」です。実装は Claude Code で行う前提で、設計と決定事項を整理しています。
> (2026-07-08 プロジェクト開始時点の原文。実装時の技術補正は末尾の「実装時補正」を参照)

## 0. 目的・前提

- **目的**：ホームラボ／学習。Amazon Bedrock を AWS のガバナンス（IAM・監査・コスト管理）に正しく組み込む経験を積む。
- **位置づけ**：すでに Claude（SaaS）は利用中。本環境の価値は「AWSネイティブな GenAI 構築の経験」と「自分の環境での制御」。モデルの賢さより *作る過程の学び* を重視。
- **重要な整理**：当初想定した「ローカルLLM」と Bedrock は別物。Bedrock はマネージドサービスで、モデルは AWS 側で動き、こちらは API を呼ぶだけ（GPU 不要）。トラフィックはモデル提供元に渡らず、入力データは基盤モデルの学習に使われない設計。

## 1. 全体スコープ（段階構成）

| Phase | 内容 | 状態 |
|---|---|---|
| **Phase 1** | 素のモデル利用。CLI/SDK で Bedrock を叩く。AWSらしい配線（IAM・監査・コスト） | **今回確定** |
| Phase 2 | 自分のデータで RAG。※managed Knowledge Bases は固定費が高いので注意 | 任意・将来 |
| Phase 3 | Agents / MCP によるツール連携 | 任意・将来 |
| Phase 4 | Guardrails、プロンプトキャッシュ等のコスト最適化 | 任意・将来 |

各 Phase は独立して価値が出る構成。Phase 1 で止めても学びは完結する。

## 2. Phase 1 確定要件

| 項目 | 決定 | 補足 |
|---|---|---|
| リージョン | 東京（ap-northeast-1）優先 | データを国内に保持。**注意**：最新モデルは米国リージョン先行のことがある。東京で提供されているモデルに限定し、厳密な国内所在を保つなら cross-region 推論は使わない（日本を出ない範囲のみ）。実際の可用性はコンソール／ドキュメントで要確認 |
| モデル | Bedrock 上の Claude | 日常は Sonnet 系、重い検討時に上位モデルの2段構え。呼び出しは Converse API（モデル差を吸収する統一API） |
| 認証・権限 | 専用 IAM ロール＋最小権限 | `bedrock:InvokeModel` / `bedrock:Converse` / `bedrock:ListFoundationModels` 程度。長期アクセスキーは避け、IAM Identity Center（SSO）で一時クレデンシャル推奨 |
| クライアント | CLI/SDK（boto3）の薄いラッパー | OpenAI互換プロキシ＋チャットUI は Phase 1 では作らない（将来の拡張余地として留保） |
| 可観測性・監査 | invocation logging / CloudTrail / CloudWatch | Bedrock の model invocation logging を有効化し入出力を S3／CloudWatch Logs へ。CloudTrail で API 監査、CloudWatch でトークン量・レイテンシ |
| コスト | AWS Budgets ＋ Cost Explorer | 月次アラート必須。従量課金の事故防止 |
| IaC | Terraform | 既存の Terraform / GitHub Actions 経験が活き、再現性を確保。Claude Code 実装と好相性 |

## 3. 構成（テキスト図）

```
[開発端末: CLI/SDK (boto3)]
        |
        | IAM 一時クレデンシャル (SSO)
        v
[Bedrock Runtime / ap-northeast-1 / Converse API] ---> [Claude]
        |
        +--> invocation logging --> S3 / CloudWatch Logs
横断: CloudTrail（API監査） / AWS Budgets（コストアラート）
```

## 4. 実装前に用意・確認すること（アカウント依存）

1. Bedrock のモデルアクセスを有効化（使う Claude モデルを **ap-northeast-1** で有効化）。
2. 東京リージョンで使いたいモデルが提供されているかをコンソールで確認。
3. AWS Budgets の閾値を決める（例：月◯◯円でアラート）。
4. IAM Identity Center（SSO）を使うか、個人アカウントの権限方針を決める。

## 5. Claude Code への実装タスク分解（例）

- **Terraform モジュール**：最小権限 IAM ロール、Bedrock invocation logging 設定、ログ用 S3 バケット、CloudWatch Logs、AWS Budgets。
- **CLI クライアント**：boto3 で Converse API 呼び出し、モデル切替、簡易チャットループ、ローカルへの会話ログ出力。
- **ドキュメント**：README と動作確認手順。

## 6. Phase 1 で身につくこと（学習チェックポイント）

- Converse API の使い方とモデル選定／切替
- IAM 最小権限と SSO 一時クレデンシャルの運用
- invocation logging / CloudTrail による GenAI の監査
- AWS Budgets によるコストガードレール
- Terraform による再現性のある構築

## 7. 将来フェーズの判断材料（メモ）

- **RAG（Phase 2）**：managed Knowledge Bases は便利だが、ベクトルストアに OpenSearch Serverless を使うと固定費が大きい（月数百ドル規模になり得る）。個人なら Aurora/pgvector や S3 ベースなど安価な代替、もしくは「RAG 無しで足りるか」をまず見極める。
- **Agents（Phase 3）**：ツール呼び出しのたびにトークンを消費（例：5回呼ぶタスクは単発の約5倍）。
- **コスト最適化（Phase 4）**：プロンプトキャッシュは使い方次第で大幅にコスト削減できる。

---

## 実装時補正（2026-07-08 調査結果）

計画時の想定に対し、実装調査で以下を補正した。

1. **cross-region 推論の再評価**
   計画では「厳密な国内所在を保つなら cross-region 推論は使わない」としていたが、**JP 系 cross-region inference profile (`jp.anthropic.*`) は東京⇔大阪の 2 リージョン内でのみルーティングされ、日本国外に出ない**。かつ最近の Claude モデル (Sonnet 4.5 以降) は東京単体のオンデマンド呼び出しに非対応のため、「国内所在 + 新しいモデル」を両立する手段として JP プロファイルを採用した。

2. **利用可能モデル (2026-07-08 実機確認)**
   - 疎通確認済み: `jp.anthropic.claude-sonnet-4-6` (既定) / `jp.anthropic.claude-haiku-4-5-20251001-v1:0` / `jp.anthropic.claude-sonnet-4-5-20250929-v1:0`
   - Opus 4.7/4.8: JP プロファイルは存在し、use case フォーム提出・アグリーメント締結後も (全ステータス AVAILABLE)、invoke だけ `not available for this account (contact AWS Sales)` で拒否。最上位モデル群にはステータス API に現れない Sales 承認制の追加ゲートがある (AWS re:Post で同事象の報告複数)。個人アカウントでは実質利用不可のため、**計画の「上位モデル」枠は Sonnet 4.6 を充てる**。

5. **モデル利用開始手続き (計画 4 章「モデルアクセス有効化」の実態)**
   コンソールの Model access 画面相当の手続きは CLI で完結できた:
   `put-use-case-for-model-access` (use case フォーム、アカウント単位) → `create-foundation-model-agreement` (モデルごと) → 伝播 ~15 分。
   注意: 手続き未完了でも直後の数回は呼び出しが通ることがある (非同期チェックのラグ)。動作確認は手続き完了を確認してから行うこと。

3. **IAM アクションの訂正**
   `bedrock:Converse` という IAM アクションは存在しない。Converse / ConverseStream は `bedrock:InvokeModel` / `bedrock:InvokeModelWithResponseStream` でカバーされる。また cross-region inference ではプロファイル ARN に加えルーティング先リージョン (東京・大阪) の foundation-model ARN の許可が必要。

4. **モデルアクセス有効化**
   東京に加え、ルーティング先の大阪 (ap-northeast-3) でも対象モデルの有効化を推奨。
