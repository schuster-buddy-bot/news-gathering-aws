# Contributing

Thanks for your interest in improving the news pipeline!

## Quick start

```bash
git clone https://github.com/schuster-buddy-bot/news-gathering-aws.git
cd news-gathering-aws
./build.sh          # builds Lambda packages into build/ (Python 3.12 pinned)
cd infra
terraform init      # once
terraform plan      # preview changes before touching AWS
```

## Making changes

**Pipeline / API code** (`lambda_handler.py`, `search_handler.py`, `api_handler.py`,
`pdf_generator.py`, `embeddings.py`):

1. Edit the module(s).
2. Run `./build.sh` — deployment ZIPs are rebuilt from source.
3. `cd infra && terraform apply` — Terraform repackages the functions when the
   archive hash changes (`source_code_hash`).
4. Test: `aws lambda invoke --function-name news-pipeline-pipeline --payload '{"force": true}' out.json`
   (or the `test_ai` / `test_embed` probes for quick checks).

**Infrastructure** (`infra/*.tf`):

1. Edit the `.tf` file(s).
2. Always run `terraform plan` first and read the diff.
3. Apply, then verify with the outputs (`terraform output`).

**Pipeline config** (`config/*.json`) — edit in S3, no redeploy needed:

```bash
aws s3 cp config/interests.json s3://<bucket>/config/interests.json
```

## Conventions

- **No secrets in git, ever.** Secrets live in SSM Parameter Store; `.env`/state files
  are gitignored.
- Least-privilege IAM: every new permission gets a scoped ARN, no wildcard resources.
- Keep the pipeline failure-tolerant: per-article errors must be caught and logged,
  never abort the run.
- Python: stdlib-first; new dependencies go into `requirements.txt` and are pinned
  to Lambda's runtime platform by `build.sh`.
- Terraform: variables for tunables (retention days, models), `default_tags` kept clean.

## Pull requests

1. Fork / branch from `main`.
2. Keep PRs focused; describe what changed and why.
3. Verify the pipeline end-to-end before opening a PR (a force run + one `/search` call).