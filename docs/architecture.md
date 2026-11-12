# Architecture

The public interface is intentionally small:

```text
linkedin_autoapply.cli / linkedin_autoapply.gui
                 |
                 v
       scripts/run_all_keywords.py
                 |
                 v
              main.py
                 |
      +----------+----------+
      |                     |
      v                     v
 src/job_search.py     src/easy_apply.py
      |                     |
      v                     v
 src/agent_match.py   src/form_filler.py
      |
      v
 scripts/codex_supervisor.py
```

The current implementation is retained behind this seam so the first public
release keeps the behavior already exercised locally. The next refactoring
step can move the modules into `linkedin_autoapply` without changing the CLI
or GUI interfaces.

## Important seams

- **AI adapter seam**: local Codex is the default; API backends are optional.
- **Browser seam**: Playwright can attach to Edge/CDP or launch a configured
  Chromium channel.
- **Application identity seam**: LinkedIn numeric Job ID is primary; JD
  fingerprint and legacy company/title are fallbacks.
- **Submission evidence seam**: a confirmed employer receipt is required for
  the submitted state.
- **Human handoff seam**: CAPTCHA, login/registration, unknown answers and
  unconfirmed results are persisted for later action.

These seams keep the complex browser implementations replaceable while the
user-facing run and configuration flow stays stable.
