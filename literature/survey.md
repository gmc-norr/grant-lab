# Task-aware model routing survey

## Review protocol

- Research area: user-facing model choice and automatic task-based LLM routing.
- Key questions:
  - Do established AI products expose explicit model names, an automatic mode, task-specific modes, or a combination?
  - Where is model choice placed so frequent workflows remain uncluttered?
  - Which measurements are recommended when selecting or routing models?
  - What cautions apply when comparing a specialized editing product with an agent-and-tool workflow?
- Inclusion criteria: current primary product documentation, official engineering guidance, or research that directly addresses model selection, routing, latency, quality, cost, or user control.
- Exclusion criteria: unsupported social-media claims, affiliate comparisons, generic model rankings, and sources that discuss only training-time mixtures of experts.
- Sources to search: official OpenAI, Anthropic, GitHub Copilot, Cursor, and model-gateway documentation; arXiv or conference literature on LLM routing; practitioner discussions only when they add a distinct UX concern.
- Search structure: `(model selector OR auto model OR model routing) AND (task OR latency OR quality OR cost OR UX)` plus product-specific variants.
- Screening: title/snippet screening, then full-page verification. Product behavior requires a primary source; commentary is labeled separately.

## Screening log

### Included primary product and engineering sources

| Source | Decision | Relevant verified evidence |
|---|---|---|
| [GitHub Copilot auto model selection](https://docs.github.com/en/copilot/concepts/auto-model-selection) and [manual model selection](https://docs.github.com/en/copilot/how-tos/use-ai-models/change-the-chat-model) | Include | Copilot offers Auto and a secondary model picker. Its task-optimized Auto combines task complexity with system health, routes at cache boundaries, reserves higher-cost reasoning models for harder work, obeys plan/admin restrictions, and reveals the actual model per response. GitHub also documents availability-only Auto variants, so the word `Auto` alone does not imply task routing. |
| [Cursor models and pricing](https://cursor.com/docs/models) | Include | Cursor retains direct model choice and offers Auto modes named `Cost`, `Balance`, and `Intelligence`. This is a capability/cost abstraction over model identifiers, not a claim that all Auto modes minimize latency. |
| [GPT-5.6 in ChatGPT](https://help.openai.com/en/articles/11909943-gpt-52-in-chatgpt) | Include | ChatGPT can automatically increase reasoning for complex requests while `Instant` remains selected. Its primary paid-plan control is a reasoning scale (`Instant`, `Medium`, `High`, `Extra High`, `Pro`), while some underlying family variants are not directly selectable. |
| [Canvas in ChatGPT](https://help.openai.com/en/articles/9930697-canvas-in-chatgpt) | Include with caveat | The documentation verifies selection-oriented editing and writing shortcuts. It does not attribute Canvas responsiveness to a particular model or disclose a distinct routing architecture, so Canvas cannot support a model-performance claim. |
| [Claude model, effort, and thinking settings](https://support.claude.com/en/articles/8664678-change-the-model-effort-and-thinking-settings) | Include | Claude groups model, effort, and thinking settings in one compact control beside Send. It is evidence for progressive disclosure of expert controls, not automatic task routing. |
| [Anthropic: Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) | Include | Anthropic recommends the simplest adequate architecture, notes that agentic systems trade latency and cost for performance, prefers predictable workflows for well-defined tasks, and gives easy-to-small/hard-to-capable model routing as a workflow example. Complexity should be added only when measured outcomes improve. |
| [OpenRouter Auto Router](https://openrouter.ai/docs/guides/routing/routers/auto-router) | Include | The router classifies prompts by task, applies a cost tier, honors allowed/excluded models and policy constraints, uses fallbacks and session stickiness, and returns the actual selected model. Its ranking signal is aggregate spend share, which is not the same as measured quality for this application. |
| [RouteLLM](https://arxiv.org/abs/2406.18665) | Include | Preference-trained routers choose between stronger and weaker models to optimize response quality and cost. The paper reports benchmark savings, but does not establish quality or latency for proposal editing. |
| [Beyond Accuracy and Cost: Latency-Aware LLM Query Routing for Dynamic Workloads](https://arxiv.org/html/2607.18253) | Include | The router jointly models quality, cost, and TTFT. It defines TTFT as queueing plus prefill/first-token computation and shows that current instance load and serving policy affect latency. Experiments use self-hosted Qwen models on H100s, so the numerical results do not transfer directly to a hosted Claude workflow. |
| [NVIDIA NIM LLM benchmarking metrics](https://docs.nvidia.com/nim/benchmarking/llm/latest/metrics.html) | Include for definitions | Separates time to first token, inter-token latency, and end-to-end request latency. These are distinct user-visible dimensions and should not be collapsed into one model-speed label. |

### Excluded or bounded sources

- Unsupported social posts, affiliate model comparisons, and generic leaderboards were excluded because they do not establish behavior for this workflow.
- OpenAI Canvas extraction was intermittent; only interaction behavior visible in the official documentation is used. No inference is made about its model or backend.
- Nielsen Norman Group progressive-disclosure pages could not be reliably extracted in this run. The UX recommendation below is therefore grounded in observed primary-product controls, not attributed to that source.
- Microsoft Research pages and arXiv `2401.14414` / `2501.01821` were screened but added no necessary evidence beyond the directly relevant routing sources above.
- Product documentation changes quickly. These observations are a dated snapshot, not stable API contracts.

## Evidence map

### 1. Auto is common, but its semantics must be explicit

The strongest direct example is GitHub Copilot: its task-optimized Auto evaluates task complexity and live availability, limits candidates through subscription and policy rules, avoids arbitrary mid-session switching at cache-sensitive boundaries, and makes the selected model inspectable. OpenRouter similarly combines task classification, policy constraints, cost preference, fallbacks, model reporting, and conversational stickiness. OpenAI automates reasoning escalation while presenting an outcome-oriented control rather than requiring a model-family decision.

This supports an Auto default, but not an opaque `best model` promise. GitHub separately documents Auto modes optimized only for reliability and availability, and OpenRouter's community-spend ranking is a proxy rather than application-specific evidence. Proposal-assist therefore needs a stated routing objective and a restricted candidate set.

### 2. Expert control is secondary and often outcome-oriented

The products surveyed do not converge on hiding model identity completely. GitHub and Cursor retain direct model pickers; Claude combines model, effort, and thinking in a compact composer control. However, OpenAI exposes speed/reasoning levels, Cursor exposes `Cost` / `Balance` / `Intelligence`, and OpenRouter exposes cost tiers. The recurring pattern is a low-friction default plus a compact override, with the routine action surface left alone.

For proposal-assist, a raw model selector on every `Ask`, `Edit`, or `Cite` action would make users solve an orchestration problem repeatedly. If an expert override is added, it belongs in workspace or conversation settings, should default to `Auto`, and should describe outcomes such as `Fast`, `Balanced`, and `Deep review`. Exact model names can remain inspectable metadata or an advanced allowlisted choice.

### 3. Workflow choice may dominate model choice for selected edits

Anthropic distinguishes predictable workflows from general agents and explicitly warns that agentic complexity can increase latency and cost. A selected-text rewrite is bounded: the input range, intended operation, and output location are known. That makes a constrained edit workflow a plausible alternative to sending every edit through a general tool-using agent.

This is an architectural hypothesis, not a measured conclusion. Citation gathering, cross-document consistency checks, and open-ended proposal analysis may still justify agent behavior. The application should classify the operation before choosing both the workflow and the model.

### 4. Measure the interaction, not the model label

The routing literature treats quality and cost jointly; latency-aware work adds TTFT and current serving load. TTFT itself includes queueing and prefill, so changing from Opus to a nominally faster model does not prove that a selected edit will feel faster. This application's own queue polling, workspace startup, prompt/context size, tool loop, output generation, and commit path can all contribute.

Minimum per-turn telemetry should record:

- operation class and selection/context sizes;
- requested policy or override and actual model used;
- queue wait, workspace/client startup, provider TTFT, generation duration, tool time, commit time, and end-to-end latency;
- input/output tokens and estimated cost;
- success, retry, cancellation, accept/reject, and subsequent manual correction signals.

Quality evaluation must be task-specific. For selected edits, useful gates include instruction adherence, preservation outside the selected range, factual/citation integrity where applicable, and human acceptance without repair. Compare candidate policies on matched tasks rather than relying on generic model rankings.

## Synthesis for proposal-assist

1. Keep `Ask`, `Edit`, and `Cite` model-agnostic. Do not add a model control to the selection toolbar.
2. First instrument the existing Opus path and split queue, startup, provider, tool, and commit latency.
3. Build a constrained selected-edit path and compare it with the current general agent on quality and end-to-end latency.
4. If multiple models are justified by those measurements, introduce `Auto` at the conversation/workspace level. Start with deterministic operation classes and an allowlisted policy rather than an unconstrained learned router.
5. Add a compact advanced override outside the selection toolbar. Prefer capability labels for normal use; show exact allowed models only when users need expert control.
6. Persist both the requested policy/override and the actual model for every turn. Surface actual-model metadata unobtrusively for audit and debugging.
7. Keep routing sticky within a coherent editing sequence unless the operation changes or a failover is required. Account explicitly for provider prompt-cache behavior before per-turn switching.
8. Promote a routing rule only when a locked evaluation shows a worthwhile quality, latency, or cost improvement. Preserve a fixed-model fallback and policy-enforced candidate set.

## Open questions

- What quality loss, if any, is acceptable for a selected rewrite in exchange for lower median and tail latency?
- Which operations are genuinely bounded (`rewrite selection`, `shorten`, `change tone`) and which require workspace tools or cross-document evidence?
- Can the hosted provider expose reliable TTFT separately from local queue and client startup time?
- How much of the observed delay comes from the five-second poll interval, cold workspace/client startup, prompt prefill, model generation, tool use, and persistence?
- Does switching models on the persistent SDK client preserve enough prompt-cache value, and at what boundary?
- Which models are permitted under hospital data-processing, residency, retention, and procurement constraints?
- Should an override apply to one turn, the conversation, or the proposal workspace? The product examples support a compact persistent control, but this requires user testing.