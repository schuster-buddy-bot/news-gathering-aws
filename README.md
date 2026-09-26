# News Pipeline — AWS Lambda Serverless

> AI-powered daily news gathering pipeline running on AWS Lambda.
> Fetches 44 RSS sources, deduplicates, classifies with LLM, generates PDF report.

## Architecture

```
EventBridge (cron 07:00 CEST daily)
    │
    ▼
Lambda (Python 3.12, 512MB, 15min)
    ├── Fetch RSS feeds (feedparser)
    ├── Dedup via DynamoDB (URL hash)
    ├── Classify + Summarize via Ollama API
    ├── Generate PDF (reportlab)
    ├── Store PDF in S3
    └── Store metadata in DynamoDB

API Gateway (REST, /v1)
    ├── GET /report/latest → presigned S3 URL
    └── GET /health → status check
```

## AWS Services (all within Always-Free tier)

| Service | Usage | Free Tier Limit |
|---------|-------|----------------|
| Lambda | ~1 invocation/day | 1M req/month |
| S3 | ~1 PDF/day (~100KB) | 5GB |
| DynamoDB | ~300 items/day | 25GB + 25 RCU/WCU |
| API Gateway | ~10 req/day | 1M req/month |
| EventBridge | 1 cron trigger/day | 14M invocations/month |
| SSM | 2 parameters | 10,000 parameters |
| CloudWatch | Lambda logs | 5GB ingestion |

**Monthly cost: $0** (within Always-Free limits)

## Deploy

### Prerequisites
- AWS CLI v2 configured with a profile
- Terraform >= 1.9

### Infrastructure

```bash
cd infra/
terraform init
terraform plan
terraform apply
```

### Upload Config

```bash
aws s3 cp sources.json s3://<bucket>/config/sources.json
aws s3 cp filters.json s3://<bucket>/config/filters.json
```

### Set Secrets

```bash
aws ssm put-parameter --name "/news-pipeline/ollama-api-key" --value "YOUR_KEY" --type SecureString
aws ssm put-parameter --name "/news-pipeline/ollama-model" --value "glm-5.3-flash" --type SecureString
```

### Test

```bash
# Manual trigger
aws lambda invoke --function-name news-pipeline-pipeline response.json

# API health
curl https://<api-id>.execute-api.eu-central-1.amazonaws.com/v1/health

# Latest report
curl https://<api-id>.execute-api.eu-central-1.amazonaws.com/v1/report/latest
```

## Tech Stack

- **Runtime:** Python 3.12
- **RSS:** feedparser
- **Sanitization:** bleach
- **PDF:** reportlab
- **AWS SDK:** boto3
- **IaC:** Terraform (AWS provider ~> 5.0)

## Project Structure

```
├── lambda_handler.py    # Main pipeline Lambda function
├── api_handler.py       # API Gateway Lambda function
├── pdf_generator.py     # reportlab PDF generation
├── requirements.txt     # Python dependencies
├── build.sh            # ZIP packaging script
├── .gitignore
├── LICENSE             # MIT
└── infra/
    ├── main.tf         # Provider + backend
    ├── lambda.tf       # Lambda functions
    ├── s3.tf           # S3 bucket
    ├── dynamodb.tf     # DynamoDB tables
    ├── apigw.tf        # API Gateway
    ├── eventbridge.tf  # EventBridge cron
    ├── iam.tf          # IAM roles + policies
    ├── ssm.tf          # SSM parameters
    ├── variables.tf    # Input variables
    └── outputs.tf      # API URL, bucket, etc.
```

## License

MIT