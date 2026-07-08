# ------------------------------------------------------------
# Bedrock model invocation logging
#
# 注意: invocation logging の設定はアカウント x リージョン単位のシングルトン。
# 同一リージョンに既存の設定がある場合、この apply で上書きされる。
# ------------------------------------------------------------

# --- ログ保管用 S3 バケット ---
resource "aws_s3_bucket" "invocation_logs" {
  bucket = "${var.project_name}-invocation-logs-${local.account_id}"
}

resource "aws_s3_bucket_public_access_block" "invocation_logs" {
  bucket = aws_s3_bucket.invocation_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "invocation_logs" {
  bucket = aws_s3_bucket.invocation_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "invocation_logs" {
  bucket = aws_s3_bucket.invocation_logs.id

  rule {
    id     = "expire-old-invocation-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = var.s3_log_expiration_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Bedrock サービスからのログ配信を許可するバケットポリシー
resource "aws_s3_bucket_policy" "invocation_logs" {
  bucket = aws_s3_bucket.invocation_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowBedrockInvocationLogDelivery"
        Effect = "Allow"
        Principal = {
          Service = "bedrock.amazonaws.com"
        }
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.invocation_logs.arn}/AWSLogs/${local.account_id}/BedrockModelInvocationLogs/*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = local.account_id
          }
          ArnLike = {
            "aws:SourceArn" = "arn:aws:bedrock:${var.aws_region}:${local.account_id}:*"
          }
        }
      }
    ]
  })
}

# --- CloudWatch Logs ---
resource "aws_cloudwatch_log_group" "invocation_logs" {
  name              = "/aws/bedrock/${var.project_name}/model-invocations"
  retention_in_days = var.log_retention_days
}

# Bedrock が CloudWatch Logs へ書き込むためのサービスロール
resource "aws_iam_role" "bedrock_logging" {
  name        = "${var.project_name}-bedrock-logging"
  description = "Service role for Bedrock invocation logging to write to CloudWatch Logs"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "bedrock.amazonaws.com"
        }
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = local.account_id
          }
          ArnLike = {
            "aws:SourceArn" = "arn:aws:bedrock:${var.aws_region}:${local.account_id}:*"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "bedrock_logging_cloudwatch" {
  name = "cloudwatch-write"
  role = aws_iam_role.bedrock_logging.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "${aws_cloudwatch_log_group.invocation_logs.arn}:log-stream:*"
      }
    ]
  })
}

# --- invocation logging 本体 ---
resource "aws_bedrock_model_invocation_logging_configuration" "this" {
  logging_config {
    text_data_delivery_enabled      = true
    image_data_delivery_enabled     = false
    embedding_data_delivery_enabled = false
    video_data_delivery_enabled     = false

    cloudwatch_config {
      log_group_name = aws_cloudwatch_log_group.invocation_logs.name
      role_arn       = aws_iam_role.bedrock_logging.arn

      # 100KB を超える入出力は S3 側へ退避される
      large_data_delivery_s3_config {
        bucket_name = aws_s3_bucket.invocation_logs.id
        key_prefix  = "large-data"
      }
    }

    s3_config {
      bucket_name = aws_s3_bucket.invocation_logs.id
    }
  }

  depends_on = [
    aws_s3_bucket_policy.invocation_logs,
    aws_iam_role_policy.bedrock_logging_cloudwatch,
  ]
}
