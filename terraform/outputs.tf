output "bedrock_user_role_arn" {
  description = "開発端末から assume する利用者ロールの ARN"
  value       = aws_iam_role.bedrock_lab_user.arn
}

output "bedrock_invoke_policy_arn" {
  description = "呼び出し用最小権限ポリシーの ARN (SSO Permission Set 等へのアタッチ用)"
  value       = aws_iam_policy.bedrock_invoke.arn
}

output "invocation_log_bucket" {
  description = "invocation log 保管用 S3 バケット名"
  value       = aws_s3_bucket.invocation_logs.id
}

output "invocation_log_group" {
  description = "invocation log の CloudWatch Logs グループ名"
  value       = aws_cloudwatch_log_group.invocation_logs.name
}

output "kb_id" {
  description = "Knowledge Base の ID (Retrieve / RetrieveAndGenerate で使用)"
  value       = aws_bedrockagent_knowledge_base.main.id
}

output "kb_source_bucket" {
  description = "RAG 原本ドキュメント格納バケット名"
  value       = aws_s3_bucket.kb_source.id
}

output "kb_data_source_id" {
  description = "KB データソース ID (同期ジョブ起動で使用)"
  value       = aws_bedrockagent_data_source.articles.data_source_id
}

output "vector_index_arn" {
  description = "S3 Vectors インデックス ARN (自前ミニ RAG で使用)"
  value       = aws_s3vectors_index.kb.index_arn
}
