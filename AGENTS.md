# AAE Codex Adapter Instructions

This repository owns Codex-specific projection and verification only. Portable
AAE behavior belongs in `adaptive-agentic-engineering`.

- Check current official OpenAI documentation before changing a native surface.
- Update `capabilities.json` and tests together.
- Preserve native Codex trust, sandbox, approval, skill, hook, agent, MCP, rule,
  plugin, and app-server behavior; do not emulate them in AAE core.
- Never persist raw hook prompts, tool responses, transcripts, or file contents.
- Do not claim a version verified until its probes and tests pass.
