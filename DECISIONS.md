# Architecture Decision Records (ADR-Light)

This document records the key engineering decisions behind **news-gathering-aws**,
the alternatives considered, and the trade-offs that shaped the final design.

> **Why this file exists:** A project without documented decisions looks like a
> tutorial follow-along. These records show the reasoning — not just the result.

---

## ADR-1 — Serverless (Lambda + API Gateway) vs Container (ECS/Fargate)

**Decision:** AWS Lambda + API Gateway for all compute.

**Context:** The pipeline runs once daily (~90s execution) and serves a
low-traffic read API. A container-based deployment (ECS/Fargate or EC2)
would mean paying for idle infrastructure 24/7 for a workload that needs
~90 seconds of compute per day.

**Alternatives considered:**
- **ECS/Fargate** — More control over runtime, but over-provisioned for a
  daily cron + low-QPS API. Minimum cost ~$10-15/month even with zero traffic.
- **EC2 t3.micro** — Cheapest option (free tier), but requires OS patching,
  SSH access management, and still pays for idle time.
- **Lambda** — Pay-per-invocation, 15-min timeout is plenty for 90s workload,
  zero idle cost, automatic scaling for the API.

**Trade-off:** Cold starts add ~200-500ms latency to the first API request
after idle. Acceptable for a portfolio/demo API. Mitigated by SSM-based
warm-start (see ADR-6).

---

## ADR-2 — DynamoDB vs RDS (PostgreSQL) for Article Storage

**Decision:** Amazon DynamoDB (single-table design, TTL-managed).

**Context:** The pipeline stores ~300 articles/day with 14-day retention
(~4,200 items max). Access patterns are simple: put, get-by-hash, scan for
search, TTL-expire. No relational queries, no joins, no complex aggregations.

**Alternatives considered:**
- **RDS PostgreSQL** — Full SQL, pgvector for semantic search, but:
  - Free tier is 12-month only (then ~$15-20/month)
  - Provisioned even when idle
  - Overkill for key-value + scan access patterns
  - Requires VPC, security groups, subnet management
- **DynamoDB** — 25 GB always-free (no expiry), single-digit-ms latency,
  TTL for automatic expiry (no cleanup Lambda needed), pay-per-request
  billing means $0 when idle.

**Trade-off:** No SQL queries, no pgvector. Semantic search is implemented
as scan-and-cosine in the Lambda (see ADR-3). This works because the corpus
is small (~4K items) and stays within free-tier RCU. At 100K+ items this
would need OpenSearch or a vector database.

---

## ADR-3 — Scan-and-Cosine Search vs OpenSearch / pgvector

**Decision:** Full-scan cosine similarity in Lambda over DynamoDB items.

**Context:** Semantic search needs cosine similarity between query embedding
and all article embeddings. With ~4,200 items × 256 dims × 4 bytes = ~4.1 MB
of vector data per scan, this completes in <1 second within a single Lambda
invocation.

**Alternatives considered:**
- **Amazon OpenSearch Serverless** — Native vector search, k-NN with HNSW.
  But: minimum ~$100/month even at zero traffic (4 OCU minimum). Eliminates
  the $0/month target.
- **pgvector on RDS** — Good vector search, but requires RDS (see ADR-2
  trade-offs) and 12-month free-tier limit.
- **FAISS in Lambda** — Faster than numpy, but adds a native dependency
  that complicates the Lambda build. Overkill at this scale.
- **Scan-and-cosine (numpy)** — Decodes Base64 vectors from DynamoDB scan,
  computes cosine similarity in numpy. Simple, no extra infrastructure,
  ~400ms p95 for 4K items. Good enough.

**Trade-off:** O(n) scan cost. At 10K+ items this would exceed Lambda's
15-min timeout or DynamoDB free-tier RCU. The 14-day TTL is the natural
scaling limit. For a production system, migrating to OpenSearch or
pgvector would be the right call — documented as a known limitation.

---

## ADR-4 — Ollama (External API) vs AWS Bedrock for AI Summaries

**Decision:** Ollama cloud API for summaries, with Bedrock Titan V2 for
embeddings (code ready, pending quota activation).

