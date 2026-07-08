# bedrock-lab — 自宅用 Bedrock GenAI 環境 (Phase 1)

Amazon Bedrock 上の Claude を AWS のガバナンス (IAM・監査・コスト管理) に正しく組み込むホームラボ / 学習環境。

計画・要件の全文は [docs/plan_phase1.md](docs/plan_phase1.md) を参照。

## 構成

```
[開発端末: chat.py (boto3)]
        |
        | 一時クレデンシャル (assume-role or SSO)
        v
[Bedrock Runtime / ap-northeast-1 / Converse API]
        |
        | jp.anthropic.* (JP cross-region inference profile)
        v
[Claude — 東京/大阪内でルーティング。日本国外に出ない]
        |
        +--> invocation logging --> S3 / CloudWatch Logs
横断: CloudTrail (API 監査) / AWS Budgets (コストアラート)
```

### 使用モデル (2026-07 時点)

| エイリアス | 推論プロファイル ID | 用途 | 疎通確認 |
|---|---|---|---|
| `sonnet` (既定) | `jp.anthropic.claude-sonnet-4-6` | 日常利用・重い検討 | ✅ 2026-07-08 |
| `haiku` | `jp.anthropic.claude-haiku-4-5-20251001-v1:0` | 軽量・高速 | ✅ 2026-07-08 |
| `sonnet45` | `jp.anthropic.claude-sonnet-4-5-20250929-v1:0` | フォールバック | ✅ 2026-07-08 |
| `opus` | `jp.anthropic.claude-opus-4-8` | 上位モデル | ❌ 利用不可 (下記) |

