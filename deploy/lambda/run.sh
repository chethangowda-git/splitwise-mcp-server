#!/bin/sh
# Lambda entry point. The AWS Lambda Web Adapter layer (/opt/bootstrap) runs this, waits
# for /health, then forwards each function URL request to the server on $PORT.
export PYTHONPATH="$LAMBDA_TASK_ROOT:$LAMBDA_TASK_ROOT/src"
exec python3 "$LAMBDA_TASK_ROOT/main.py" --http --port "${PORT:-8080}"
