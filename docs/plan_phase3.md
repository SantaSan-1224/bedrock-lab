# 自宅用 Bedrock GenAI環境 構築計画（Phase 3: Agents）

> Phase 2（RAG）までの資産の上に、「調べて→判断して→ツールを使って答える」エージェントを構築する。
> 本書は計画／要件。実装は Claude Code で行う。2026-07-10 作成・合意（同日 AgentCore 軸に改訂）。

## 0. 目的・ユースケース

- **目的**: 企業で最も現実的なエージェントユースケースである「運用一次対応エージェント」の構図を、個人の AWS アカウントで安全に再現する。エージェントループを自前実装（Converse API の tool use）で理解した上で、同じエージェントを **AgentCore Runtime** にデプロイし、「ローカル開発 → マネージドランタイムでの運用化」を体験する。
- **ユースケース**: 個人版・AWS 運用エージェント。自分のアカウントに対して以下のような質問に、エージェントが**自分でツールを選んで**答える。
  - 「今月の AWS コストはいくら? サービス別に見せて」→ Cost Explorer ツール
  - 「Bedrock の invocation ログに最近エラーは出てる?」→ CloudWatch Logs ツール
  - 「このアカウントに今どんなリソースが残ってる?」→ リソース棚卸しツール
  - 「S3 Vectors の 2048B エラーの対処は?」→ Knowledge Base 検索（Phase 2 資産）
  - 「コストが先月より増えてたら原因になりそうなリソースを調べて」→ **複数ツールのマルチステップ実行**
- **安全方針**: ツールはすべて **read-only**(照会系のみ)。書き込み系の権限はエージェントに一切持たせない。企業導入で最初に問われる「エージェントに何をさせてよいか」の線引きを、設計として体験する。
- **位置づけ**: Phase 1（ガバナンス）+ Phase 2（KB + S3 Vectors）を土台に拡張。環境は destroy 済みのため、Phase 3 の apply で全資産を再構築する（KB 再同期含め約15分）。

## 0.5 技術選定の経緯（2026-07-10 改訂の理由）

- 当初案は Bedrock Agents（アクショングループ + Lambda）を方式①としていた。しかし **Bedrock Agents は「Classic」となり 2026-07-30 で新規顧客の受付を終了**（公式ドキュメント明記）。終息するサービスは学習・記事化の題材として不適
- AWS の公式な後継は **Amazon Bedrock AgentCore**。当初「東京非対応」との情報（2026年4月時点のブログ）で Out にしていたが、**公式リージョン表で東京対応済みを確認**（Runtime / Gateway / Identity / Memory / Built-in Tools / Observability / Policy / Evaluations が ✓。非対応は payments preview のみ）→ 方式①を AgentCore に差し替え
- この「Classic 新規終了 → AgentCore へ」という状況自体が記事の時事フックになる

## 1. スコープ

**In:**
- 方式②: 自前 tool use ループ（Converse API の toolConfig / toolUse / toolResult をローカル Python で回す。追加インフラ不要）
- 方式①: **AgentCore Runtime へのデプロイ**（②のエージェントコードを bedrock-agentcore SDK でラップし、東京の Runtime にデプロイ。InvokeAgentRuntime で呼び出し）
- read-only 運用ツール 3種 + KB 検索
- 2方式の比較メモ（`docs/agent_comparison.md`。Phase 2 の rag_comparison.md と同じ型。比較軸は「ローカル実行 vs マネージドランタイム」）

**Out（将来 or 対象外）:**
- **Bedrock Agents Classic**: 2026-07-30 新規受付終了のため対象外（§0.5）
- AgentCore Gateway（ツールの MCP 化）・Memory・Identity: **M6 おまけ候補**。Runtime を最小で完走させることを優先し、時間と興味に応じて Gateway から着手
- マルチエージェント協調（単一エージェントに絞る）
- 書き込み系ツール（リソース作成・変更・削除。read-only 徹底）
- Guardrails・プロンプトキャッシュ（Phase 4）
- 本業の顧客情報・PJ 資料の利用（Phase 2 から継続の禁止事項）

