#!/bin/sh
# Deploy or update the server as an AWS Lambda function with a public HTTPS function URL.
#
# Reads SPLITWISE_CLIENT_ID, SPLITWISE_CLIENT_SECRET and (optionally) REDIS_URL from .env and
# stores them, with a generated SERVER_SECRET, as encrypted SSM parameters under /$FUNCTION.
# The function loads them at startup, so no secret sits in its configuration. Safe to re-run.
#
# Needs: AWS CLI v2 signed in to your account. Optional env: FUNCTION, AWS_REGION, MAX_CONCURRENCY.
set -eu

root="$(cd "$(dirname "$0")/../.." && pwd)"
FUNCTION="${FUNCTION:-splitwise-mcp}"
REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}"
REGION="${REGION:-us-east-1}"
ROLE="$FUNCTION-lambda"
SSM_PATH="/$FUNCTION"
MAX_CONCURRENCY="${MAX_CONCURRENCY:-2}"
export AWS_REGION="$REGION" AWS_PAGER=""

say() { printf '\n==> %s\n' "$*"; }
envval() { sed -n "s/^$1=//p" "$root/.env" 2>/dev/null | tail -1 | sed -e 's/^"//' -e 's/"$//'; }

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
say "Account $ACCOUNT, region $REGION, function $FUNCTION"

# ---------------------------------------------------------------- secrets (never printed)
put_secret() { # name value
  tmp="$(mktemp)"; chmod 600 "$tmp"
  python3 -c 'import json,sys; print(json.dumps({"Name": sys.argv[1], "Value": sys.argv[2], "Type": "SecureString", "Overwrite": True}))' \
    "$SSM_PATH/$1" "$2" > "$tmp"
  aws ssm put-parameter --cli-input-json "file://$tmp" >/dev/null
  rm -f "$tmp"
  echo "   stored $SSM_PATH/$1"
}
say "Storing secrets in SSM Parameter Store"
for name in SPLITWISE_CLIENT_ID SPLITWISE_CLIENT_SECRET; do
  value="$(envval "$name")"
  [ -n "$value" ] || { echo "   $name is missing from .env"; exit 1; }
  put_secret "$name" "$value"
done
redis="$(envval REDIS_URL)"
if [ -n "$redis" ]; then
  put_secret REDIS_URL "$redis"
elif aws ssm get-parameter --name "$SSM_PATH/REDIS_URL" >/dev/null 2>&1; then
  echo "   $SSM_PATH/REDIS_URL already exists (kept)"
else
  echo "   no REDIS_URL: state is kept in memory (single instance only)"
fi
# SERVER_SECRET is created once and kept: changing it signs everyone out.
if aws ssm get-parameter --name "$SSM_PATH/SERVER_SECRET" >/dev/null 2>&1; then
  echo "   $SSM_PATH/SERVER_SECRET already exists (kept)"
else
  put_secret SERVER_SECRET "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
fi

# ---------------------------------------------------------------- IAM role
say "IAM role $ROLE"
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  created_role=1
fi
# Least privilege: read only this app's parameters.
aws iam put-role-policy --role-name "$ROLE" --policy-name read-own-parameters --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{\"Effect\": \"Allow\", \"Action\": [\"ssm:GetParametersByPath\", \"ssm:GetParameter\"],
    \"Resource\": [\"arn:aws:ssm:$REGION:$ACCOUNT:parameter$SSM_PATH\", \"arn:aws:ssm:$REGION:$ACCOUNT:parameter$SSM_PATH/*\"]}]
}"
ROLE_ARN="arn:aws:iam::$ACCOUNT:role/$ROLE"
[ -z "${created_role:-}" ] || { echo "   waiting for the new role to propagate"; sleep 12; }

# ---------------------------------------------------------------- Lambda Web Adapter layer
say "Finding the latest AWS Lambda Web Adapter layer"
LAYER=""
for v in $(seq 40 -1 18); do
  arn="arn:aws:lambda:$REGION:753240598075:layer:LambdaAdapterLayerArm64:$v"
  if aws lambda get-layer-version-by-arn --arn "$arn" >/dev/null 2>&1; then LAYER="$arn"; break; fi
