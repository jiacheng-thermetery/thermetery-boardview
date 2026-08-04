# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Agent delegation policy (follow at all times)

- Delegate applicable tasks to Opus or Sonnet as you see fit.
- Crucially: do **not** launch adversarial review in agent swarms using
  Fable; instead use Sonnet predominantly and Opus (medium effort) if the
  task is important.

## Repository ground rules

- Private keys (`private/fz_key.txt`, `private/XZZ_Key.txt`, and anything
  else under `private/`) are furnished locally and must always remain
  gitignored. Never commit key material, decrypted caches (`*.fz.cache`),
  or derived topology pickles (`*.tvw.topocache.pkl`).
- Boardview files under `boardviews/` are proprietary test data — never
  commit them; use them freely for local cross-format verification.
- Do not push to `main`. Land changes via pull requests.