## 2. 確定要件

| 項目 | 決定 | 補足 |
|---|---|---|
| リージョン | 東京（ap-northeast-1） | 国内所在方針を継続。AgentCore の東京対応は公式リージョン表で確認済み（2026-07-10） |
| 生成モデル | `jp.anthropic.claude-sonnet-4-6` | エージェントコードは自前（boto3）なので、ローカル・Runtime どちらでも jp. プロファイルを指定できる。国内完結は従来どおり |
| 方式 | ② 自前 tool use ループ（ローカル）→ ① 同コードを AgentCore Runtime にデプロイ | 実装順は **② → ①**。ローカルで動くものをクラウド運用化する流れ（開発→本番化のストーリー） |
| ツール | (a) コスト照会（Cost Explorer）(b) ログ調査（CloudWatch Logs: invocation logging のロググループ）(c) リソース棚卸し（Resource Groups Tagging API）(d) KB 検索（Retrieve API、Phase 2 資産） | すべて read-only。同一のツール実装を②①で共用する |
| 実行主体と権限 | ②: ローカル実行（利用者ロール bedrock-lab-user がツール権限を持つ）①: Runtime 実行ロール（クラウド側がツール権限を持つ） | **「エージェントのコードをどこで実行し、誰の権限で動かすか」が Phase 3 の中心的な学び**。②→①で権限の置き場所が移動する様子を体験 |
| IAM | Phase 1 の最小権限方針を踏襲 | 利用者ロールに②用ツール権限（ce / logs / tag / Retrieve の read-only）を追加。①用に Runtime 実行ロールを新設（同ツール権限 + モデル呼び出し）。呼び出し側には `bedrock-agentcore:InvokeAgentRuntime` 系を追加 |
| 監査 | invocation logging 継続 | ②のモデル呼び出しは従来どおり記録されるはず。①（Runtime 経由）でどう記録されるか + AgentCore Observability との関係を実機確認（比較観点） |
| コスト | Budgets $10/月を継続 | AgentCore は消費ベース課金（Runtime: vCPU $0.0895/h + メモリ $0.00945/GB-h）。**実行時間課金なので待機中はゼロのはず**（無料枠・最低課金・付随コストは実装前確認）。**注意: Cost Explorer API は $0.01/リクエスト** — ループにステップ上限を設ける |
| IaC | Terraform を基本、AgentCore デプロイは公式手順に従う | AgentCore は starter toolkit（agentcore CLI）+ コンテナ（ECR）のフローが主流の見込み。Terraform の対応状況は実装前確認し、無理に Terraform に寄せない（学習目的では公式フロー体験を優先） |

## 3. 構成（テキスト図 — 実装後に draw.io 化して README へ）

```
方式② 自前 tool use ループ (agent_cli.py / ローカル実行):
  [ユーザー] → [ループ: Converse API (toolConfig 付き)]
                  ├─ stopReason=tool_use → ローカルで boto3 実行 → toolResult 返却 → 再呼び出し
                  │     ツール: get_cost (CE) / search_logs (CWL) / list_resources (Tagging) / search_kb (Retrieve)
                  └─ stopReason=end_turn → 回答表示
  権限: bedrock-lab-user ロール (利用者側が全ツールの鍵を持つ)

方式① AgentCore Runtime (同じエージェントをマネージドランタイムへ):
  [ユーザー] → InvokeAgentRuntime → [AgentCore Runtime (東京) / セッション分離]
                                        └─ [エージェントコンテナ (②と同じループ+ツール)]
                                              ├─ モデル: jp.anthropic.claude-sonnet-4-6
                                              └─ KB Retrieve (Phase 2 資産)
  権限: Runtime 実行ロール (ツールの鍵はクラウド側)
  デプロイ: bedrock-agentcore SDK でラップ → コンテナ化 (ECR) → agentcore CLI

横断: invocation logging / CloudTrail / Budgets (Phase 1 資産) + AgentCore Observability (①)
```

