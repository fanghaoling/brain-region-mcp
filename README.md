# design-review-mcp

[English](README.md) | [简体中文](README.zh-CN.md)

`design-review-mcp` is an MCP server and CLI for adversarial design review. It fans out a plan, source change, or
document to multiple LLM reviewer roles, retrieves project-specific knowledge, normalizes duplicate findings, and returns
consensus-oriented reports that are easier to act on.

The core pipeline is project-agnostic. Project-specific behavior lives in adapters; the current package includes
`generic` and `unity` adapters, with Unity ECS/NetCode/Burst as the first deep integration target.

## Highlights

- Review plans, code, Markdown, ADRs, RFCs, and config documents.
- Use reviewer roles such as `planner`, `safety`, `architecture`, `performance`, `feasibility`, and `visionary`.
- Run one model or a panel of models, including official LiteLLM providers and OpenAI/Anthropic-compatible gateways.
- Retrieve framework and project-local knowledge before review.
- Normalize findings into canonical buckets and separate consensus, majority, and individual issues.
- Render JSON, Markdown, and SARIF output.
- Track review memory with `mark_finding` so accepted/rejected findings can influence later confidence calibration.
- Merge defaults from builtin values, global config, project config, environment variables, and explicit call arguments.

## Architecture

Most pieces are swappable. Adapter-specific behavior stays out of `core/`.

| Layer | Contract | Default implementation |
|---|---|---|
| `ModelBackend` | `async complete(...)` | `LiteLLMBackend` |
| `KnowledgeProvider` | `retrieve/list_cases/add_case` | `YamlKnowledgeProvider` |
| `ProjectAdapter` | `read_context/version/convention + reviewers/knowledge` | `UnityAdapter` / `GenericAdapter` |
| `ReportRenderer` | `render(ReviewReport)` | Markdown / JSON / SARIF renderers |
| `Stage` | `process(ctx) -> ctx` | retrieve, context, prompt, review, parse, normalize, consensus, score |

Adding another project type should usually mean adding a new adapter package, not changing the core pipeline.

## Review Pipeline

```text
ReviewDocument
  -> RetrieveStage
  -> ContextStage
  -> PromptStage
  -> ReviewStage      # fan-out across panel x dimensions
  -> ParseStage
  -> NormalizeStage   # canonical finding buckets
  -> ConsensusStage
  -> ScoreStage
  -> ReviewReport
```

The pipeline is designed to reduce "confident but unsupported" feedback:

- Findings need evidence quotes.
- Knowledge retrieval can inject project gotchas and version-specific cases.
- Reviewer prompts are role-specific.
- Canonical normalization reduces duplicate phrasing across models.
- Calibrated confidence combines model agreement, severity, retrieval hits, and review memory.

## Knowledge Base

Review quality depends heavily on project knowledge. The package includes general Unity ECS/Burst/FlowField/NetCode
seed cases under `design_review/adapters/unity/knowledge/`, but your project-specific architecture decisions and past
bugs should live in project-local knowledge files.

Recommended project-local location:

```text
<project-root>/.design-review/knowledge/*.yaml
```

Example:

```yaml
- id: MYSYSTEM-001
  title: "Avoid structural changes inside hot ECS loops"
  version: {entities: ">=1.4,<2.0"}
  triggers: ["EntityCommandBuffer", "structural change", "hot loop"]
  category: ecs_perf
  bad_pattern: "Directly create or destroy entities inside a frequently running system update."
  recommended_pattern: "Record changes into an ECB and play them back at a safe sync point."
  source: "MEMORY.md#ecs-structural-changes"
```

Tips:

- Write one concrete, reproducible gotcha per case.
- Put words that will appear in plans or code into `triggers`.
- Keep sensitive project knowledge local and ignored by git.
- Use `list_knowledge` to inspect the loaded framework and local cases.

## Installation

```bash
cd <path-to-design-review-mcp>
uv sync --extra dev
```

Run the test suite:

```bash
uv run pytest tests/ -q
uv run --extra dev ruff check .
```

## MCP Setup

Register the stdio server in Codex, Claude Code, or another MCP client:

```jsonc
{
  "type": "stdio",
  "command": "uv",
  "args": [
    "run",
    "--directory",
    "<path-to-design-review-mcp>",
    "design-review-mcp"
  ],
  "env": {
    "UNITY_PROJECT_ROOT": "<path-to-unity-project>",
    "DESIGN_REVIEW_CONFIG": "<path-to-design-review-mcp>/design_review_config.json"
  }
}
```

Keep API keys in `.env` or process environment variables. Do not commit `.env` or local `design_review_config.json`.

## CLI

The `design-review` CLI uses the same pipeline as the MCP server.

