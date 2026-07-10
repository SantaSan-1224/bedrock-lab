# ------------------------------------------------------------
# Phase 3 M3: AgentCore Runtime (方式①: マネージドランタイム)
#
# M2 の自前 tool use ループを直接コードデプロイ (S3 zip) で載せる。
# 事前に scripts/build_runtime_zip.sh で zip を生成しておくこと。
#
# 方式② (ローカル) と同じツール権限を Runtime 実行ロール側に持たせる。
# 「ツールの鍵を利用者が持つか、クラウド側の実行ロールに置くか」が
# 2方式の比較の中心 (計画書 §2)。
# ------------------------------------------------------------

# --- デプロイパッケージ置き場 ---
resource "aws_s3_bucket" "agent_code" {
  bucket        = "${var.project_name}-agent-code-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "agent_code" {
  bucket                  = aws_s3_bucket.agent_code.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "agent_package" {
  bucket = aws_s3_bucket.agent_code.id
  key    = "ops-agent/deployment_package.zip"
  source = "${path.module}/../build/runtime/deployment_package.zip"
  # zip が変わったら Runtime を更新させる
  etag = filemd5("${path.module}/../build/runtime/deployment_package.zip")
}

# --- Runtime 実行ロール ---
resource "aws_iam_role" "agent_runtime" {
  name        = "${var.project_name}-agent-runtime"
  description = "Execution role for the ops agent on AgentCore Runtime"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "bedrock-agentcore.amazonaws.com" }
        Action    = "sts:AssumeRole"
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.account_id }
        }
      }
    ]
  })
}

# モデル呼び出し + KB Retrieve + S3 Vectors (利用者ロールと同じポリシーを共有)
resource "aws_iam_role_policy_attachment" "agent_runtime_invoke" {
  role       = aws_iam_role.agent_runtime.name
  policy_arn = aws_iam_policy.bedrock_invoke.arn
}

# read-only ツール権限 (方式②の利用者ロールと同一 — 権限の置き場所だけが違う)
resource "aws_iam_role_policy_attachment" "agent_runtime_tools" {
  role       = aws_iam_role.agent_runtime.name
  policy_arn = aws_iam_policy.agent_tools_readonly.arn
}

# Runtime 固有: デプロイコードの読み取り + 自身のログ出力
resource "aws_iam_role_policy" "agent_runtime_infra" {
  name = "runtime-infra"
  role = aws_iam_role.agent_runtime.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadDeploymentPackage"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${aws_s3_bucket.agent_code.arn}/*"]
      },
      {
        Sid    = "WriteRuntimeLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
        ]
        Resource = ["arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/bedrock-agentcore/*"]
      },
    ]
  })
}

# --- Runtime 本体 (直接コードデプロイ) ---
resource "aws_bedrockagentcore_agent_runtime" "ops_agent" {
  agent_runtime_name = "bedrock_lab_ops_agent"
  role_arn           = aws_iam_role.agent_runtime.arn

  agent_runtime_artifact {
    code_configuration {
      entry_point = ["agent_runtime.py"]
      runtime     = "PYTHON_3_13"
      code {
        s3 {
          bucket = aws_s3_bucket.agent_code.id
          prefix = aws_s3_object.agent_package.key
        }
      }
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  lifecycle_configuration {
    idle_runtime_session_timeout = 300  # 5分アイドルでセッション終了
    max_lifetime                 = 1800 # セッション最長30分
  }

  depends_on = [aws_iam_role_policy.agent_runtime_infra]
}

# --- 利用者ロールから Runtime を呼び出す権限 ---
resource "aws_iam_policy" "invoke_agent_runtime" {
  name        = "${var.project_name}-invoke-agent-runtime"
  description = "Allow invoking the ops agent on AgentCore Runtime"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "InvokeOpsAgent"
        Effect = "Allow"
        Action = ["bedrock-agentcore:InvokeAgentRuntime"]
        Resource = [
          aws_bedrockagentcore_agent_runtime.ops_agent.agent_runtime_arn,
          "${aws_bedrockagentcore_agent_runtime.ops_agent.agent_runtime_arn}/*",
        ]
      },
      {
        Sid      = "DiscoverRuntimes"
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:ListAgentRuntimes"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "bedrock_lab_user_invoke_runtime" {
  role       = aws_iam_role.bedrock_lab_user.name
  policy_arn = aws_iam_policy.invoke_agent_runtime.arn
}

output "agent_runtime_arn" {
  value       = aws_bedrockagentcore_agent_runtime.ops_agent.agent_runtime_arn
  description = "AgentCore Runtime の ARN (invoke_runtime.py が参照)"
}