## 4. 実装前に確認すること

1. AgentCore の課金詳細: 無料枠・最低課金の有無、付随コスト（ECR イメージ保管・CodeBuild 等）。「待機中ゼロ」が本当かを確認し、destroy 運用（Runtime 削除）の手順も整理
2. AgentCore Runtime のデプロイフロー実際: starter toolkit（agentcore CLI）の東京での挙動、コンテナビルドの要件（ARM64?）、ECR の扱い
3. Terraform の AgentCore リソース対応状況（対応が薄ければ CLI フロー + 既存資産のみ Terraform で割り切る）
4. InvokeAgentRuntime の呼び出し権限体系と、セッション管理（Phase 2 の sessionId との違い）
5. invocation logging に Runtime 経由のモデル呼び出しが記録されるか（②との差分）
6. Cost Explorer API 課金（$0.01/call）を踏まえたループ上限設計

## 5. 実装タスク分解

| マイルストーン | 内容 |
|---|---|
| M1 | 環境再 apply（Phase 1+2 資産の再構築 + KB 再同期）+ ②用の IAM 拡張（利用者ポリシーに read-only ツール権限） |
| M2 | **方式② 自前 tool use ループ**（`agent_cli.py`）: ツール4種の定義と実装、ループ制御（最大ステップ数）、`--verbose` でツール選択・引数・所要時間を可視化 |
| M3 | **方式① AgentCore Runtime**: M2 のエージェントを bedrock-agentcore SDK でラップ → コンテナ化 → 東京の Runtime にデプロイ → InvokeAgentRuntime クライアントで呼び出し（Observability の見え方も確認） |
| M4 | 2方式で同一の質問セット（単一ツール / KB / マルチステップの3種）を実行し、`docs/agent_comparison.md` に比較記録（レイテンシ・コールドスタート・セッション分離・権限構造・観測性・コスト・デプロイ手間） |
| M5 | README 更新（構成図 draw.io 化）、`phase3` タグ、記事化判断（時事フック: Classic 新規終了 → AgentCore。Phase 2 と同じ「実測比較」構図の続編） |
| M6（おまけ） | 候補: (a) AgentCore Gateway でツールを MCP 化（旬のトピック）(b) Streamlit UI（app.py）にエージェントモード追加。時間と興味で選択 |

## 6. 学習チェックポイント

- tool use のプロトコル（toolConfig / toolUse / toolResult、stopReason）を説明できる
- エージェントループの構成要素（ツール選択 → 実行 → 観察 → 再計画 → 停止条件）と暴走対策（ステップ上限）
- AgentCore の全体像（Runtime / Gateway / Memory / Identity / Observability の役割分担）と Bedrock Agents Classic との違い
- **実行主体と権限の設計**: ツールの鍵を利用者側で持つ（②）かクラウド側の実行ロールに置く（①）かの違いを、インフラの権限設計として説明できる
- Runtime のセッション分離モデルと、コンテナベースのデプロイパイプライン（開発→運用化の流れ）
- read-only ツール設計の考え方と、書き込みを許す場合に何が必要か（承認フロー・Policy・Guardrails）を言語化できる
- マルチステップ実行時のコンテキスト増加（ツール結果が履歴に積まれる）とコストの関係

## 7. Phase 4 以降への接続

- **プロンプトキャッシュ（Phase 4）**: エージェントは毎ターン同じシステムプロンプト + ツール定義を再送するため、キャッシュ効果が最も出やすい構造。Phase 3 の実測トークン数がそのまま Phase 4 の効果測定のベースラインになる
- **AgentCore の残り要素（将来）**: Gateway（MCP）を M6 で触れなかった場合の Phase 3.5 候補。Memory（長期記憶）・Identity（エージェントの認証認可）も企業文脈で価値の高い題材
