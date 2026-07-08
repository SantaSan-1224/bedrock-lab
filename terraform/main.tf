data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id

  # データ所在を日本国内に限定する方針:
  #   - 呼び出し対象は JP cross-region inference profile (jp.anthropic.*) のみ
  #   - JP プロファイルのルーティング先は 東京 (ap-northeast-1) / 大阪 (ap-northeast-3) の 2 リージョン
  # cross-region inference の IAM は「プロファイル ARN + 全ルーティング先リージョンの
  # foundation-model ARN」の両方を許可する必要がある。
  bedrock_invoke_resources = [
    "arn:aws:bedrock:${var.aws_region}:${local.account_id}:inference-profile/jp.anthropic.*",
    "arn:aws:bedrock:ap-northeast-1::foundation-model/anthropic.*",
    "arn:aws:bedrock:ap-northeast-3::foundation-model/anthropic.*",
  ]
}
