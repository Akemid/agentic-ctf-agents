#!/usr/bin/env bash
# Task 0.1 (DAY 0): SageMaker endpoint quota for ml.g4dn.xlarge (L-B67CFA0C, us-east-2).
# Status: request 9cf62dab75704e41a9648bd55ce941ffvCArSmVu APPROVED on 2026-10-14; value is 1.0.
#
# Usage:  bash spikes/s0_request_quota.sh            # default: read-only status
#         bash spikes/s0_request_quota.sh status     # same as above
#         bash spikes/s0_request_quota.sh request    # MUTATING: file an increase request (explicit opt-in)
# `request` is skipped when the current value already meets the desired value or a
# PENDING/CASE_OPENED request exists.
set -euo pipefail

REGION="${AWS_REGION:-us-east-2}"
QUOTA_CODE="L-B67CFA0C"   # "ml.g4dn.xlarge for endpoint usage"
DESIRED=1
CMD="${1:-status}"

show_status() {
  aws service-quotas get-service-quota --service-code sagemaker --quota-code "$QUOTA_CODE" \
    --region "$REGION" --query 'Quota.[QuotaName,Value]' --output text
  aws service-quotas list-requested-service-quota-change-history-by-quota \
    --service-code sagemaker --quota-code "$QUOTA_CODE" --region "$REGION" \
    --query 'RequestedQuotas[].[CaseId,Status,DesiredValue,Created]' --output table
}

case "$CMD" in
  status)
    show_status
    ;;
  request)
    CURRENT="$(aws service-quotas get-service-quota --service-code sagemaker --quota-code "$QUOTA_CODE" \
      --region "$REGION" --query 'Quota.Value' --output text)"
    if awk -v c="$CURRENT" -v d="$DESIRED" 'BEGIN { exit !(c >= d) }'; then
      echo "Skip: current value $CURRENT >= desired $DESIRED; nothing to request."
      exit 0
    fi
    OPEN="$(aws service-quotas list-requested-service-quota-change-history-by-quota \
      --service-code sagemaker --quota-code "$QUOTA_CODE" --region "$REGION" \
      --query "RequestedQuotas[?Status=='PENDING' || Status=='CASE_OPENED'].CaseId" --output text)"
    if [[ -n "$OPEN" && "$OPEN" != "None" ]]; then
      echo "Skip: a request is already open ($OPEN)."
      exit 0
    fi
    aws service-quotas request-service-quota-increase \
      --service-code sagemaker --quota-code "$QUOTA_CODE" --desired-value "$DESIRED" --region "$REGION" \
      --query 'RequestedQuota.[CaseId,Status,DesiredValue]' --output text
    echo "Filed. Re-run with 'status' until the applied value is >= $DESIRED."
    ;;
  *)
    echo "usage: $0 [status|request]" >&2
    exit 2
    ;;
esac
