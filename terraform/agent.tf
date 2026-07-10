# ------------------------------------------------------------
# Phase 3: AWS 運用エージェント用のツール権限 (方式②: ローカル実行)
#
# エージェントに持たせるのは read-only (照会系) のみ。
# 書き込み系の権限は一切付与しない (計画書 §0 の安全方針)。
#
# 方式② (自前 tool use ループ) ではツールをローカルで実行するため、
# 利用者ロール bedrock-lab-user 側にツール権限を追加する。
# 方式① (AgentCore Runtime) では Runtime 実行ロール側に同等権限を
# 持たせる (M3 で追加)。権限の置き場所の違いが Phase 3 の学びの中心。
# ------------------------------------------------------------
resource "aws_iam_policy" "agent_tools_readonly" {
  name        = "${var.project_name}-agent-tools-readonly"
  description = "Read-only tool permissions for the ops agent (Cost Explorer / CW Logs / Tagging API)"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # コスト照会ツール。注意: Cost Explorer API は $0.01/リクエスト。
        # エージェントループ側でステップ上限を設けて連打を防ぐ
        Sid      = "ToolCostExplorer"
        Effect   = "Allow"
        Action   = ["ce:GetCostAndUsage"]
        Resource = "*" # CE はリソースレベル制約非対応
      },
      {
        # ログ調査ツール。invocation logging のロググループに限定
        Sid    = "ToolLogsSearch"
        Effect = "Allow"
        Action = [
          "logs:FilterLogEvents",
          "logs:GetLogEvents",
        ]
        Resource = [
          aws_cloudwatch_log_group.invocation_logs.arn,
          "${aws_cloudwatch_log_group.invocation_logs.arn}:log-stream:*",
        ]
      },
      {
        # ロググループの発見用 (Describe はワイルドカードが必要)
        Sid      = "ToolLogsDiscover"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = "*"
      },
      {
        # リソース棚卸しツール (Resource Groups Tagging API)
        Sid      = "ToolResourceInventory"
        Effect   = "Allow"
        Action   = ["tag:GetResources"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "bedrock_lab_user_agent_tools" {
  role       = aws_iam_role.bedrock_lab_user.name
  policy_arn = aws_iam_policy.agent_tools_readonly.arn
}
