# AAE Codex Adapter

Native Codex integration for [Adaptive Agentic Engineering](https://github.com/Wolfeitz/adaptive-agentic-engineering).

The adapter uses Codex-owned capabilities rather than imitating them:

- `AGENTS.md` for portable project instructions;
- `.agents/skills` for native progressive skill discovery;
- `.codex/hooks.json` for lifecycle events and native trust review;
- `.codex/agents` for fresh read-only independent review;
- Codex's own MCP, rules, sandbox, approvals, plugins, and app-server when a task needs them.

```bash
python -m pip install -e ../adaptive-agentic-engineering -e .
aae-codex init /path/to/project
aae-codex doctor /path/to/project
```

`init` preserves existing Codex configuration. `sync-skills` updates only files
marked as adapter-generated; it never replaces a hand-authored native skill.
Review and approve project hooks through Codex's native `/hooks` interface.

`capabilities.json` records the currently verified surface. A scheduled job
tests the latest Codex release; upstream drift fails visibly and requires a
reviewed adapter update.
