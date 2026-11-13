# KB-Sync Lambda

Triggers a Bedrock Knowledge Base ingestion job whenever a file is
created/deleted under the KB's S3 prefix, so KB content stays in sync
with S3 without manually clicking "Sync" in the console.

```
S3 (PutObject/DeleteObject)
  -> EventBridge rule (aws.s3 Object Created/Deleted)
  -> Lambda (index.py)
  -> bedrock-agent:StartIngestionJob
```

## Files

- `index.py` — Lambda handler. Reads `KNOWLEDGE_BASE_ID` and
  `DATA_SOURCE_ID` from environment variables, checks for an
  already-running ingestion job (to avoid `ConflictException`), and
  starts a new one.

## Required environment variables

| Variable | Description |
|---|---|
| `KNOWLEDGE_BASE_ID` | Bedrock Knowledge Base ID |
| `DATA_SOURCE_ID` | S3 data source ID attached to the KB |

## IAM permissions needed by the Lambda execution role

- `bedrock:StartIngestionJob`
- `bedrock:ListIngestionJobs`
- `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`

## Step 1 — Enable EventBridge notifications on the S3 bucket

S3 must be told to forward events to the default EventBridge bus
(this is separate from the older S3 "Event Notifications" feature):

```bash
aws s3api put-bucket-notification-configuration ^
  --bucket <YOUR_BUCKET> ^
  --notification-configuration "{\"EventBridgeConfiguration\": {}}"
```

Or via Console: S3 → bucket → **Properties** → **Amazon EventBridge** → **Edit** → Enable.

## Step 2 — Create the Lambda execution role

Trust policy (`trust-policy.json`):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
```

Permissions policy (`lambda-policy.json`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:*:*:*"
    },
    {
      "Effect": "Allow",
      "Action": ["bedrock:StartIngestionJob", "bedrock:ListIngestionJobs"],
      "Resource": "arn:aws:bedrock:<REGION>:<ACCOUNT_ID>:knowledge-base/<KB_ID>"
    }
  ]
}
```

```bash
aws iam create-role --role-name kb-sync-lambda-role --assume-role-policy-document file://trust-policy.json

aws iam put-role-policy --role-name kb-sync-lambda-role --policy-name kb-sync-policy --policy-document file://lambda-policy.json
```

## Step 3 — Deploy the Lambda function

```bash
cd KB-Sync
powershell Compress-Archive -Path index.py -DestinationPath function.zip -Force

aws lambda create-function ^
  --function-name kb-sync-on-s3-change ^
  --runtime python3.12 ^
  --role arn:aws:iam::<ACCOUNT_ID>:role/kb-sync-lambda-role ^
  --handler index.handler ^
  --timeout 30 ^
  --zip-file fileb://function.zip ^
  --environment "Variables={KNOWLEDGE_BASE_ID=<KB_ID>,DATA_SOURCE_ID=<DATA_SOURCE_ID>}"
```

To update code after editing `index.py`:

```bash
powershell Compress-Archive -Path index.py -DestinationPath function.zip -Force
aws lambda update-function-code --function-name kb-sync-on-s3-change --zip-file fileb://function.zip
```

## Step 4 — Create the EventBridge rule

Only match Object Created/Deleted events for the bucket/prefix the KB
data source points at (`event-pattern.json`):

```json
{
  "source": ["aws.s3"],
  "detail-type": ["Object Created", "Object Deleted"],
  "detail": {
    "bucket": {
      "name": ["<YOUR_BUCKET>"]
    },
    "object": {
      "key": [{"prefix": "<YOUR_KB_PREFIX>/"}]
    }
  }
}
```

```bash
aws events put-rule ^
  --name kb-s3-sync-rule ^
  --event-pattern file://event-pattern.json ^
  --state ENABLED
```

## Step 5 — Point the rule at the Lambda function

```bash
aws events put-targets ^
  --rule kb-s3-sync-rule ^
  --targets "Id"="1","Arn"="arn:aws:lambda:<REGION>:<ACCOUNT_ID>:function:kb-sync-on-s3-change"

aws lambda add-permission ^
  --function-name kb-sync-on-s3-change ^
  --statement-id eventbridge-invoke ^
  --action lambda:InvokeFunction ^
  --principal events.amazonaws.com ^
  --source-arn arn:aws:events:<REGION>:<ACCOUNT_ID>:rule/kb-s3-sync-rule
```

## Step 6 — Test

```bash
aws s3 cp test.txt s3://<YOUR_BUCKET>/<YOUR_KB_PREFIX>/test.txt

aws logs tail /aws/lambda/kb-sync-on-s3-change --follow

aws bedrock-agent list-ingestion-jobs --knowledge-base-id <KB_ID> --data-source-id <DATA_SOURCE_ID>
```

## Notes / gotchas

- **Concurrent uploads**: `StartIngestionJob` fails with `ConflictException`
  if a job is already running for the data source. `index.py` checks
  `ListIngestionJobs` for an `IN_PROGRESS` job and skips starting a new
  one, but bulk uploads (many files at once) will still fire many Lambda
  invocations in a short window. For heavy batch updates, consider
  batching via an SQS delay queue instead of triggering per-object.
- **Async**: `StartIngestionJob` returns immediately; the actual sync can
  take minutes. The Lambda does not need to wait for completion.
- **Cross-account/region**: if the S3 bucket and the Knowledge Base are
  in different accounts, additional EventBridge cross-account bus and
  resource policy configuration is required.
- **Cost**: Lambda + EventBridge cost is negligible at low event volume;
  each triggered ingestion job does consume embedding-model calls, so
  avoid over-triggering on large batch updates.
