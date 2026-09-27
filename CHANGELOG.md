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

## [1.0.0] — 2026-09-26

### Added
- Serverless news pipeline: 44 RSS sources → dedup → filter → AI summaries
  → embeddings → semantic search → daily PDF report
- AWS Lambda + API Gateway + DynamoDB + S3, all Terraform IaC
- Demo UI on S3 with semantic search and PDF download
- 43 unit tests, ruff linting, GitHub Actions CI
- 5-agent compound review: 1 critical + 11 warnings fixed
- Sprint W41: Issues #1–#7 closed