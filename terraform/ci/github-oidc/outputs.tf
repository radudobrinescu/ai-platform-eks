output "role_arn" {
  description = "Set this as the GitHub repository variable E2E_ROLE_ARN."
  value       = aws_iam_role.e2e.arn
}

output "trusted_subject" {
  description = "The exact OIDC subject the trust policy accepts."
  value       = local.subject
}
