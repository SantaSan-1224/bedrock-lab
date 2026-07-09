# ------------------------------------------------------------
# Phase 2: RAG (Knowledge Bases + S3 Vectors)
#
# 原本 (S3) → KB 同期 (チャンキング → Titan V2 埋め込み) → S3 Vectors
# ベクトルストアに S3 Vectors を使うことで固定費ゼロを維持する。
# ------------------------------------------------------------

# --- 原本ドキュメント格納バケット ---
resource "aws_s3_bucket" "kb_source" {
  bucket = "${var.project_name}-kb-source-${local.account_id}"
}

resource "aws_s3_bucket_public_access_block" "kb_source" {
  bucket = aws_s3_bucket.kb_source.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "kb_source" {
  bucket = aws_s3_bucket.kb_source.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# --- S3 Vectors (ベクトルストア) ---
resource "aws_s3vectors_vector_bucket" "kb" {
  vector_bucket_name = "${var.project_name}-vectors"
  # 学習環境: destroy 時にベクトルごと削除できるようにする
  force_destroy = true
}

resource "aws_s3vectors_index" "kb" {
  vector_bucket_name = aws_s3vectors_vector_bucket.kb.vector_bucket_name
  index_name         = "${var.project_name}-kb-index"
  data_type          = "float32"
  dimension          = 1024 # Titan Text Embeddings V2 の既定次元
  distance_metric    = "cosine"

  metadata_configuration {
    # KB はチャンク本文 (AMAZON_BEDROCK_TEXT) と内部管理 JSON (AMAZON_BEDROCK_METADATA) を
    # ベクトルごとのメタデータに格納する。両方を non-filterable に指定するのが公式要件。
    # 片方でも filterable 側に残すと「Filterable metadata must have at most 2048 bytes」で
    # 同期が失敗する (日本語チャンクは UTF-8 で膨らむため特に顕在化しやすい)。
    # あわせてカスタムメタデータは filterable/non-filterable 合算で 1KB/ベクトルの制限あり。
    non_filterable_metadata_keys = ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]
  }
}

# --- KB 用サービスロール ---
resource "aws_iam_role" "kb_service" {
  name        = "${var.project_name}-kb-service"
  description = "Service role for Bedrock Knowledge Base (S3 read / embedding / S3 Vectors access)"

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
            "aws:SourceArn" = "arn:aws:bedrock:${var.aws_region}:${local.account_id}:knowledge-base/*"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "kb_service" {
  name = "kb-access"
  role = aws_iam_role.kb_service.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadSourceDocuments"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          aws_s3_bucket.kb_source.arn,
          "${aws_s3_bucket.kb_source.arn}/*",
        ]
      },
      {
        Sid      = "InvokeEmbeddingModel"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = ["arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.titan-embed-text-v2:0"]
      },
      {
        Sid    = "AccessVectorIndex"
        Effect = "Allow"
        Action = [
          "s3vectors:GetIndex",
          "s3vectors:PutVectors",
          "s3vectors:GetVectors",
          "s3vectors:QueryVectors",
          "s3vectors:DeleteVectors",
          "s3vectors:ListVectors",
        ]
        Resource = [aws_s3vectors_index.kb.index_arn]
      },
    ]
  })
}

# --- Knowledge Base 本体 ---
resource "aws_bedrockagent_knowledge_base" "main" {
  name        = "${var.project_name}-kb"
  description = "Personal ops-knowledge search over published articles and lab notes"
  role_arn    = aws_iam_role.kb_service.arn

  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = "arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.titan-embed-text-v2:0"
      embedding_model_configuration {
        bedrock_embedding_model_configuration {
          dimensions          = 1024
          embedding_data_type = "FLOAT32"
        }
      }
    }
  }

  storage_configuration {
    type = "S3_VECTORS"
    s3_vectors_configuration {
      index_arn = aws_s3vectors_index.kb.index_arn
    }
  }

  depends_on = [aws_iam_role_policy.kb_service]
}

# --- データソース (S3 原本) ---
resource "aws_bedrockagent_data_source" "articles" {
  knowledge_base_id = aws_bedrockagent_knowledge_base.main.id
  name              = "articles"
  # 学習環境: データソース削除時にベクトルストア側のデータも削除
  data_deletion_policy = "DELETE"

  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn = aws_s3_bucket.kb_source.arn
    }
  }

  # チャンキングは KB デフォルト (固定 300 トークン / オーバーラップ 20%) でスタート。
  # 検索品質に応じて M2 以降で vector_ingestion_configuration による戦略比較を検討
}
