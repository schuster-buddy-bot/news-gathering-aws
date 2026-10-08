# Review: OIDC Federation Bootstrap

## Verdict: Safe to merge (with P2 follow-up) — no P1 blockers

**Date:** 2026-10-08  
**Reviewer:** Compound Review (5-agent parallel)  
**Commits:** ab8a92f..204de00 (9 commits, 3 files, +121/-58 lines)  
**Files:** `infra/oidc.tf`, `.github/workflows/deploy.yml`, `.github/workflows/drift-detection.yml`

---

## Verification (Concept-Level)

- [x] Requirements: OIDC federation replaces static access keys — ✅ achieved
- [x] Architecture: Matches DECISIONS.md ADR #8 (API-Key vs Cognito vs IAM → OIDC) — ✅ clean replacement
- [x] Use cases: Deploy + Drift Detection workflows both green via OIDC — ✅ verified
- [x] Quality goals: Keyless auth, repo-scoped trust, no static secrets — ✅ met
- [x] Scope: Unchanged — OIDC bootstrap + i18n deploy, no feature creep

## Testing (Code-Level)

- Unit tests: **82/82 passing** (2.06s)
- Linting: **ruff clean**
- Terraform: **fmt clean, validate success**
- CI: **green** (22s)
- Deploy: **2 consecutive green runs** (workflow_run 1m55s + workflow_dispatch 2m3s)
- Live verification: API health ok, demo UI i18n (36 data-i18n attrs, EN/DE 108 strings each)

---

## Findings by Perspective

### Security Sentinel

| # | Sev | Finding | File | Recommendation |
|---|-----|---------|------|----------------|
| S1 | P2 | `apigateway:*` on `arn:aws:apigateway:eu-central-1::*` grants full API Gateway management across all APIs in the account/region, not just this project's | oidc.tf:155 | Scope to project REST API: `arn:aws:apigateway:${region}::/restapis/*/` or accept the risk with an explicit ADR comment |
| S2 | P2 | `TagRead` statement grants 7 actions on `*` resources — several (lambda:ListTags, sqs:ListQueueTags, cloudwatch:ListTagsForResource) support resource-level scoping | oidc.tf:215 | Scope the ones that support it to project ARNs; only use `*` for SSM/events if truly needed |
| S3 | P3 | Trust policy `repo:schuster-buddy-bot@*/news-gathering-aws@*` uses `@*` wildcards for org/repo IDs — broad but bounded by the environment condition | oidc.tf:40-41 | Pin exact org/repo IDs from CloudTrail once confirmed; drop the `@*` wildcards |
| S4 | P3 | Hardcoded region in 2 workflow files — no single edit point for region migration | deploy.yml:44, drift-detection.yml:39 | Use workflow-level `env: AWS_REGION: eu-central-1` or restore `vars.AWS_REGION` with a committed fallback |

**Positives:** Self-modification risk addressed (github_actions role excluded from IAMManage). Trust policy scoped to repo + environment. No secrets in code. `id-token: write` permission correctly set.

### Architecture Strategist

| # | Sev | Finding | File | Recommendation |
|---|-----|---------|------|----------------|
| A1 | P2 | Deploy and Drift workflows share an OIDC role — Deploy needs write perms (terraform apply), Drift only needs read (terraform plan). Single role violates least-privilege by combining both | oidc.tf | Split into `github-actions-deploy` (write) and `github-actions-readonly` (plan/drift only) roles |
| A2 | P3 | Region hardcoded in 3 files (main.tf:21, deploy.yml:44, drift-detection.yml:39) — no single edit point | all | Per-file `env:` block or restore repo variable |
| A3 | P3 | 14 statements in one inline policy is fine at this scale; splitting is premature | oidc.tf | Revisit only if readonly-split (A1) lands, then policies become per-principal files |

**Positives:** `data.aws_iam_policy_document` (vs raw JSON) is right. Statement-per-service with meaningful SIDs is right. Inline-on-role fits current scale. Trust policy two-pattern design functionally sound. Concurrency controls + health check retained.

### Code Simplicity

| # | Sev | Finding | File | Recommendation |
|---|-----|---------|------|----------------|
| C1 | P2 | IAMManage (11 actions on 3 roles) and IAMRead (6 actions on `*`) overlap — 5 actions appear in both | oidc.tf:90-105, 108-118 | Merge IAMRead into IAMManage with `*` resources for the read actions, or drop the overlapping actions from one statement |
| C2 | P3 | 9-commit trial-and-error history suggests the policy was built by error-chasing, not principled design | git log | Squash-merge would hide this, but the lesson is: enumerate permissions from `terraform plan` + provider docs before writing IAM policies |
| C3 | P3 | Trust policy could be one exact pattern instead of two if the exact org/repo IDs are known | oidc.tf:38-42 | Pin exact IDs from CloudTrail, drop the legacy pattern |

**Positives:** Comments are clear and consistent. SIDs are descriptive. The critic-driven hardening commit (3515748) shows good iterative improvement.

### Pattern Recognition

