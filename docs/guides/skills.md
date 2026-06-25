# Skills

A **skill** is a reusable, parameterised prompt template that the agent can
invoke as a tool. Skills are defined in `SKILL.md` files and executed as
sub-agents — either sharing the current conversation (`inline`) or in full
isolation (`fork`).

## Defining a skill

One skill lives in one directory containing a `SKILL.md` file:

```
.agent/skills/
└── review-pr/
    └── SKILL.md
```

`SKILL.md` is YAML frontmatter + a Markdown body (the prompt template):

```markdown
---
name: review-pr
description: Review a GitHub pull request for quality and security
tools:
  - Read
  - Grep
  - Glob
  - Bash
execution_mode: fork
parameters:
  pr_number:
    type: string
    description: The PR number to review
    required: true
---

Review pull request #{{ pr_number }} for:
1. Code quality and readability
2. Security vulnerabilities
3. Project-convention adherence
4. Test coverage

Run:
- gh pr view {{ pr_number }} --json title,body,files
- gh pr diff {{ pr_number }}

Then produce a structured review.
```

### Frontmatter fields

| Field | Default | Meaning |
|-------|---------|---------|
| `name` | (required) | Unique skill id. |
| `description` | `""` | Shown to the LLM for routing. |
| `tools` | `["*"]` | Tool allow-list for the skill sub-agent. |
| `model` | inherit | Model override. |
| `permission_mode` | inherit | Clamped by the parent (never more permissive). |
| `system_prompt` | `""` | Extra system prompt for the sub-agent. |
| `execution_mode` | `inline` | `inline` or `fork` (see below). |
| `background` | `false` | With `fork`, run as an async background sub-agent. |
| `parameters` | `{}` | Declared params (`type`, `description`, `required`). |

## Discovery & precedence

At `query()` startup, skills are discovered from (first-wins precedence):

1. `AgentConfig.skills_dir` → source `config`
2. `<workspace_root>/.agent/skills/` → source `project`
3. `~/.agent/skills/` → source `user`

Plugins may also contribute skills (source `plugin`). Discovery is offloaded
to a worker thread so startup never blocks the event loop.

## Execution modes

| Mode | History | Use when |
|------|---------|----------|
| `inline` | Inherits the parent message history (`parent_messages`) | The skill needs the current conversation context. |
| `fork` | Fresh history, fully isolated | The skill is self-contained (e.g. PR review). |

Both modes run through `run_agent()` — a skill **cannot** re-enter the parent
turn loop (tool execution is a leaf operation). Both therefore inherit the
hook environment and the `MAX_AGENT_DEPTH` recursion guard automatically.

## Invocation

The LLM calls the built-in `Skill` tool:

```json
{ "skill": "review-pr", "arguments": { "pr_number": "123" }, "mode": "fork" }
```

`mode` is optional and overrides the skill's default `execution_mode`.

Template placeholders `{{ name }}` (whitespace-tolerant) are substituted from
`arguments`. A missing **required** parameter aborts with an error; unknown
placeholders are left intact.

The CLI also lists loaded skills:

```
/skills
```

## Permissions

`SkillPermissionEngine` matches skill names against `deny` / `ask` / `allow`
rules (exact, `fnmatch`, or `*`). The default rule is `ask`. The `Skill` tool
is marked **destructive** because its sub-agent may run `Write`/`Edit`/`Bash`,
so it is never auto-approved by the non-destructive fallback.

## Programmatic use

```python
from agent_core import SkillDefinition, SkillRegistry

reg = SkillRegistry()
reg.register(SkillDefinition(name="hello", prompt="Say hi to {{ name }}."))
```