**Context:** AI summaries need a chat model. AWS Bedrock would keep
everything in-account, but the Bedrock quota for our AWS account was not
provisioned at build time (pending AWS support ticket).

**Alternatives considered:**
- **AWS Bedrock (Claude / Titan)** — In-account, no external API key,
  native AWS integration. Blocked on quota provisioning. Code is written
  and ready to flip via SSM parameter (no redeploy needed).
- **OpenAI API** — Best model quality, but adds a third-party dependency
  and costs ~$20/month for daily summaries.
- **Self-hosted Ollama** — Would require a GPU instance (not free-tier).
  Using Ollama's hosted API gives us model access at ~$0.50/month.
- **Ollama cloud API** — Cheapest, good enough quality, works today.
  External dependency (API key in SSM).

**Trade-off:** External API dependency for summaries. If Ollama is down,
the pipeline degrades gracefully (falls back to raw article descriptions).
The Bedrock flip is a single SSM parameter change — no code change needed.

---

## ADR-5 — Terraform vs AWS CDK vs Serverless Framework

**Decision:** Terraform (~1.9).

**Context:** Infrastructure as Code is required for reproducibility and
portfolio signal. The choice is between Terraform, AWS CDK (TypeScript),
and Serverless Framework.

**Alternatives considered:**
- **AWS CDK** — TypeScript, tighter AWS integration, but introduces a
  Node.js build step and synthesizes to CloudFormation (less portable).
  Good for AWS-only shops, but Terraform is more broadly valued as a skill.
- **Serverless Framework** — Fastest for Lambda-first projects, but
  abstracts away too much infrastructure detail. Hides the IAM/resource
  reasoning that makes a portfolio project valuable.
- **Terraform** — Provider-agnostic (shows multi-cloud awareness),
  explicit resource definitions (better for learning/demonstrating IAM),
  HCL is declarative and readable, broadest industry adoption.

**Trade-off:** More boilerplate than CDK or Serverless Framework. No
built-in Lambda deployment packaging (we use `archive_file` data source
+ a shell build script). This is acceptable — the explicitness is a
portfolio signal, not a liability.

---

## ADR-6 — SSM Parameter Store vs Secrets Manager vs Lambda Env Vars

**Decision:** AWS SSM Parameter Store (SecureString) for all secrets.

**Context:** Two secrets need management: the Ollama API key and the API
Gateway key. Lambda environment variables are visible in the AWS console
and stored in plaintext — not suitable for secrets.

**Alternatives considered:**
- **AWS Secrets Manager** — Purpose-built for secrets, automatic rotation,
  but $0.40/secret/month = ~$1/month for 2-3 secrets. Breaks $0 target.
- **Lambda environment variables** — Free, but plaintext in console and
  CloudTrail. Not acceptable for API keys.
- **SSM Parameter Store (SecureString)** — Free tier, KMS-encrypted at
  rest, decrypted at runtime via Lambda IAM role. No rotation, but
  acceptable for a portfolio project with low key churn.

**Trade-off:** No automatic key rotation (Secrets Manager provides this).
For a production system with frequent key changes, migration to Secrets
Manager would be warranted. The SSM approach also enables a "warm-start"
optimization: the Lambda fetches SSM parameters on cold start and caches
them in the handler closure, reducing first-request latency by ~200ms.

---

## ADR-7 — API Key + Usage Plan vs Cognito vs IAM Auth

**Decision:** API Gateway API key + usage plan throttling.

**Context:** The `/search` and `/report/latest` endpoints need access
control. The public demo (`/demo/*`) needs no auth but must be throttled.

**Alternatives considered:**
- **Amazon Cognito** — Full user management, JWT tokens, but overkill for
  a single-key portfolio API. Adds complexity (user pool, app client,
  token refresh logic) without proportional value.
- **IAM auth (SIGv4)** — Secure, but requires callers to have AWS
  credentials. Not practical for a public demo or portfolio review.
- **Custom Lambda authorizer** — Flexible, but reinvents API key logic
  that API Gateway provides natively.
- **API key + usage plan** — Native API Gateway feature, $0 additional
  cost, throttling built in, simple to configure in Terraform.

