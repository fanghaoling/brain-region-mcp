# design-review-mcp

AI Design Review Framework — 多模型对抗设计审查 MCP 工具。

把设计文档/代码 fan-out 给多个不同厂商大模型并行审查，结合项目知识库检索注入历史踩坑，按 canonical 归一 + 校准共识汇总，提高规划质量。

## 架构

全插件化（所有项目特定逻辑进 adapter，core 项目无关）：

| 可换层 | 协议 | 默认实现 |
|---|---|---|
| `ModelBackend`（调用层实现） | `async complete(...)` | `LiteLLMBackend` |
| `KnowledgeProvider` | `retrieve/list_cases/add_case` | `YamlKnowledgeProvider` |
| `ProjectAdapter` | `read_context/version/convention + reviewers/knowledge` | `UnityAdapter` / `GenericAdapter` |
| `ReportRenderer` | `render(ReviewReport)` | `MarkdownRenderer` / `JSONRenderer` |
| `Stage`（Pipeline 步骤） | `process(ctx)->ctx` | retrieve/context/prompt/review/parse/normalize/consensus/score |

日后加 RustAdapter/CppAdapter/WebAdapter 只加 adapter 包，不动 core。

## Review Pipeline

```
ReviewDocument → RetrieveStage → ContextStage → PromptStage → ReviewStage(fan-out)
              → ParseStage → NormalizeStage(canonical) → ConsensusStage → ScoreStage → ReviewReport
```

防冷门技术栈"共谋错误"：强制 `evidence_quote`（无引用丢弃）+ 知识库 RAG（版本过滤）+ 角色化 reviewer（独立 system_prompt+采样）+ canonical normalize（防同义漏报）+ calibrated confidence。

## 安装

```bash
cd Tools/design-review-mcp
uv sync
```

## 配置

API key 走环境变量（litellm 约定）：

| 模型 | env | model 字符串 |
|---|---|---|
| OpenAI GPT-5 | `OPENAI_API_KEY` | `gpt-5` |
| Anthropic Claude | `ANTHROPIC_API_KEY` | `claude-opus-4-8` / `claude-sonnet-4-6` |
| 火山豆包 | `ARK_API_KEY` | `volcengine/<ARK_ENDPOINT_ID>` |
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek/deepseek-chat` |
| 智谱 GLM | `ZAI_API_KEY` | `zai/glm-4.7` |

> ⚠️ `litellm>=1.83.0`（1.82.7/1.82.8 被投毒，已 pin 排除）。

## 注册到 Claude Code

在 `~/.claude.json` 的对应项目 `mcpServers` 加：

```jsonc
"design-review": {
  "type": "stdio",
  "command": "uv",
  "args": ["run", "--directory", "<项目>/Tools/design-review-mcp", "design-review-mcp"],
  "env": {
    "UNITY_PROJECT_ROOT": "<项目>",
    "OPENAI_API_KEY": "...",
    "ANTHROPIC_API_KEY": "...",
    "ARK_API_KEY": "..."
  }
}
```

## 开发

```bash
uv run pytest tests/
```

## License

Apache-2.0
