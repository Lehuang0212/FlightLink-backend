# Project Development Skills Lockfile

> These skills are **development-only references** installed at project scope under `.agents/skills/`.
> They must **not** be bundled into ECS runtime packages and are **not** installed globally.

| Skill | Upstream Repository | Pinned Commit | Upstream Subdirectory | License | Install Location | Extracted Files | Local Adjustments |
|---|---|---|---|---|---|---|---|
| `fastapi-templates` | `https://github.com/wshobson/agents` | `c96b5f6552dba32e4492307493ee613581430715` | `plugins/api-scaffolding/skills/fastapi-templates` | MIT (Copyright (c) 2024 Seth Hobson) | `.agents/skills/fastapi-templates/` | `SKILL.md`, `references/details.md`, `LICENSE`, `SOURCE.md` | None (upstream `SKILL.md` and `references/` verbatim) |
| `openapi-spec-generation` | `https://github.com/wshobson/agents` | `c96b5f6552dba32e4492307493ee613581430715` | `plugins/documentation-generation/skills/openapi-spec-generation` | MIT (Copyright (c) 2024 Seth Hobson) | `.agents/skills/openapi-spec-generation/` | `SKILL.md`, `references/details.md`, `references/code-first-and-tooling.md`, `LICENSE`, `SOURCE.md` | None (upstream `SKILL.md` and `references/` verbatim) |
| `property-based-testing` | `https://github.com/trailofbits/skills` | `149d7b21f4067765b82e67029730b42d228ef4b4` | `plugins/property-based-testing/skills/property-based-testing` | CC BY-SA 4.0 (Trail of Bits) | `.agents/skills/property-based-testing/` | `SKILL.md`, `references/generating.md`, `references/interpreting-failures.md`, `references/libraries.md`, `references/refactoring.md`, `references/reviewing.md`, `LICENSE`, `SOURCE.md` | Removed upstream `effort: low` frontmatter field from `SKILL.md` so it cannot override this project's `high` reasoning setting; omitted upstream `agents/openai.yaml`, `assets/`, `README.md`, and plugin evals/hooks. Documented per CC BY-SA 4.0 §3(a)(1)(B). |

## Verification

- Only `SKILL.md`, `references/`, `LICENSE`, and `SOURCE.md` are present under each skill directory.
- No plugin marketplaces, extra agents (`agents/`), or hooks were installed.
- All three skills satisfy Antigravity / workspace skill discovery layout (`.agents/skills/<name>/SKILL.md` with valid `name` and `description` YAML frontmatter).
- None of the installed `SKILL.md` files contain model-specific `effort` or `model` overrides.
