#!/usr/bin/env bash
set -euo pipefail

# Temp files are cleaned up on any exit path (including `set -e` aborts).
STATUS_BODY_FILE=""
APPROVED_BODY_FILE=""
trap 'rm -f "$STATUS_BODY_FILE" "$APPROVED_BODY_FILE"' EXIT

# Defensive defaults so `set -u` never trips on an optional/unset input.
POSSIBLE_ISSUE_REFERENCE="${POSSIBLE_ISSUE_REFERENCE:-}"
JIRA_CHECK_CONTEXT="${JIRA_CHECK_CONTEXT:-}"
DEFAULT_HOTFIX_PREFIX="${DEFAULT_HOTFIX_PREFIX:-}"
DEFAULT_REVERT_PREFIX="${DEFAULT_REVERT_PREFIX:-}"
BRANCH_NAME="${BRANCH_NAME:-}"
CHECK_JIRA_VALID_PROJECT_PREFIXES="${CHECK_JIRA_VALID_PROJECT_PREFIXES:-True}"
JIRA_VALID_PROJECT_PREFIXES="${JIRA_VALID_PROJECT_PREFIXES:-}"
APPROVED_FIELD_FOR_DEVELOPMENT_BY_FIELD_IS_EMPTY="${APPROVED_FIELD_FOR_DEVELOPMENT_BY_FIELD_IS_EMPTY:-}"

# Issue keys start with a letter (e.g. SHS-491); avoids false positives like "2-3".
ISSUE_KEY=$(echo "$POSSIBLE_ISSUE_REFERENCE" | grep -oiE '[a-z][a-z0-9]{1,9}-[0-9]+' | head -n 1 || true)
echo "ISSUE_KEY=$ISSUE_KEY" >> "$GITHUB_ENV"

if [[ -n "$JIRA_CHECK_CONTEXT" ]]; then
  echo "$JIRA_CHECK_CONTEXT"
fi

echo "DEFAULT_HOTFIX_PREFIX [$DEFAULT_HOTFIX_PREFIX]"
echo "DEFAULT_REVERT_PREFIX [$DEFAULT_REVERT_PREFIX]"
echo "BRANCH_NAME [$BRANCH_NAME]"
echo "JIRA_VALID_PROJECT_PREFIXES [$JIRA_VALID_PROJECT_PREFIXES]"
echo "ISSUE_KEY [$ISSUE_KEY]"

if [[ -n "$DEFAULT_HOTFIX_PREFIX" && "$BRANCH_NAME" == "$DEFAULT_HOTFIX_PREFIX"* ]]; then
  echo "PREFIX_TO_IGNORE_FOUND=1" >> "$GITHUB_ENV"
  echo "Hotfix branch detected, skipping Jira issue status check."
  exit 0
fi

if [[ -n "$DEFAULT_REVERT_PREFIX" && "$BRANCH_NAME" == "$DEFAULT_REVERT_PREFIX"* ]]; then
  echo "PREFIX_TO_IGNORE_FOUND=1" >> "$GITHUB_ENV"
  echo "Revert branch detected, skipping Jira issue status check."
  exit 0
fi

