# Changelog

All notable changes to this project are documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed — 2026-09-27
- **Presigned URL bug:** Lambda Python 3.12 runtime boto3 (~1.34) truncated
  the SigV4 service name to `s` in presigned S3 URLs. Fix: bundle boto3
  v1.43.91 into the API Lambda deployment package.
- **Daily report corpus:** PDF now includes all articles with
  `first_seen=today` from DynamoDB (not just new-since-last-run). Falls
  back to yesterday when today has < 10 items.

### Changed — 2026-09-27
- **PDF layout v5:** Unified with local news-gatherer v4 — Table of
  Contents, Top Picks (8 featured with AI summaries), 7 category sections
  with colored header bars, zebra striping, page numbers. Lambda-safe
  (no image downloads, pure BytesIO).

### Added — 2026-09-27
- **Separate demo API key:** `news-pipeline-demo-public` for shareable
  URLs (`?key=...` query parameter), independent from the gateway key.

## [1.1.0] — 2026-09-27

### Added
- **Public demo stack (CQRS read side)** — Issues #8, #11, #9, #10:
  - `demo_handler.py`: public READ-ONLY Lambda (`news-pipeline-demo`) — 6 routes
    (`GET /demo/search`, `GET /demo/report/latest`, `GET /demo/topics`,
    `POST /demo/search-by-topics`, `GET /demo/browse`, `GET /demo/articles`),
    reusing the private search engine + report presign logic (DRY refactor:
    `search_handler.search_articles()`, `api_handler.latest_report()`)
  - `infra/demo-api.tf`: API Gateway resources for all `/demo/*` routes
    (apiKeyRequired = false), usage plan `news-pipeline-demo-public`
    (rate 5 / burst 10), per-method throttle targets (canonical
    `/~1demo~1…/GET` ResourcePath encoding), demo Lambda with strictly
    read-only IAM role (Scan/GetObject/GetParameter/Bedrock invoke — no writes)
  - Gateway responses remap Lambda "Rate Exceeded" (`API_CONFIGURATION_ERROR`
    / `DEFAULT_5XX`) to a clean retryable `429` with `rate_limited` JSON body
  - Stage access logging → `/apigw/news-pipeline-access` (JSON audit trail)
  - Demo UI v2: tabbed SPA (Search / Topic Explorer / Browse) with chip editor,
    CSV topic upload, category browsing with live counts and article modal;
    no API key anywhere; hash routing + `?q=`/`?topics=` deep links
  - `config/demo_topics.json` (S3-backed, editable without redeploy)
  - Deploy workflow: builds `demo_handler.py` into the api ZIP, syncs
    `demo/index.html` to the S3 website bucket
- 21 new tests (75 total): all demo routes, aggregation/dedupe, CORS,
  S3 fallback, no-write guardrail

### Verified live (eu-central-1)
- All 6 demo endpoints 200 without key; private routes unchanged (403 without key)
- 15 parallel requests on `/demo/search` → 10×200 + 5×429 (throttle target met)
- Presigned PDF download works from the public demo (200, `%PDF-` magic)
- Headless-Chromium E2E against the live S3 site: 19/19 checks pass

## [1.0.0] — 2026-09-26

### Added
- Serverless news pipeline: 44 RSS sources → dedup → filter → AI summaries
  → embeddings → semantic search → daily PDF report
- AWS Lambda + API Gateway + DynamoDB + S3, all Terraform IaC
- Demo UI on S3 with semantic search and PDF download
- 43 unit tests, ruff linting, GitHub Actions CI
- 5-agent compound review: 1 critical + 11 warnings fixed
- Sprint W41: Issues #1–#7 closed