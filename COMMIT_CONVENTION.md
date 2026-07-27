# Commit convention

Use `TYPE(SCOPE): subject` (same family as IMS / PatsPrints / PatsScraper).

| Type | Use for |
|------|---------|
| FEAT | New behavior |
| FIX | Bug fixes |
| MOD | Enhancements |
| CHORE | Tooling, CI, repo hygiene |
| DOCS | Documentation only |
| CI | GitHub Actions / hooks |

**Branch model:** local `feature` only (never push `feature`) → merge to `dev` → push `dev` → PR `dev` → `main`.

Run the `release-pr` skill after atomic commits on `feature`.