done
[ -n "$LAYER" ] || { echo "   could not find the Lambda Web Adapter layer in $REGION"; exit 1; }
echo "   $LAYER"

# ---------------------------------------------------------------- build
say "Building the package"
"$root/deploy/lambda/build.sh"
ZIP="$root/build/lambda/function.zip"

env_json() { # public_url
  python3 -c 'import json,sys; print(json.dumps({"Variables": {
    "AWS_LAMBDA_EXEC_WRAPPER": "/opt/bootstrap", "AWS_LWA_PORT": "8080", "PORT": "8080",
    "AWS_LWA_READINESS_CHECK_PATH": "/health", "MCP_JSON_RESPONSE": "1",
    "SSM_PARAMETER_PATH": sys.argv[2], "PUBLIC_URL": sys.argv[1]}}))' "$1" "$SSM_PATH"
}

# ---------------------------------------------------------------- function
say "Function $FUNCTION"
if aws lambda get-function --function-name "$FUNCTION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FUNCTION" --zip-file "fileb://$ZIP" --architectures arm64 >/dev/null
  aws lambda wait function-updated-v2 --function-name "$FUNCTION"
  echo "   code updated"
else
  aws lambda create-function --function-name "$FUNCTION" --runtime python3.13 --architectures arm64 \
    --handler run.sh --role "$ROLE_ARN" --zip-file "fileb://$ZIP" --memory-size 1024 --timeout 30 \
    --layers "$LAYER" --environment "$(env_json "")" >/dev/null
  aws lambda wait function-active-v2 --function-name "$FUNCTION"
  echo "   created"
fi

# ---------------------------------------------------------------- public URL
say "Function URL"
if ! URL="$(aws lambda get-function-url-config --function-name "$FUNCTION" --query FunctionUrl --output text 2>/dev/null)"; then
  URL="$(aws lambda create-function-url-config --function-name "$FUNCTION" --auth-type NONE \
    --invoke-mode BUFFERED --query FunctionUrl --output text)"
fi
# The app does its own OAuth, so the URL is public. Both permissions are required for public URLs.
aws lambda add-permission --function-name "$FUNCTION" --statement-id FunctionURLAllowPublicAccess \
  --action lambda:InvokeFunctionUrl --principal "*" --function-url-auth-type NONE >/dev/null 2>&1 || true
aws lambda add-permission --function-name "$FUNCTION" --statement-id FunctionURLInvokeAllowPublicAccess \
  --action lambda:InvokeFunction --principal "*" --invoked-via-function-url >/dev/null 2>&1 || true
PUBLIC_URL="${URL%/}"
echo "   $PUBLIC_URL"

# Set every setting here too, so a function first made in the console ends up the same.
aws lambda update-function-configuration --function-name "$FUNCTION" --role "$ROLE_ARN" \
  --runtime python3.13 --handler run.sh --memory-size 1024 --timeout 30 --layers "$LAYER" \
  --environment "$(env_json "$PUBLIC_URL")" >/dev/null
aws lambda wait function-updated-v2 --function-name "$FUNCTION"

# ---------------------------------------------------------------- guard rails
say "Guard rails"
aws logs create-log-group --log-group-name "/aws/lambda/$FUNCTION" >/dev/null 2>&1 || true
aws logs put-retention-policy --log-group-name "/aws/lambda/$FUNCTION" --retention-in-days 14
echo "   logs kept 14 days"
if aws lambda put-function-concurrency --function-name "$FUNCTION" \
     --reserved-concurrent-executions "$MAX_CONCURRENCY" >/dev/null 2>&1; then
  echo "   at most $MAX_CONCURRENCY copies at once"
else
  echo "   could not cap concurrency (new accounts often can't reserve any); set a budget alert instead"
fi

say "Checking the deployment"
curl -sf "$PUBLIC_URL/health" && echo

cat <<EOF

Done.
  Connector URL (claude.ai -> Settings -> Connectors):  $PUBLIC_URL/mcp
  Splitwise app Callback URL:                           $PUBLIC_URL/oauth/splitwise/callback
EOF
