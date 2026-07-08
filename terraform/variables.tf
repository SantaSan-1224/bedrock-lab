variable "aws_region" {
  description = "Bedrock を呼び出すソースリージョン"
  type        = string
  default     = "ap-northeast-1"
}

variable "aws_profile" {
  description = "AWS CLI プロファイル名 (空文字ならデフォルトの資格情報チェーンを使用)"
  type        = string
  default     = ""
}

variable "project_name" {
  description = "リソース名のプレフィックス"
  type        = string
  default     = "bedrock-lab"
}

variable "alert_email" {
  description = "Budgets アラートの通知先メールアドレス (必須)"
  type        = string

  validation {
    condition     = can(regex("^[^@]+@[^@]+$", var.alert_email))
    error_message = "alert_email はメールアドレス形式で指定してください。"
  }
}

variable "monthly_budget_usd" {
  description = "月次コスト予算 (USD)。アカウント全体のコストに対するアラート閾値"
  type        = number
  default     = 20
}

variable "log_retention_days" {
  description = "CloudWatch Logs (invocation log) の保持日数"
  type        = number
  default     = 30
}

variable "s3_log_expiration_days" {
  description = "S3 上の invocation log の保持日数 (ライフサイクルで削除)"
  type        = number
  default     = 90
}
