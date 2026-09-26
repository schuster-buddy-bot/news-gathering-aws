# News Pipeline on AWS Lambda

Serverless deployment of the [news-gathering](https://github.com/LbTdW/news-gathering) pipeline:
daily RSS ingestion → URL-hash deduplication (DynamoDB) → keyword filtering → AI summarization
(Ollama API) → PDF report (reportlab) → S3, with a REST API for retrieval. 100% free tier.

## Architecture

```
EventBridge (cron 05:00 UTC = 07:00 CEST daily)
    │
    ▼
Lambda: news-pipeline-pipeline (Python 3.12, 512 MB, 15 min timeout)
    ├── loads config from S3        s3://<bucket>/config/{sources,filters,config}.json
    ├── fetches 40+ RSS feeds       (urllib + ElementTree, parallel)
    ├── dedup vs. DynamoDB          articles table, PK url_hash, TTL 90d
    ├── keyword filter              include/exclude keyword lists
    ├── AI summaries (top 10)       HTTPS → ollama.com/api/chat, key via SSM
    ├── renders PDF                 reportlab (in-process, no subprocess)
    ├── stores PDF                  s3://<bucket>/reports/YYYY-MM-DD-report.pdf
    └── stores digest JSON          s3://<bucket>/archive/YYYY-MM-DD-digest.json
                                    + DynamoDB reports table (PK date)

API Gateway (REST, stage v1)  ──►  Lambda: news-pipeline-api (256 MB)
    ├── GET /               → endpoint info
    ├── GET /health         → {"status": "ok", "version": "1.0"}
    └── GET /report/latest  → metadata + presigned S3 URL (60 min)
```

## API Demo

```bash
# Health check
curl https://<api-id>.execute-api.eu-central-1.amazonaws.com/v1/health

# Latest report: metadata + presigned download URL (1 h validity)
curl https://<api-id>.execute-api.eu-central-1.amazonaws.com/v1/report/latest
# → {"date": "2026-09-26", "article_count": 125, "url": "https://...s3.eu-central-1.amazonaws.com/..."}
```

Download the `url` from `/report/latest` in a browser or `curl` to get the PDF.

## Layout

```
├── lambda_handler.py   # pipeline Lambda (fetch → dedup → filter → summarize → PDF → store)
├── api_handler.py      # API Gateway Lambda (/health, /report/latest, /)
├── pdf_generator.py    # reportlab report rendering (pure function, PDF bytes out)
├── requirements.txt    # bleach, reportlab, requests
├── build.sh            # builds deployment packages into build/
└── infra/              # Terraform (AWS provider ~> 5.0)
    ├── main.tf         # provider + caller identity
    ├── variables.tf    # region, model, retention, TTLs
    ├── s3.tf           # private bucket (encryption, versioning, 90d lifecycle)
    ├── dynamodb.tf     # articles (dedup, TTL) + reports (metadata), pay-per-request
    ├── lambda.tf       # both functions + log groups (14d retention)
    ├── iam.tf          # least-privilege roles (specific ARNs)
    ├── apigw.tf        # REST API, 3 GET routes, AWS_PROXY
    ├── eventbridge.tf  # daily schedule cron(0 5 * * ? *)
    ├── ssm.tf          # /news-pipeline/{ollama-api-key, ollama-model}
    └── outputs.tf      # API URL, bucket, function/table names
```

## Deploy

```bash
# 1. Build Lambda packages (pins deps to Python 3.12 / manylinux2014)
./build.sh

# 2. Provision infrastructure
cd infra
terraform init
terraform plan -out=tfplan
terraform apply tfplan

# 3. Upload pipeline config
aws s3 cp sources.json s3://<bucket>/config/sources.json
aws s3 cp filters.json s3://<bucket>/config/filters.json

# 4. Set your Ollama API key (created as placeholder by Terraform)
aws ssm put-parameter --name /news-pipeline/ollama-api-key \
  --type SecureString --value "<your-ollama-api-key>" --overwrite
```

## Run / Test

```bash
# Manual run, bypassing DynamoDB dedup (full report even on the same day)
echo '{"force": true}' > payload.json
aws lambda invoke --function-name news-pipeline-pipeline \
  --payload fileb://payload.json out.json
cat out.json

# Dedup check: a normal run skips everything already processed
echo '{}' > payload.json
aws lambda invoke --function-name news-pipeline-pipeline \
  --payload fileb://payload.json out2.json   # → "skipped_already_seen": 301

# Ollama API probe without touching storage (debugging)
echo '{"test_ai": true}' > payload-ai.json
aws lambda invoke --function-name news-pipeline-pipeline \
  --payload fileb://payload-ai.json out-ai.json
```

## Design Notes

- **Dedup**: single-key DynamoDB table (`url_hash` = SHA-256 of normalized title + URL).
  `BatchGetItem` in chunks of 100 → cross-run dedup costs 2–3 API calls. Entries expire via
  DynamoDB TTL after 90 days so the table self-cleans.
- **Graceful AI degradation**: with a missing/placeholder API key the pipeline falls back to
  truncated descriptions (same behavior as the original gateway pipeline). Summaries are tagged
  `ai_summary: true/false` in the digest archive.
- **Presigned URLs are regional**: the API Lambda presigns against
  `s3.<region>.amazonaws.com` (virtual-host style). The global `s3.amazonaws.com` endpoint
  issues a 307 redirect that breaks the SigV4 host signature → 403 after redirect.
- **No secrets in the repo**: the Ollama API key lives in SSM Parameter Store
  (SecureString, decrypted at runtime). Terraform creates the parameter with a placeholder
  and `ignore_changes = [value]` so applies never clobber the real key.
- **Free tier**: ~30 Lambda runs/month (1M free), one PDF/day in S3 (5 GB free),
  a few hundred small DynamoDB items/month (25 GB free) → $0/month.

## Differences from the gateway version

| Gateway version | AWS version |
|---|---|
| Embedding-based dedup (qwen3-embedding) | DynamoDB URL-hash dedup (no vector store in free tier) |
| PDF via subprocess | reportlab in-process |
| Filesystem archive | S3 (reports + digest JSON) + DynamoDB metadata |
| Telegram delivery | API Gateway endpoints (decoupled by design) |
| ETag caching | dropped (stateless Lambda) |

## License

MIT — see [LICENSE](LICENSE).