# CLAUDE.md — bedrock-lab

## プロジェクト概要

- 自宅用 Bedrock GenAI 環境 (ホームラボ / 学習)。業務 PJ ではない
- 目的: Bedrock を AWS ガバナンス (IAM・監査・コスト) に正しく組み込む経験
- **Phase 1 完了** (素のモデル利用、`phase1` タグ)。**Phase 2 (RAG) 完了** (`phase2` タグ) — KB + S3 Vectors、chat.py RAGモード + mini_rag.py (3方式) + Streamlit UI。実測比較は `docs/rag_comparison.md`
- **Phase 3 (Agents) M1-M5 完了** — AWS 運用エージェント (read-onlyツール4種: get_cost/search_logs/list_resources/search_kb)。方式②=agent_cli.py (自前 tool use ループ、ローカル) / 方式①=同コードを AgentCore Runtime に直接コードデプロイ (agent_runtime.py + build_runtime_zip.sh + terraform/runtime.tf)。実測比較は `docs/agent_comparison.md`。残 = M6 おまけ (Gateway MCP化 or Streamlit)
- 禁止事項: **本業の顧客情報・PJ資料は投入しない** (公開記事と個人メモのみ)。エージェントのツールは read-only 限定 (書き込み権限を持たせない)
- Phase 2 の運用知見: 自作 S3 Vectors インデックスは non-filterable に AMAZON_BEDROCK_TEXT + AMAZON_BEDROCK_METADATA の2つ必須 / カスタムメタデータ 1KB制限 / QueryVectors+returnMetadata には GetVectors 権限 / KB Retrieve は結合チャンクを返す (生チャンクの数倍)
- Phase 3 の運用知見: Bedrock Agents Classic は 2026-07-30 新規終了 (AgentCore が後継、東京対応済み) / 直接コードデプロイは Lambda zip と同型 (arm64/py3.13 で依存取得) / ツール選択は同一コードでも非決定的に揺れる / Runtime のセッション = microVM のプロセスメモリ (idle timeout が会話の寿命) / KB ID・Runtime ARN はハードコード禁止 (名前から動的解決)

## 確定事項 (Phase 1)

- リージョン: ap-northeast-1 (東京)。データ所在は日本国内限定
- モデル呼び出しは **JP cross-region inference profile のみ** (東京/大阪ルーティング、国外に出ない)
  - 疎通済 (2026-07-08): `jp.anthropic.claude-sonnet-4-6` (既定) / `jp.anthropic.claude-haiku-4-5-20251001-v1:0` / `jp.anthropic.claude-sonnet-4-5-20250929-v1:0`
- **Opus 4.7/4.8 は手続き完了でも invoke 不可** (全ステータス AVAILABLE でも `contact AWS Sales` で拒否。最上位モデルは Sales 承認制の追加ゲートあり)。上位モデル枠は Sonnet 4.6 で運用。global プロファイルは国内所在方針に反するので使わない
- モデル利用開始手続き (use case フォーム + アグリーメント) は CLI で実施済み。手順は README「モデルアクセスの有効化」参照。伝播 ~15分、手続き前でも数回通ることがある点に注意
- API: Converse / ConverseStream (boto3)。IAM アクションは `bedrock:InvokeModel*` (`bedrock:Converse` は存在しない)
- 認証: 長期キー禁止。`bedrock-lab-user` ロールの assume か SSO

## 構成

```
terraform/   IAM (最小権限ポリシー+利用者ロール) / invocation logging (S3+CWL) / Budgets
client/      chat.py (boto3 Converse API、ストリーミング、JSONL会話ログ)
docs/        計画書
```

## 運用ルール

- `terraform apply` 前に `terraform.tfvars` を用意 (alert_email 必須。gitignore 済み)
- Terraform state はローカル。このディレクトリを消すと state も消える点に注意
- invocation logging はアカウント x リージョンのシングルトン。東京の既存設定を上書きする
- 会話ログ `client/logs/` はコミットしない (gitignore 済み)

## 既知の注意点

- モデルアクセス有効化は東京 + 大阪の両方で実施 (JP プロファイルのルーティング先のため)
- JP プロファイルの提供モデルは今後増える想定。追加時は `client/chat.py` の `MODEL_ALIASES` と README の表を更新
- Phase 2 (RAG) 検討時: managed Knowledge Bases + OpenSearch Serverless は固定費大。安価な代替を先に検討