- JP プロファイルは**東京 (ap-northeast-1) / 大阪 (ap-northeast-3) 間のみ**でルーティングされる。リージョン間の通信は AWS Global Network 内で完結し、データは日本国外に出ない。
- **Opus 4.7 / 4.8 は手続きを完了しても呼び出し不可** (2026-07-08 確認)。use case フォーム提出・アグリーメント締結を終え `get-foundation-model-availability` の全項目が AVAILABLE でも、invoke だけ `not available for this account (contact AWS Sales)` で拒否される。最上位モデル群にはステータス API に現れない **AWS Sales 承認制の追加ゲート**がある ([AWS re:Post に同事象の報告あり](https://repost.aws/questions/QUV81Zo9tgTsmfx2ZCPUR0vA/claude-opus-4-7-4-8-on-amazon-bedrock-returns-accessdeniedexception-despite-full-entitlement-and-valid-agreement))。個人アカウントでは当面利用不可のため、上位モデル枠は Sonnet 4.6 で運用 (`opus` エイリアスと締結済みアグリーメントは開放時に備え残置)。
- 最新の提供状況はコンソールまたは `aws bedrock list-inference-profiles --region ap-northeast-1` で確認。

## 前提条件

- Terraform >= 1.5 / Python 3.10+ / AWS CLI
- Terraform を実行できる管理者相当のクレデンシャル (構築時のみ)

## セットアップ

### 1. Bedrock モデルアクセスの有効化 (初回のみ・実施済み)

Anthropic モデルは利用開始前に「**use case フォーム提出**」と「**モデル提供契約 (アグリーメント) の締結**」が必要。コンソール (Bedrock > Model access) からでも、以下のように **CLI でも完結できる** (本環境は 2026-07-08 に CLI で実施済み)。

```bash
# 1) use case フォーム提出 — アカウント単位、全リージョン共有。
#    formData は base64 化した JSON。intendedUsers は「人数」の数値文字列である点に注意
cat > usecase_form.json <<'EOF'
{
  "companyName": "Personal (individual)",
  "companyWebsite": "https://github.com/xxxx",
  "intendedUsers": "1",
  "industryOption": "Technology",
  "otherIndustryOption": "",
  "useCases": "Personal home lab for learning AWS Bedrock governance. Low volume, non-production."
}
EOF
aws bedrock put-use-case-for-model-access --region ap-northeast-1 \
  --form-data "$(base64 -i usecase_form.json)"

# 2) 使うモデルごとにアグリーメント締結
m=anthropic.claude-sonnet-4-6
token=$(aws bedrock list-foundation-model-agreement-offers --model-id "$m" \
  --region ap-northeast-1 --query 'offers[0].offerToken' --output text)
aws bedrock create-foundation-model-agreement --model-id "$m" \
  --offer-token "$token" --region ap-northeast-1

# 3) 状態確認 — agreementAvailability が AVAILABLE になれば締結完了
aws bedrock get-foundation-model-availability --model-id "$m" --region ap-northeast-1
```

- 締結後、実際に呼べるようになるまで **15 分程度の伝播ラグ**がある
- フォーム/アグリーメントは**アカウント単位**で、ルーティング先の大阪にも自動反映される (リージョンごとの作業は不要)
- **注意**: 手続き未完了でも直後の数回は呼び出しが通ることがある (非同期チェックのラグ)。「一度通った = 手続き完了」ではない

### 2. Terraform で基盤構築

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# terraform.tfvars を編集 (alert_email は必須)

terraform init
terraform plan
terraform apply
```

作成されるリソース:

| リソース | 用途 |
|---|---|
| IAM ポリシー `bedrock-lab-invoke` | `jp.anthropic.*` プロファイル限定の呼び出し最小権限 |
| IAM ロール `bedrock-lab-user` | 開発端末から assume する利用者ロール |
| S3 バケット + CloudWatch Logs | invocation logging の出力先 (S3 は 90 日、CWL は 30 日で自動削除) |
| Bedrock invocation logging 設定 | プロンプト/応答の全文記録 (テキストのみ) |
| AWS Budgets | 月次予算の 50% / 80% 実績、100% 予測超過でメール通知 |

> **注意**: invocation logging はアカウント x リージョン単位のシングルトン設定。東京リージョンに既存の設定がある場合は上書きされる。

### 3. CLI クライアントの準備

```bash
cd client
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 4. 一時クレデンシャルの設定

長期アクセスキーは使わず、利用者ロールを assume する。`~/.aws/config` に追記:

```ini
[profile bedrock-lab]
role_arn = <terraform output bedrock_user_role_arn の値>
source_profile = <普段の管理用プロファイル>
region = ap-northeast-1
```

IAM Identity Center (SSO) を使う場合は、Permission Set に `terraform output bedrock_invoke_policy_arn` のポリシーをアタッチし、`aws sso login` で取得したプロファイルをそのまま使う。

### 5. 動作確認

```bash
# 単発実行
python chat.py --profile bedrock-lab --once "こんにちは。1文で自己紹介して。"

# 対話モード
python chat.py --profile bedrock-lab
```

チャット内コマンド: `/model` (切替・一覧) / `/clear` / `/usage` / `/help` / `/quit`

## 監査・可観測性の確認

| 観点 | 確認先 |
|---|---|
| 入出力の全文記録 | CloudWatch Logs `/aws/bedrock/bedrock-lab/model-invocations`、S3 `bedrock-lab-invocation-logs-<account-id>` |
| トークン量・レイテンシ | invocation log 内の `usage` / `metrics`、CloudWatch メトリクス (AWS/Bedrock 名前空間: Invocations, InputTokenCount, OutputTokenCount, InvocationLatency) |
| API 監査 | CloudTrail イベント履歴 (90 日、無料)。設定変更系 (PutModelInvocationLoggingConfiguration 等) は管理イベントとして記録される |
| コスト | Budgets メール通知 + Cost Explorer (サービス = Amazon Bedrock でフィルタ) |
| ローカル会話ログ | `client/logs/session_*.jsonl` (usage 含む) |

## 設計メモ

- **`bedrock:Converse` という IAM アクションは存在しない**。Converse / ConverseStream の呼び出し権限は `bedrock:InvokeModel` / `bedrock:InvokeModelWithResponseStream` でカバーされる。
- **IAM の `description` は日本語不可** (Latin-1 の範囲のみ)。日本語を入れると `ValidationError` で apply が失敗する。
- cross-region inference の IAM は「プロファイル ARN + 全ルーティング先リージョンの foundation-model ARN」の両方の許可が必要。本環境では東京・大阪の 2 リージョン分を許可している。
- 最近の Claude モデル (Sonnet 4.5 以降) は**東京リージョン単体のオンデマンド呼び出しに非対応**のものが多く、inference profile 経由が実質必須。国内所在を保ちつつ新しいモデルを使う手段が JP プロファイル。
- Terraform state はローカル管理 (Phase 1)。複数端末で扱う場合は S3 バックエンド + DynamoDB ロックへ移行する。
- 入力データは基盤モデルの学習に使われない (Bedrock の仕様)。invocation logging を有効にしてもモデル提供元にデータは渡らない。

## コスト目安

- Sonnet 4.6: $3 / $15、Opus 4.8: $5 / $25 (入力/出力 100 万トークンあたり)。JP プロファイル利用による追加料金はない
- S3 / CloudWatch Logs / Budgets: 個人利用の範囲ではほぼ無視できる規模 (数十円/月)
- 固定費はゼロ。使わなければ課金されない

## Phase 2 以降 (将来)

- **Phase 2 (RAG)**: managed Knowledge Bases + OpenSearch Serverless は固定費が大きい (月数百ドル規模)。Aurora Serverless v2 / pgvector や S3 Vectors 等の安価な代替を先に検討
- **Phase 3 (Agents / MCP)**: ツール呼び出し回数分トークンを消費する点に注意
- **Phase 4 (最適化)**: プロンプトキャッシュ、Guardrails
