# ------------------------------------------------------------
# 呼び出し用の最小権限ポリシー
#
# 注意: Converse / ConverseStream API に "bedrock:Converse" という
# IAM アクションは存在しない。Converse API の呼び出し権限は
# bedrock:InvokeModel / bedrock:InvokeModelWithResponseStream でカバーされる。
# ------------------------------------------------------------
resource "aws_iam_policy" "bedrock_invoke" {
  name        = "${var.project_name}-invoke"
  description = "Least-privilege access to invoke Claude on Bedrock via JP inference profiles only"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "InvokeClaudeViaJapanProfileOnly"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ]
        Resource = local.bedrock_invoke_resources
      },
      {
        Sid    = "DiscoverModelsAndProfiles"
        Effect = "Allow"
        Action = [
          "bedrock:ListFoundationModels",
          "bedrock:GetFoundationModel",
          "bedrock:ListInferenceProfiles",
          "bedrock:GetInferenceProfile",
          "bedrock:ListKnowledgeBases",
        ]
        Resource = "*"
      },
      {
        # Phase 2: Knowledge Base への検索 (Retrieve) と検索+生成 (RetrieveAndGenerate)。
        # RetrieveAndGenerate の生成側は既存の InvokeClaudeViaJapanProfileOnly でカバーされる
        Sid    = "QueryKnowledgeBase"
        Effect = "Allow"
        Action = [
          "bedrock:Retrieve",
          "bedrock:RetrieveAndGenerate",
        ]
        Resource = ["arn:aws:bedrock:${var.aws_region}:${local.account_id}:knowledge-base/*"]
      },
      {
        # Phase 2 M4: 自前ミニ RAG (方式③) 用。
        # KB を経由しないため、埋め込みモデルとベクトルインデックスへの直接権限が必要になる。
        # ①(RetrieveAndGenerate) → ③(自前) の順に必要権限が広がる点は方式比較の観点の1つ
        Sid      = "MiniRagEmbedding"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = ["arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.titan-embed-text-v2:0"]
      },
      {
        Sid    = "MiniRagVectorQuery"
        Effect = "Allow"
        Action = [
          "s3vectors:QueryVectors",
          # QueryVectors で returnMetadata=true を使う場合、GetVectors も必要 (実測)
          "s3vectors:GetVectors",
          "s3vectors:GetIndex",
        ]
        Resource = [aws_s3vectors_index.kb.index_arn]
      },
    ]
  })
}

# ------------------------------------------------------------
# 開発端末から assume する専用ロール
#
# 同一アカウント内のプリンシパル (IAM ユーザー / SSO ロール) からのみ
# assume 可能。長期アクセスキーを配らず、一時クレデンシャルで利用する。
# IAM Identity Center を使う場合は、Permission Set に
# aws_iam_policy.bedrock_invoke をアタッチする構成でもよい (README 参照)。
# ------------------------------------------------------------
resource "aws_iam_role" "bedrock_lab_user" {
  name                 = "${var.project_name}-user"
  description          = "User role for the Bedrock lab environment (least privilege)"
  max_session_duration = 3600

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${local.account_id}:root"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "bedrock_lab_user_invoke" {
  role       = aws_iam_role.bedrock_lab_user.name
  policy_arn = aws_iam_policy.bedrock_invoke.arn
}
