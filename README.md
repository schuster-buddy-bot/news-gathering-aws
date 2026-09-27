# news-gathering-aws — Serverless AI News Pipeline

[![CI](https://github.com/schuster-buddy-bot/news-gathering-aws/actions/workflows/ci.yml/badge.svg)](https://github.com/schuster-buddy-bot/news-gathering-aws/actions/workflows/ci.yml)
[![Security](https://github.com/schuster-buddy-bot/news-gathering-aws/actions/workflows/security.yml/badge.svg)](https://github.com/schuster-buddy-bot/news-gathering-aws/actions/workflows/security.yml)
[![Deploy](https://github.com/schuster-buddy-bot/news-gathering-aws/actions/workflows/deploy.yml/badge.svg)](https://github.com/schuster-buddy-bot/news-gathering-aws/actions/workflows/deploy.yml)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?style=flat&logo=python&logoColor=white)](https://www.python.org/)
[![Terraform](https://img.shields.io/badge/Terraform-%7E1.9-7B42BC?style=flat&logo=terraform&logoColor=white)](https://terraform.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A 100 % serverless news pipeline on AWS: **44 RSS sources → deduplication → filtering →
AI summaries → embeddings → semantic search → daily PDF report**, plus a secured REST API.
Built entirely within the AWS free tier (target: ~$0/month).

📋 **Architecture diagram:** [docs/architecture.html](docs/architecture.html) (open in browser)

## Features

- **44 RSS/Atom sources** fetched in parallel (AI/ML/tech from US, EU, Asia)
- **Cross-run deduplication** via DynamoDB (URL-hash, TTL-managed)
- **AI summaries** of the top articles (Ollama chat API, SSM-managed key)
- **Embeddings + semantic search** — every article gets a 256-dim vector from
  AWS Bedrock Titan Text Embeddings V2 (semantic, L2-normalized; local feature
  hashing as automatic fallback), cosine similarity search at `GET /search?q=...`
- **Interest-based ranking** — articles scored against a configurable interest profile,
  PDF "Top Picks" ordered by relevance
- **Daily PDF report** (reportlab, in-process) + digest JSON archive
- **Secured API** — API key auth + usage-plan throttling (~100 req/min), secrets in SSM
- **Infrastructure as Code** — the full stack in Terraform

## Architecture

```
                ┌────────────────────────────────────────────────────────┐
                │                    AWS (eu-central-1)                  │
                │                                                        │
 EventBridge ───┼──► Lambda news-pipeline-pipeline                       │
 cron 05:00 UTC │        │                                               │
                │        ├─ S3        config/{sources,filters,interests}.json
                │        ├─ fetch     44 RSS feeds (parallel, retry)     │
                │        ├─ dedup     DynamoDB articles (TTL 14d)        │
                │        ├─ filter    include/exclude keywords           │
                │        ├─ summarize ollama.com/api/chat (top 10)       │
                │        ├─ embed     title+summary → 256-dim vector     │
                │        │              → DynamoDB (Base64 float32)      │
                │        ├─ relevance cosine(interests profile, article) │
                │        ├─ PDF        reportlab → S3 reports/ (TTL 14d) │
                │        └─ digest     S3 archive/ + DynamoDB (TTL 30d)  │
                │                                                        │
                │     API Gateway REST (stage v1, API-key auth)          │
 HTTPS ─────────┼──► GET /            Lambda news-pipeline-api  (public) │
                │    GET /health      Lambda news-pipeline-api  (public) │
                │    GET /search      Lambda news-pipeline-search 🔑     │
                │    GET /report/latest  Lambda news-pipeline-api 🔑     │
                │                                                        │
                │    SSM Parameter Store: ollama-api-key, ollama-model,  │
                │    embedding-model, api-key   (SecureString/String)    │
                └────────────────────────────────────────────────────────┘
```

### Search flow

```
GET /search?q=AI agents
   │
   ▼
Lambda search:  embed(q) ──► DynamoDB scan (attribute_exists(embedding))
                ──► cosine similarity vs every article vector
                ──► top-K JSON results (score-desc)
```

## API

Base URL: `https://r4w0f48k64.execute-api.eu-central-1.amazonaws.com/v1`

| Endpoint | Auth | Description |
|---|---|---|
| `GET /` | public | Service info + endpoint list |
| `GET /health` | public | Health check (DynamoDB + S3 connectivity) |
| `GET /search?q=<terms>&limit=<1-50>` | `x-api-key` | Semantic search over the last ~14 days of articles |
| `GET /report/latest` | `x-api-key` | Latest daily report metadata + presigned PDF URL (1 h) |

**Rate limiting:** usage plan — burst 100, sustained 2 req/s (≈100 req/min per key).
Requests without (or with a wrong) key get `403`.

### Examples

```bash
API=https://r4w0f48k64.execute-api.eu-central-1.amazonaws.com/v1
KEY=<your-api-key>   # request from the maintainer, or deploy your own stack

# Public: health check
curl $API/health
# → {"status": "ok", "version": "1.0", "region": "eu-central-1"}

# Semantic search (requires key)
curl -H "x-api-key: $KEY" "$API/search?q=AI+agents"
# → {
#     "query": "AI agents",
#     "model": "local-hashed-256",
#     "corpus_size": 125,
#     "count": 10,
#     "results": [
#       {
#         "title": "One company is at the center of a wave of rogue AI attacks",
#         "url": "https://www.theverge.com/...",
#         "category": "major",
#         "date": "2026-09-26",
#         "source": "The Verge AI",
#         "summary": "In July, OpenAI revealed that its AI agents ...",
#         "similarity_score": 0.311
#       },
#       ...
#     ]
#   }

# Latest report (requires key) — returns presigned PDF download URL
curl -H "x-api-key: $KEY" "$API/report/latest"
# → {"date": "2026-09-26", "article_count": 125, "url": "https://...s3...", "expires_in": 3600}
```

## Data retention

| Store | Content | TTL |
|---|---|---|
| S3 `reports/` | Daily PDF reports | 14 days |
| S3 `archive/` | Digest JSON | 14 days |
| DynamoDB `articles` | Dedup hashes + embeddings + relevance | 14 days |
| DynamoDB `reports` | Report metadata | 30 days |

## Tech stack

| Layer | Tech |
|---|---|
| Compute | AWS Lambda (Python 3.12, ARM-free x86, 512/256 MB) |
| IaC | Terraform ~> 5.0 (AWS + archive provider) |
| Storage | S3 (PDF/JSON, encrypted, versioned), DynamoDB (pay-per-request, TTL) |
| API | API Gateway REST (AWS_PROXY, API keys, usage plan) |
| Scheduling | EventBridge `cron(0 5 * * ? *)` |
| AI | Ollama chat API (summaries), feature-hashing embeddings (256-dim, stdlib) |
| PDF | reportlab (in-process) |
| Secrets | SSM Parameter Store (SecureString) |

### About the embeddings

The pipeline defaults to a **deterministic feature-hashing embedder** (`local-hashed-256`):
unigrams + bigrams hashed into 256 buckets, sublinear TF weighting, L2-normalized.
Pure stdlib — no network calls, no extra cost, works in every region. It powers lexical
similarity ("AI agents" finds agent articles).

The design keeps a clean swap path to a **neural embedder**: set the SSM parameter
`/news-pipeline/embedding-model` to an Ollama embedding model and point `OLLAMA_ENDPOINT`
at an embed-capable host — `embeddings.py` then uses `POST <host>/api/embed` instead.
(The public ollama.com API currently exposes chat models only.)

## Deploy

Prerequisites: AWS CLI (configured profile), Terraform >= 1.5, Python 3 with pip.

```bash
git clone https://github.com/schuster-buddy-bot/news-gathering-aws.git
cd news-gathering-aws

# 1. Build Lambda packages (pins deps to Python 3.12 / manylinux2014)
./build.sh

# 2. Provision infrastructure
cd infra
terraform init
terraform apply -auto-approve

# 3. Upload pipeline config
aws s3 cp ../config/interests.json s3://<bucket>/config/interests.json
# + your own sources.json / filters.json (same prefix)

# 4. Secrets (SecureString) — placeholders created by Terraform
aws ssm put-parameter --name /news-pipeline/ollama-api-key \
  --type SecureString --value "<your-ollama-api-key>" --overwrite

# 5. Read your API key for /search + /report/latest
aws ssm get-parameter --name /news-pipeline/api-key --with-decryption \
  --query Parameter.Value --output text
```

### Run manually

```bash
# Full run, bypassing cross-run dedup (repeatable on the same day)
aws lambda invoke --function-name news-pipeline-pipeline \
  --payload '{"force": true}' out.json

# Normal scheduled run (dedup applies)
aws lambda invoke --function-name news-pipeline-pipeline \
  --payload '{}' out.json

# Debug probes (no storage touched)
aws lambda invoke --function-name news-pipeline-pipeline \
  --payload '{"test_ai": true}' out-ai.json      # Ollama chat probe
aws lambda invoke --function-name news-pipeline-pipeline \
  --payload '{"test_embed": true}' out-embed.json # embedding probe
```

## Cost (AWS Always-Free / Free Tier)

All AWS services run within the Always-Free or 12-month Free Tier. Measured usage as of 2026-09-27:

| Service | Actual Usage | Free Tier Allowance | Cost |
|---|---|---|---|
| Lambda | ~2 runs/day × ~90 s × 512 MB ≈ 90 GB-s/mo | 1M req + 400,000 GB-s/month | $0 |
| DynamoDB | ~300 items × 14 d retention, <1 GB | 25 GB + 25 RCU/25 WCU always-free | $0 |
| S3 | ~50 MB (PDFs + JSON + config) | 5 GB (12-month) | $0 |
| API Gateway | <100 calls/month (demo) | 1M calls (12-month) | $0 |
| EventBridge | 1 schedule | free | $0 |
| SSM | 4 parameters | free tier | $0 |
| CloudWatch | ~50 MB logs | 10 GB/month free | $0 |
| **AWS Total** | | | **$0/month** |

External: Ollama API for AI summaries — ~$0.50/month depending on plan (not billed through AWS).

## Repository layout

```
├── lambda_handler.py   # pipeline Lambda (fetch → dedup → filter → summarize → embed → score → PDF)
├── embeddings.py       # embedding providers (local feature hashing / Ollama) + Base64 packing
├── search_handler.py   # semantic-search Lambda (GET /search)
├── api_handler.py      # API Gateway Lambda (/health, /report/latest, /)
├── pdf_generator.py    # reportlab rendering (relevance-ranked Top Picks)
├── config/interests.json  # interest profile (edit in S3 without redeploy)
├── build.sh            # builds deployment packages into build/
└── infra/              # Terraform
    ├── main.tf         # provider + caller identity
    ├── variables.tf    # region, models, retention, TTLs
    ├── s3.tf           # private bucket (encryption, versioning, 14d lifecycle)
    ├── dynamodb.tf     # articles (TTL 14d) + reports (TTL 30d)
    ├── lambda.tf       # pipeline + api + search functions, log groups
    ├── iam.tf          # least-privilege roles (specific ARNs)
    ├── apigw.tf        # REST API, 4 GET routes, API key + usage plan
    ├── eventbridge.tf  # daily schedule
    ├── ssm.tf          # /news-pipeline/{ollama-api-key, ollama-model, embedding-model, api-key}
    └── outputs.tf      # API URL, search URL, bucket, function/table names
```

## Design notes

- **Least-privilege IAM** — every role scoped to specific resource ARNs; no `"*"` resources.
- **No secrets in the repo** — Ollama key + API key live in SSM (SecureString). Terraform
  state files are gitignored.
- **Graceful degradation** — missing AI key → description fallback; failed embedding →
  article stored without embedding; failed feed → retry, then warning in the report footer.
- **Regional presigned URLs** — presigning uses virtual-host addressing
  (`s3.eu-central-1.amazonaws.com`); the global endpoint breaks SigV4 after a 307 redirect.
- **Embeddings scale with retention** — the search corpus = last 14 days of filtered
  articles (~120/day) — scan-and-cosine stays well within DynamoDB free-tier RCU.

## License

MIT — see [LICENSE](LICENSE).