**Trade-off:** API keys are passed in headers (`x-api-key`), which is less
secure than JWT. Keys don't expire automatically. For a portfolio API this
is the right balance of security and simplicity.

---

## ADR-8 — CQRS Split: Public Demo (Read-Only) vs Private API (Full Access)

**Decision:** Separate demo Lambda with read-only IAM role + separate
API routes (`/demo/*`), distinct from the authenticated API.

**Context:** The portfolio should be publicly accessible without an API key,
but the write-side endpoints (pipeline trigger) must remain private.

**Alternatives considered:**
- **Same Lambda, path-based auth** — One handler checks API key for
  private routes, skips for demo routes. Simpler, but violates
  least-privilege: the demo Lambda would have write IAM permissions.
- **API Gateway with optional auth** — Reduces to one Lambda, but the IAM
  role still needs write access for the private routes.
- **CQRS split (chosen)** — Two Lambdas: `news-pipeline-api` (has API key,
  full DynamoDB/S3 access) and `news-pipeline-demo` (no API key, read-only
  IAM: only `Query`/`Scan` on DynamoDB, only `GetObject` on S3). Separate
  API Gateway routes `/demo/*` with throttling (5 rps, burst 10).

**Trade-off:** Two Lambda functions and two IAM roles to maintain.
Duplicates some query logic. But the security guarantee (demo Lambda
cannot write to DynamoDB even if compromised) is worth the duplication.

---

## Known Limitations & Future Improvements

| Area | Current State | Production Upgrade Path |
|---|---|---|
| Vector search | O(n) scan-and-cosine (~4K items) | OpenSearch Serverless or pgvector |
| AI summaries | External Ollama API | AWS Bedrock (code ready, pending quota) |
| Key rotation | Manual SSM updates | AWS Secrets Manager with auto-rotation |
| Auth | Static API key | Cognito + JWT or OAuth2 |
| Monitoring | CloudWatch logs | X-Ray tracing + custom metrics + alerts |
| Eval | No systematic evaluation harness | Golden dataset + regression tracking (planned) |
| Deployment | Auto-deploy on push to main | Blue/green or canary with traffic shifting |
| GraphRAG query | Phase 1: ERE + graph storage only | Phase 2: hybrid vector+graph `/insights` endpoint |

## ADR-10 — Graph Storage: DynamoDB Adjacency List vs Amazon Neptune

**Date:** 2026-10-08
**Status:** Accepted

### Context
GraphRAG Phase 1 needs a knowledge graph to store entities and relations extracted from news articles. The graph will support multi-hop traversal for a future `/insights` endpoint.

### Decision
Use DynamoDB with an adjacency-list pattern and a reverse-traversal GSI instead of Amazon Neptune.

### Rationale
1. **Cost:** DynamoDB PAY_PER_REQUEST is $0/month when idle; Neptune serverless has a minimum hourly cost.
2. **No VPC:** Neptune requires a VPC; DynamoDB is serverless and IAM-scoped.
3. **Scale fit:** The daily article volume (~300) produces at most a few thousand nodes/edges, easily handled by DynamoDB queries and `batch_get_item`.
4. **TTL symmetry:** The same `ttl` attribute can expire graph items alongside article dedup rows, keeping storage bounded.

### Schema
- `PK = NODE#<normalized_entity>`, `SK = META#` for entity metadata.
- `SK = EDGE#<target>#<relation>` for outgoing edges.
- GSI1 (`GSI1PK = NODE#<target>`, `GSI1SK = EDGE#<source>#<relation>`) for reverse traversal.

### Trade-off
DynamoDB is not optimized for complex graph algorithms (PageRank, betweenness). For Phase 3 we load the subgraph into NetworkX in the Lambda for metric computation, accepting the Lambda timeout and memory constraints.

---

## ADR-11 — LLM Orchestration: LangChain Core + Provider Factory

**Date:** 2026-10-08
**Status:** Accepted

### Context
The pipeline needs structured output (JSON triplets + summary) from a chat model. We want to switch between Ollama (today) and AWS Bedrock (future) without code changes.