```bash
uv run design-review plan path/to/plan.md --output markdown
cat plan.md | uv run design-review plan -
uv run design-review plan --text "# Plan" --dimensions planner feasibility
uv run design-review code src/a.py src/b.py --output sarif --output-file review.sarif
uv run design-review doc docs/rfc.md --type rfc --output markdown
```

Common options:

- `--panel`: model list or endpoint shortcuts.
- `--dimensions`: reviewer dimensions.
- `--adapter`: `auto`, `generic`, or `unity`.
- `--retrieve-top-k`: number of knowledge cases to retrieve.
- `--effort`: reasoning/thinking effort where supported.
- `--max-cost-usd`: preflight budget cap.
- `--timeout`: per-model timeout.

## Configuration

Defaults are resolved in this order:

```text
builtin < global config < project config < env < explicit tool args
```

See:

- [Config precedence](docs/config_precedence.md)
- [Endpoint configuration](docs/endpoint_config.md)

Typical local config path:

```text
<path-to-design-review-mcp>/design_review_config.json
```

`design_review_config.json` can hold defaults such as:

- `panel`
- `dimensions`
- `retrieve_top_k`
- `timeout`
- `normalizer_model`
- `effort`
- `max_cost_usd`
- `endpoints`
- `privacy_policy`
- `context_modes`

## Custom Gateway Endpoints

Use `endpoints` for OpenAI-compatible or Anthropic-compatible gateways such as New API, one-api, OpenRouter-style
proxies, or internal model bridges. Use one endpoint per wire protocol.

```json
{
  "endpoints": {
    "modelbridge_openai": {
      "provider": "openai",
      "base_url": "https://www.modelbridge.cloud/v1",
      "api_key_env": "MODEBRIDGE_API_KEY",
      "models": ["gpt-5.5", "gpt-5.4-mini"]
    },
    "modelbridge_anthropic": {
      "provider": "anthropic",
      "base_url": "https://www.modelbridge.cloud",
      "api_key_env": "MODEBRIDGE_API_KEY",
      "models": ["claude-haiku-4-5"]
    }
  },
  "panel": ["modelbridge_openai/gpt-5.5", "modelbridge_anthropic/claude-haiku-4-5"]
}
```

Panel shortcuts:

- `"endpoints"` expands every declared model under every endpoint.
- `"endpoint_id"` expands every model under one endpoint.
- `"endpoint_id/model"` runs one model through one endpoint.
- Native LiteLLM strings such as `"gpt-4o"` or `"deepseek/deepseek-chat"` bypass endpoint config and use provider env vars.

## Cost And Effort Controls

Two optional controls are available:

- `max_cost_usd`: preflight cost cap for a review. Jobs are kept in panel order until the estimate would exceed the cap.
- `effort`: reasoning/thinking intensity for providers that support it. Unsupported providers ignore it.

The report includes estimated budget information and actual usage/cost where the provider returns it.

## Privacy Mode

By default, every model in the panel receives the review document. For sensitive plan reviews, `privacy_policy` can enable
a strict mode where a trusted model sees the full document, adversarial reviewers see a redacted summary, and the trusted
model later mediates evidence.

```json
{
  "privacy_policy": {
    "policy": "strict",
    "trusted": {"endpoint": "trusted_gateway", "model": "trusted-model", "label": "trusted"},
    "min_coverage": 0.5
  }
}
```

Strict privacy is most useful for plan review. Code review can lose too much semantic detail after redaction.

## Review Memory

Use `mark_finding` to record whether a finding was useful:

```text
mark_finding(finding_id="gpt-4o-3", decision="accepted", params_hash="...")
```

Valid decisions are `accepted`, `rejected`, and `partial`. Feedback is stored in the local SQLite review database and is
used to calibrate future confidence per `(model, dimension)`.

## Output

Reports include:

- `consensus`: findings all models agreed on.
- `majority`: findings supported by multiple models.
- `individual`: one-model findings.
- `failed_models`: isolated model failures.
- `budget`, `usage`, `risk`, and `context_compression` metadata.

SARIF output can be uploaded to GitHub Code Scanning or consumed by IDEs.

## Project Layout

```text
design_review/
  server.py              # MCP server entry point
  cli.py                 # design-review CLI
  core/                  # pipeline, stages, schemas, report models
  adapters/              # generic and Unity adapters
  providers/             # LLM backends
  knowledge/             # retrieval providers
  privacy/               # privacy policies
  output/                # renderers
tests/                   # pytest coverage
docs/                    # focused docs
```

## Security Notes

- Do not commit `.env`, `.env.local`, API keys, generated databases, or local `design_review_config.json` files.
- Prefer `api_key_env` over plaintext `api_key`.
- `Assets/Generated/AIGenerated/design_reviews.db` is generated local data and should not be used in tests.

## License

Apache-2.0