| # | Sev | Finding | File | Recommendation |
|---|-----|---------|------|----------------|
| P1-1 | P1 | 9 direct-to-main pushes, each triggering a full production `terraform apply -auto-approve` against live AWS — violates repo convention "Always use PRs" | git log | Future IAM changes: enumerate permissions in one pass (from plan + CloudTrail), use a PR, not fix-forward on main |
| P1-2 | P2 | IAMManage lists 3 roles (pipeline, api, search) but codebase also has `aws_iam_role.demo` and `aws_iam_role.apigw_cloudwatch` — any PutRolePolicy/GetRolePolicy on those → AccessDenied | oidc.tf:98-100 | Add demo + apigw_cloudwatch role ARNs to IAMManage resources |
| P1-3 | P2 | Missing `iam:TagRole`, `iam:UntagRole`, `iam:ListRoleTags` — provider sets `default_tags` on all resources, so creating/re-tagging any role triggers TagRole | oidc.tf | Add tag actions to IAMManage, or add a role-specific tag statement |
| P2-1 | P2 | Action wildcards (`lambda:*`, `s3:*`, `sqs:*`, `events:*`, `apigateway:*`) contradict CONTRIBUTING.md: "Least-privilege IAM: every new permission gets a scoped ARN, no wildcard resources" | oidc.tf:88,130,145,210,260 | Enumerate actions explicitly, or document the wildcard decision as an ADR |
| P2-2 | P3 | TagRead comment says "AWS doesn't support resource-level scoping" — several entries actually do support it | oidc.tf:212-213 | Correct the comment; scope the ones that support resource-level perms |
| P3-1 | P3 | Duplicated workflow scaffolding (OIDC block, terraform setup) across deploy + drift | both workflows | Extract composite action `.github/actions/terraform-setup` |
| P3-2 | P3 | Thumbprint is legacy — AWS now validates against root CA chain, the pinned value is vestigial | oidc.tf:23 | Add comment noting the value is vestigial; harmless but prevents future "fix" attempts |

**Positives:** Naming conventions consistent (`${var.project}-*` prefixing). OIDC approach matches DECISIONS.md ADR stack. Pipeline integration correct (id-token:write, environment gating, concurrency, health check).

### Performance Oracle

| # | Sev | Finding | File | Recommendation |
|---|-----|---------|------|----------------|
| F1 | P2 | No pip/Terraform provider caching — every deploy re-downloads from scratch (~25-50s wasted) | deploy.yml | Add `actions/cache` for pip + `.terraform/providers` |
| F2 | P2 | Full `terraform apply` on demo-only commits (e.g. index.html change triggers Lambda rebuild + apply) | deploy.yml | Add path filters: `on.push.paths: ['infra/**', 'src/**', 'build.sh']` separate from demo deploy |
| F3 | P3 | 3 sequential `terraform output` calls, each re-reads state from S3 | deploy.yml:78,88,96 | Single `terraform output -json > /tmp/tfout.json` + `jq -r` |
| F4 | P3 | Health check uses fixed `sleep 5` + one-shot curl | deploy.yml:99 | `curl --retry 6 --retry-delay 2 --retry-all-errors` |
| F5 | P3 | OIDC thumbprint is vestigial — AWS ignores it, could cause confusion if it goes stale | oidc.tf:23 | Comment noting it's vestigial |

**Positives:** OIDC change is performance-neutral. ~2 min deploy is normal-to-good. Inline policy size has no IAM evaluation impact at this scale. No runtime latency impact from any changes.

---

## What's Working Well

1. **OIDC bootstrap works end-to-end** — 2 consecutive green deploys via keyless auth
2. **Self-modification risk addressed** — github_actions role excluded from its own manage scope
3. **Trust policy properly scoped** — repo + environment, not account-wide
4. **i18n is live** — 108 strings EN/DE, 36 data-i18n attributes, verified via curl
5. **Critic review from failure-check was acted on** — hardening commit before final deploy
6. **Terraform state in sync** — local + CI apply match
7. **Tests all green** — 82/82, ruff, tf fmt, tf validate

## Summary

**Overall assessment:** The OIDC bootstrap is functional and secure enough for production. The main weaknesses are:

1. **P1-1 (process):** 9 direct-to-main pushes with production deploys for debugging — use PRs + permission enumeration next time
2. **P2 (security):** `apigateway:*` on account-wide resources, TagRead over-scoping, missing demo/apigw roles in IAMManage
3. **P2 (architecture):** Single role for deploy + drift (should split read/write)
4. **P2 (patterns):** Action wildcards contradict CONTRIBUTING.md — document as ADR or enumerate
5. **P2 (performance):** No CI caching, full apply on demo-only commits

**Recommended actions (in priority order):**
1. Add demo + apigw_cloudwatch roles to IAMManage (prevents next AccessDenied)
2. Add iam:TagRole/UntagRole/ListRoleTags (prevents tag-related failure)
3. Document action wildcards as ADR or enumerate specific actions
4. Scope apigateway permissions to project REST API
5. Add CI caching (pip + terraform providers)
6. Split deploy/readonly roles (architecture improvement)

**Scope gate:** ✅ Review covers original scope (OIDC bootstrap). No issues change project direction. No P1 blocking findings on the code itself (P1-1 is a process finding).