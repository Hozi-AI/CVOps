# Contributing to CVOps

Thank you for considering a contribution. This document explains how to get set up, how to structure your work, and what the bar is for a PR to be merged.

## Table of contents

- [Development setup](#development-setup)
- [Branch and commit convention](#branch-and-commit-convention)
- [Pull request checklist](#pull-request-checklist)
- [Code style](#code-style)
- [Running tests](#running-tests)
- [Reporting issues](#reporting-issues)

---

## Development setup

```bash
git clone https://github.com/Hozi-AI/CVOps.git
cd CVOps
cp .env.example .env          # fill in local secrets
sh scripts/git-setup.sh       # install git hooks (enforces commit format)
```

### API (Python 3.12)

```bash
cd services/api
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Requires Docker for the test suite (testcontainers spins up postgres automatically).

### Frontend (Node 20)

```bash
cd services/frontend
npm install
npm run dev
```

---

## Branch and commit convention

### Branch format

```
<type>/<5-8-word-kebab-title>
```

| Type | Use case |
|---|---|
| `feat` | New feature |
| `fix` | Bug fix |
| `chore` | Tooling, deps, CI, build config |
| `docs` | Documentation only |
| `refactor` | Restructuring without behaviour change |
| `test` | New or updated tests |
| `style` | Formatting, whitespace (no logic change) |

Examples: `feat/add-jwt-refresh-rotation`, `fix/resolve-cursor-pagination-off-by-one`

### Commit format

```
<type>: <5–8 word imperative title>

3–8 sentences explaining WHY this change was needed.
Focus on the problem, not the implementation.

Co-authored-by: Your Name <you@example.com>  # if applicable
```

### Atomic commits

One commit = one responsibility. Do not mix:
- feature work + bug fix
- refactor + logic change
- formatting + behaviour change
- changes across unrelated domains

---

## Pull request checklist

Before opening a PR:

- [ ] All existing tests pass: `pytest tests/ -q`
- [ ] Ruff reports no issues: `ruff check src/ tests/`
- [ ] Ruff format is clean: `ruff format --check src/ tests/`
- [ ] Mypy reports no errors: `mypy src/`
- [ ] New behaviour is covered by tests
- [ ] The PR title matches the commit format (`<type>: <5–8 word title>`)
- [ ] The PR template is filled in (`## What`, `## Why`, `## How`, `## Test plan`)
- [ ] The issue being closed is linked (`Closes #N`)

---

## Code style

**Python:**
- Formatter/linter: [Ruff](https://docs.astral.sh/ruff/) — configured in `pyproject.toml`
- Line length: 100
- Target: Python 3.12
- Type annotations: required everywhere (mypy strict mode)

**Comments:** Only write a comment when the *why* is non-obvious — a hidden constraint, a subtle invariant, or a workaround for a specific bug. Never describe *what* the code does.

**TypeScript:**
- ESLint + Prettier (configured in `services/frontend`)

---

## Running tests

```bash
cd services/api

# Full test suite (requires Docker)
pytest tests/ -q

# Single file
pytest tests/routers/test_auth.py -q

# With output
pytest tests/ -s -v
```

The test suite uses testcontainers to spin up a real PostgreSQL instance. No mocking of the database layer — this is intentional to catch migration/ORM divergence.

---

## Reporting issues

Use the GitHub issue templates:

- **Bug report** — reproducible problem with steps to reproduce and expected vs. actual behaviour
- **Feature request** — new capability with a clear problem statement

For security vulnerabilities, see [SECURITY.md](SECURITY.md) — do **not** open a public issue.