# Fail-secure: only explicit off-values disable the check; anything else keeps it on.
# `tr` (not ${x,,}) keeps this portable to bash 3.2 (e.g. macOS runners).
CHECK_PREFIXES_LOWER=$(printf '%s' "$CHECK_JIRA_VALID_PROJECT_PREFIXES" | tr '[:upper:]' '[:lower:]')
case "$CHECK_PREFIXES_LOWER" in
  false|0|no|off)
    echo "Project prefix validation disabled (CHECK_JIRA_VALID_PROJECT_PREFIXES=$CHECK_JIRA_VALID_PROJECT_PREFIXES)."
    ;;
  *)
    if [[ -z "$JIRA_VALID_PROJECT_PREFIXES" ]]; then
      echo "JIRA_VALID_PROJECT_PREFIXES is empty. Please configure the valid Jira project prefixes."
      exit 1
    fi

    if [[ -n "$ISSUE_KEY" ]]; then
      ISSUE_PREFIX=$(echo "$ISSUE_KEY" | cut -d'-' -f1 | tr '[:lower:]' '[:upper:]')
      IFS=',' read -ra VALID_PREFIXES <<< "$JIRA_VALID_PROJECT_PREFIXES"
      PREFIX_IS_VALID=0
      for PREFIX in "${VALID_PREFIXES[@]}"; do
        # Trim surrounding whitespace, then upper-case (tr for bash 3.2 portability).
        TRIMMED_PREFIX="${PREFIX#"${PREFIX%%[![:space:]]*}"}"
        TRIMMED_PREFIX="${TRIMMED_PREFIX%"${TRIMMED_PREFIX##*[![:space:]]}"}"
        TRIMMED_PREFIX=$(printf '%s' "$TRIMMED_PREFIX" | tr '[:lower:]' '[:upper:]')
        if [[ "$ISSUE_PREFIX" == "$TRIMMED_PREFIX" ]]; then
          PREFIX_IS_VALID=1
          break
        fi
      done
      if [[ "$PREFIX_IS_VALID" == "0" ]]; then
        echo "The space '$ISSUE_PREFIX' is not recognized as valid in the SOX workflow."
        exit 1
      fi
      echo "Space '$ISSUE_PREFIX' recognized as valid in the SOX workflow."
    fi
    ;;
esac

echo "Searching for Jira issue $ISSUE_KEY at $JIRA_BASE_URL"
JIRA_API_ISSUE_ENDPOINT="$JIRA_BASE_URL/rest/api/2/issue/$ISSUE_KEY?fields=key,status"
echo "Requesting issue at $JIRA_API_ISSUE_ENDPOINT"

STATUS_BODY_FILE="$(mktemp)"
STATUS_HTTP_CODE=$(curl -sS --max-time 30 --retry 2 --retry-connrefused \
  --request GET --url "$JIRA_API_ISSUE_ENDPOINT" \
  --user "${JIRA_USER_EMAIL}:${JIRA_API_TOKEN}" \
  --write-out '%{http_code}' --output "$STATUS_BODY_FILE") || {
    echo "Network error while contacting Jira at $JIRA_BASE_URL."
    exit 1
  }

# Issue absent in this Jira: not an error, let the caller try the alternative Jira.
if [[ "$STATUS_HTTP_CODE" == "404" ]]; then
  echo "Issue $ISSUE_KEY not found at $JIRA_BASE_URL (HTTP 404)."
  exit 0
fi

if [[ "$STATUS_HTTP_CODE" -lt 200 || "$STATUS_HTTP_CODE" -ge 300 ]]; then
  echo "Unexpected HTTP status $STATUS_HTTP_CODE from Jira at $JIRA_BASE_URL."
  cat "$STATUS_BODY_FILE"
  exit 1
fi

JIRA_RESPONSE_STATUS=$(jq -r '.fields.status.name' < "$STATUS_BODY_FILE")
echo "Response Status [$JIRA_RESPONSE_STATUS]"
echo "JIRA_STATUS_ALLOWED_TO_MERGE -> [$JIRA_STATUS_ALLOWED_TO_MERGE]"
echo "JIRA_RESPONSE_STATUS=$JIRA_RESPONSE_STATUS" >> "$GITHUB_ENV"

ISSUE_KEY_FOUND=0
if [[ "$JIRA_RESPONSE_STATUS" != "null" ]]; then
  ISSUE_KEY_FOUND=1
  echo "ISSUE_KEY_FOUND=1" >> "$GITHUB_ENV"
  echo "Issue key found!"
fi

# Only validate the responsible field when the card actually exists in THIS Jira,
# otherwise the alternative-Jira flow would abort here on a non-existent issue.
if [[ "$ISSUE_KEY_FOUND" == 1 && -n "$APPROVED_FIELD_FOR_DEVELOPMENT_BY_FIELD_IS_EMPTY" ]]; then
  echo "Checking approved for development field is empty ($APPROVED_FIELD_FOR_DEVELOPMENT_BY_FIELD_IS_EMPTY)"
  APPROVED_FIELD_ENDPOINT="$JIRA_BASE_URL/rest/api/2/issue/$ISSUE_KEY?fields=$APPROVED_FIELD_FOR_DEVELOPMENT_BY_FIELD_IS_EMPTY"
  echo "Requesting issue at $APPROVED_FIELD_ENDPOINT"

  APPROVED_BODY_FILE="$(mktemp)"
  APPROVED_HTTP_CODE=$(curl -sS --max-time 30 --retry 2 --retry-connrefused \
    --request GET --url "$APPROVED_FIELD_ENDPOINT" \
    --user "${JIRA_USER_EMAIL}:${JIRA_API_TOKEN}" \
    --write-out '%{http_code}' --output "$APPROVED_BODY_FILE") || {
      echo "Network error while contacting Jira at $JIRA_BASE_URL."
      exit 1
    }

  if [[ "$APPROVED_HTTP_CODE" -lt 200 || "$APPROVED_HTTP_CODE" -ge 300 ]]; then
    echo "Unexpected HTTP status $APPROVED_HTTP_CODE from Jira at $JIRA_BASE_URL."
    cat "$APPROVED_BODY_FILE"
    exit 1
  fi

  # Pass the field name as data (--arg) instead of interpolating it into the jq
  # program: avoids jq injection and handles field names with special chars.
  JIRA_RESPONSE_RESPONSIBLE_FIELD=$(jq -r --arg f "$APPROVED_FIELD_FOR_DEVELOPMENT_BY_FIELD_IS_EMPTY" '.fields[$f]' < "$APPROVED_BODY_FILE")
  echo "Response field value = '$JIRA_RESPONSE_RESPONSIBLE_FIELD'"

  if [[ "$JIRA_RESPONSE_RESPONSIBLE_FIELD" != "null" ]]; then
    echo "Approved for development by field isn't empty."
  else
    echo "Approved for development by field is empty."
    exit 1
  fi
fi
