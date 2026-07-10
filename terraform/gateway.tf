# ------------------------------------------------------------
# Phase 3 M6: AgentCore Gateway — ツールの MCP 化
#
# agent_cli.py のツール4種を Lambda ターゲットとして Gateway に載せ、
# MCP (Model Context Protocol) を話す任意のクライアントから使えるようにする。
# インバウンド認証は IAM (SigV4) — Cognito/JWT のセットアップは不要。
# ------------------------------------------------------------

# --- ツール Lambda (agent_cli.py のツール実装を同梱) ---
data "archive_file" "gateway_tools" {
  type        = "zip"
  output_path = "${path.module}/../build/gateway_tools.zip"

  source {
    content  = file("${path.module}/../lambda/gateway_tools_handler.py")
    filename = "gateway_tools_handler.py"
  }
  source {
    content  = file("${path.module}/../client/agent_cli.py")
    filename = "agent_cli.py"
  }
}

resource "aws_iam_role" "gateway_tools_lambda" {
  name        = "${var.project_name}-gateway-tools-lambda"
  description = "Execution role for the MCP tool Lambda behind AgentCore Gateway"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
}

# ツール権限は方式①②と同じポリシーを共有 (3つ目の「鍵束の持ち主」)
resource "aws_iam_role_policy_attachment" "gateway_tools_lambda_tools" {
  role       = aws_iam_role.gateway_tools_lambda.name
  policy_arn = aws_iam_policy.agent_tools_readonly.arn
}

resource "aws_iam_role_policy" "gateway_tools_lambda_extra" {
  name = "kb-retrieve-and-logging"
  role = aws_iam_role.gateway_tools_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # search_kb ツール用 (モデル呼び出しは不要なので bedrock_invoke は付けない)
        Sid      = "SearchKb"
        Effect   = "Allow"
        Action   = ["bedrock:Retrieve"]
        Resource = ["arn:aws:bedrock:${var.aws_region}:${local.account_id}:knowledge-base/*"]
      },
      {
        Sid    = "LambdaLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = ["arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/lambda/*"]
      },
    ]
  })
}

resource "aws_lambda_function" "gateway_tools" {
  function_name    = "${var.project_name}-gateway-tools"
  role             = aws_iam_role.gateway_tools_lambda.arn
  runtime          = "python3.13"
  handler          = "gateway_tools_handler.handler"
  filename         = data.archive_file.gateway_tools.output_path
  source_code_hash = data.archive_file.gateway_tools.output_base64sha256
  timeout          = 30
  memory_size      = 256

  environment {
    variables = {
      KB_ID = aws_bedrockagent_knowledge_base.main.id
    }
  }
}

# --- Gateway 実行ロール (ターゲット Lambda を呼ぶ) ---
resource "aws_iam_role" "gateway" {
  name        = "${var.project_name}-gateway"
  description = "Service role for AgentCore Gateway to invoke Lambda targets"

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

resource "aws_iam_role_policy" "gateway_invoke_lambda" {
  name = "invoke-tool-lambda"
  role = aws_iam_role.gateway.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = [aws_lambda_function.gateway_tools.arn]
      }
    ]
  })
}

# --- Gateway 本体 (MCP / IAM 認証) ---
resource "aws_bedrockagentcore_gateway" "ops_tools" {
  name            = "${var.project_name}-ops-tools"
  role_arn        = aws_iam_role.gateway.arn
  authorizer_type = "AWS_IAM"
  protocol_type   = "MCP"
}

# --- ターゲット: ツール4種 (同一 Lambda、ツールごとにスキーマ定義) ---
resource "aws_bedrockagentcore_gateway_target" "ops_tools" {
  gateway_identifier = aws_bedrockagentcore_gateway.ops_tools.gateway_id
  name               = "ops"
  description        = "Read-only AWS ops tools (cost / logs / inventory / knowledge base)"

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = aws_lambda_function.gateway_tools.arn

        tool_schema {
          inline_payload {
            name        = "get_cost"
            description = "AWS のコストを Cost Explorer で照会する。期間指定がなければ今月分。サービス別内訳を取得できる。1リクエスト $0.01。"
            input_schema {
              type = "object"
              property {
                name        = "start_date"
                type        = "string"
                description = "開始日 YYYY-MM-DD (含む)。省略時は今月1日"
              }
              property {
                name        = "end_date"
                type        = "string"
                description = "終了日 YYYY-MM-DD (含まない)。省略時は明日"
              }
              property {
                name        = "group_by_service"
                type        = "boolean"
                description = "サービス別に分けるか。既定 true"
              }
            }
          }

          inline_payload {
            name        = "search_logs"
            description = "Bedrock invocation logging の CloudWatch Logs を検索する。モデル呼び出し履歴・エラー調査に使う。"
            input_schema {
              type = "object"
              property {
                name        = "filter_pattern"
                type        = "string"
                description = "CloudWatch Logs のフィルタパターン。省略時は全件"
              }
              property {
                name        = "hours"
                type        = "integer"
                description = "何時間前まで遡るか。既定 24"
              }
              property {
                name        = "max_events"
                type        = "integer"
                description = "取得する最大イベント数。既定 10"
              }
            }
          }

          inline_payload {
            name        = "list_resources"
            description = "アカウント内のリソースを Resource Groups Tagging API で棚卸しする。サービス別件数と ARN 一覧を返す。"
            input_schema {
              type = "object"
            }
          }

          inline_payload {
            name        = "search_kb"
            description = "個人の技術ナレッジベース (本人の記事・構築メモ) を検索する。AWS のエラー対処・過去の検証・設計判断を調べる。"
            input_schema {
              type = "object"
              property {
                name        = "query"
                type        = "string"
                description = "検索クエリ (日本語可)"
                required    = true
              }
              property {
                name        = "top_k"
                type        = "integer"
                description = "取得件数。既定 3"
              }
            }
          }
        }
      }
    }
  }
}

# --- 利用者ロールから Gateway を呼ぶ権限 ---
resource "aws_iam_policy" "invoke_gateway" {
  name        = "${var.project_name}-invoke-gateway"
  description = "Allow invoking the ops tools MCP gateway"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeOpsGateway"
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:InvokeGateway"]
        Resource = [aws_bedrockagentcore_gateway.ops_tools.gateway_arn]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "bedrock_lab_user_invoke_gateway" {
  role       = aws_iam_role.bedrock_lab_user.name
  policy_arn = aws_iam_policy.invoke_gateway.arn
}

output "gateway_mcp_url" {
  value       = aws_bedrockagentcore_gateway.ops_tools.gateway_url
  description = "MCP エンドポイント URL (mcp_gateway_client.py が参照)"
}
