# ------------------------------------------------------------
# AWS Budgets - 従量課金の事故防止
#
# アカウント全体の月次コストに対して 3 段階で通知する。
#   1. 実績が予算の 50% 到達
#   2. 実績が予算の 80% 到達
#   3. 予測が予算の 100% 超過見込み
# ------------------------------------------------------------
resource "aws_budgets_budget" "monthly" {
  name        = "${var.project_name}-monthly"
  budget_type = "COST"

  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