### Decision
Use `langchain-core` with lightweight provider packages (`langchain-ollama`, `langchain-aws`) and a factory function `get_llm(provider, model, **kwargs)`.

### Rationale
1. **Structured output:** `with_structured_output(Pydantic, include_raw=True)` gives us typed extraction plus raw response access for debugging/fallback.
2. **Swappable providers:** The provider name is read from SSM `/news-pipeline/llm-provider`; changing it flips the model without a redeploy.
3. **Lazy imports:** Provider classes are imported inside the factory function so unused providers do not add cold-start cost.
4. **Size budget:** The dependency closure (langchain-core + langchain-ollama + langchain-aws + networkx + pydantic) adds ~27MB, keeping the deploy ZIP well under 250MB.

### Trade-off
LangChain adds an abstraction layer. For a portfolio project the provider-switching and structured-output benefits outweigh the added complexity.

---

## ADR-12 — Graph Algorithms: NetworkX Inside the Lambda

**Date:** 2026-10-08
**Status:** Accepted

### Context
Phase 3 needs PageRank and betweenness centrality over the knowledge graph to identify influential entities in the daily report.

### Decision
Run NetworkX 3.7 inside the pipeline Lambda, loading the graph from DynamoDB into memory.

### Rationale
1. **No extra infrastructure:** NetworkX is pure Python and ships in the Lambda package.
2. **Performance budget:** PageRank on 27K nodes completes in ~0.5s; k-sampled betweenness (`k=300`) fits within the 15-minute Lambda timeout.
3. **Control:** The algorithm parameters and random seed are code-defined, making results reproducible.

### Trade-off
Exact betweenness centrality on large graphs is infeasible in Lambda. We will use `nx.betweenness_centrality(G, k=300, seed=42)` as an approximation and document it as such.

---

## ADR-9 — OIDC Federation: Action Wildcards vs Enumerated Permissions

**Date:** 2026-10-08
**Status:** Accepted

### Context
The GitHub Actions OIDC role (`news-pipeline-github-actions-role`) needs permissions for `terraform apply` to manage all project resources. The initial policy used ~15 enumerated actions per service, which proved insufficient — Terraform's `plan` phase reads tags, continuous backups, bucket policies, queue attributes, and other metadata that isn't obvious from the resource declarations alone.

This led to 7 iterations of fix → fail → fix cycles during the OIDC bootstrap, each requiring a full CI roundtrip (~2 min per failed deploy).

### Decision
Use **service-scoped action wildcards** (`lambda:*`, `s3:*`, `dynamodb:*`, `sqs:*`, `events:*`, `logs:*`) on **project-resource-prefix-scoped** ARNs, rather than enumerating every specific action.

### Rationale
1. **Terraform's permission surface is large and unstable** — AWS adds new read operations that Terraform calls during `plan`. Enumerated permissions break on provider updates.
2. **Resource scoping provides the security boundary** — wildcards are on `news-pipeline-*` resources only, not account-wide.
3. **Exceptions documented** — `apigateway:*` is scoped to the project REST API ID (not all APIs). Tag-read operations use `*` resources where AWS doesn't support resource-level scoping (documented as AWS limitation).
4. **Self-modification prevented** — the OIDC role cannot edit its own policy (excluded from IAMManage resources).

### Accepted Risks
- **Blast radius:** A compromised Actions run can attach inline policies to pipeline/api/search/demo roles via `iam:PutRolePolicy`, then invoke Lambda functions to access project secrets (SSM SecureStrings). This is contained within the project boundary — no account pivot is possible (no trust policy edits, no CreateAccessKey, no OIDC provider mutation).
- **Mitigation:** `aws-deploy` environment protection rules (required reviewer) should be enabled to gate deploys. See P2-4 from security review.

### Alternatives Considered
- **Enumerated actions** (rejected: 7 iterations to discover all needed permissions, fragile to provider updates)
- **AdministratorAccess** (rejected: no resource scoping, full account access)
- **Separate plan/apply roles** (deferred: P2 from architecture review, future work)

### Related
- Security review: `docs/reviews/2026-10-08-oidc-bootstrap-review.md`
- IAM policy: `infra/oidc.tf`
