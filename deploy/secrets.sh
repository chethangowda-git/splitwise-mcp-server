#!/bin/sh
# Store the server's secrets as encrypted SSM SecureString parameters under /splitwise-mcp/
# (CloudFormation can't create SecureStrings, so template.yaml only reads them).
#
# Reads SPLITWISE_CLIENT_ID, SPLITWISE_CLIENT_SECRET and (optionally) REDIS_URL from .env.
# Creates SERVER_SECRET once and never replaces it (that would sign everyone out).
# Never prints a secret. Safe to re-run. Optional env: SSM_PATH, AWS_REGION, AWS_PROFILE.
set -eu

root="$(cd "$(dirname "$0")/.." && pwd)"
SSM_PATH="${SSM_PATH:-/splitwise-mcp}"
export AWS_REGION="${AWS_REGION:-us-east-1}" AWS_PAGER=""

envval() { sed -n "s/^$1=//p" "$root/.env" 2>/dev/null | tail -1 | sed -e 's/^"//' -e 's/"$//'; }
exists() { aws ssm get-parameter --name "$SSM_PATH/$1" >/dev/null 2>&1; }
put() { # name value
  tmp="$(mktemp)"; chmod 600 "$tmp"
  python3 -c 'import json,sys; print(json.dumps({"Name": sys.argv[1], "Value": sys.argv[2], "Type": "SecureString", "Overwrite": True}))' \
    "$SSM_PATH/$1" "$2" > "$tmp"
  aws ssm put-parameter --cli-input-json "file://$tmp" >/dev/null
  rm -f "$tmp"
  echo "stored $SSM_PATH/$1"
}

for name in SPLITWISE_CLIENT_ID SPLITWISE_CLIENT_SECRET REDIS_URL; do
  value="$(envval "$name")"
  if [ -n "$value" ]; then put "$name" "$value"
  elif exists "$name"; then echo "$SSM_PATH/$name already set (kept)"
  else echo "$SSM_PATH/$name missing: add it to .env and re-run"; [ "$name" = REDIS_URL ] || exit 1
  fi
done
if exists SERVER_SECRET; then echo "$SSM_PATH/SERVER_SECRET already set (kept)"
else put SERVER_SECRET "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"; fi
