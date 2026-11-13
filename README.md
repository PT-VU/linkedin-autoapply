# LinkedIn AutoApply

AI-assisted LinkedIn job discovery and application workflow for local use.

The project keeps deterministic browser operations in Python and delegates
semantic work to an AI backend. It supports local Codex by default and can
also be connected to an API backend. The workflow is configurable for any
resume, industry, keyword set, and location.

## What it does

1. Reuses a logged-in Edge/Chromium session, or guides a first-time user
   through an interactive LinkedIn login before the run starts.
2. Searches each configured keyword and location.
3. Collects LinkedIn job cards and follows LinkedIn Job ID based deduplication.
4. Uses AI scoring and a user-supplied requirements/rubric file.
5. Processes Easy Apply forms with deterministic field handling plus AI help.
6. Optionally opens and attempts External Apply forms.
7. Sends CAPTCHA, login, registration, unknown validation, and unconfirmed
   submissions to the manual queue instead of claiming success.
8. Records confirmed applications and queue entries in local data files.

External Apply is intentionally experimental. ATS implementations differ, and
CAPTCHA/reCAPTCHA, bot-risk checks, login/registration walls, client-side
validation, and missing employer receipts can reduce its success rate. The
project does not bypass CAPTCHA or invent answers.

## Quick start from source

Requirements: Python 3.10+, Microsoft Edge or Chromium, and either a local
Codex CLI (the default backend) or an OpenAI-compatible API key.

```powershell
$ProjectRoot = "C:\path\to\linkedin-autoapply"
Set-Location $ProjectRoot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
playwright install chromium
Copy-Item config.example.yaml config.yaml
```

Edit `config.yaml`, then run the environment check:

```powershell
python -m linkedin_autoapply doctor
```

Start the desktop control panel:

```powershell
python -m linkedin_autoapply configure
```

Or run from the command line:

```powershell
python -m linkedin_autoapply run --config config.yaml --max-pages 0 --date-posted "Past 24 hours"
```

The command starts the current multi-keyword workflow and its local Codex
supervisor. `--dry-run` is available for non-submitting checks.

## Desktop EXE

The repository ships the source launcher and reproducible build script rather
than a prebuilt binary. This keeps user credentials, resumes, and browser
runtime state out of the release artifact.

The small Tk control panel exposes the settings most users need:

- keywords and location;
- posting-age filter and page limit;
- target keyword count and an AI keyword expansion/optimization switch;
- score threshold;
- a single optional free-text area for answers discovered during a run;
- Easy Apply on/off;
- External Apply experimental mode on/off;
- dry-run mode;
- resume, cover-letter template, and job-requirements file selection;
- start, stop, environment check, and live log output.

On EXE startup, the launcher creates the local runtime directories and checks
for a usable browser. Installed Microsoft Edge is preferred; if Edge is not
available, Playwright Chromium is downloaded to the user's Playwright cache
and the workflow falls back to it. Python dependencies are already inside the
portable package. The Chromium download always asks for user confirmation
first; declining it leaves the Start button disabled. Codex installation/login
and LinkedIn authentication remain user-provided because they are account
credentials, not package dependencies.

The left-side gear tab, **Model Configuration**, reads visible model slugs from
the local Codex model catalog/config and lets the user choose the Codex model.
The same tab configures an OpenAI-compatible API backend, including base URL,
model name, API-key environment-variable name, and optional JSON response mode.
Only the environment-variable name is saved. The API key itself stays in the
user's environment and is never written to `config.yaml`.

Build the portable directory from a Windows PowerShell prompt:

```powershell
$ProjectRoot = "C:\path\to\linkedin-autoapply"
Set-Location $ProjectRoot
pip install -e .[dev]
.\build_exe.ps1
```

The build copies only `config.example.yaml` by default, so local resumes,
locations, and profile settings are not embedded in the portable directory.
To intentionally carry an existing local configuration into a private build,
set `LINKEDIN_AUTOAPPLY_INCLUDE_LOCAL_CONFIG=1` before running the script.

Double-click `dist\LinkedInAutoApply\LinkedInAutoApply.exe`. The worker
executable is placed beside it by the build script.

## Beginner first-run checklist

1. Install Python, Edge/Chromium, and Codex CLI, or prepare an
   OpenAI-compatible API endpoint.
2. Copy `config.example.yaml` to `config.yaml`, or open the desktop control
   panel and select your resume. The resume is required when starting a run;
   other console fields may be left blank while saving.
3. In **Console**, enter search keywords and a location. Choose a posting-age
   window (24 hours, 1 week, 2 weeks, or 1 month), review the score threshold,
   and keep keyword optimization enabled if you want the active list to adapt.
4. In **Model Configuration**, select Local Codex or API. In API mode, set the
   base URL, model name, and the environment-variable name that contains your
   key; never paste the key into the repository or YAML file.
5. Click **Save model configuration** on the model page, then return to
   **Console** and save the run settings. Saving the two pages is independent.
6. Run `python -m linkedin_autoapply doctor` (or **Check environment** in the
   GUI). Fix missing dependencies before starting.
7. Click **Start**. On the first run, a visible LinkedIn window opens if no
   authenticated session is available. Complete login, SSO, CAPTCHA, or 2FA
   yourself; the program never reads or stores those credentials.
8. Watch the live log. Confirmed applications are recorded locally. CAPTCHA,
   login/registration, unknown answers, and unconfirmed submissions are kept
   for manual handling rather than being reported as successful.

For a non-submitting smoke test, enable **Preview only; do not submit** or add
`--dry-run` to the CLI command.

## Platform support

The packaged EXE and the reproducible build script are Windows-only. The
current end-to-end workflow also assumes Windows Edge/CDP behavior, PowerShell
process supervision, and Windows window handling for human handoff. The
Python modules are not deliberately marketed as cross-platform yet; a
macOS/Linux port would need platform-specific browser, process, and packaging
work before it should be considered supported.

### First run and LinkedIn login

The first run may not have a LinkedIn cookie yet. The program then opens a
visible Edge window at LinkedIn's normal login page and pauses the workflow
for up to `linkedin.auth.login_timeout_seconds` (300 seconds by default).
Complete the normal login, SSO, CAPTCHA, or 2FA in that window; once an
authenticated LinkedIn navigation marker is detected, the job run continues
automatically. Later runs reuse the local Edge profile/session. The program
never asks for, reads, or stores the user's password or 2FA code.

Set `linkedin.auth.interactive_login: false` for unattended environments that
must fail fast instead of waiting for a human login. To use a separate browser
profile, set `linkedin.profile_path` to an explicit local directory; do not use
Incognito mode.

## Files users configure

| File | Purpose |
|---|---|
| `config.yaml` | Keywords, location, filters, AI backend, apply switches |
| `user_files/resume.*` | Resume selected in the control panel |
| `user_files/job_requirements.md` | Personal job-search requirements and rubric context |
| `user_files/cover_letter_template.*` | Optional template or style reference |
| `data/scoring_rubric.md` | Optional detailed scoring rubric |

The GUI copies selected documents into `user_files/`. They are ignored by Git.
Do not place passwords, cookies, API keys, or browser profiles in the public
repository.

## AI backends

### Local Codex (default)

```yaml
ai:
  backend: codex
  command: codex
  model: ""
```

An empty model inherits the model available in the user's local Codex setup.
The public distribution does not pin a model; the user's local Codex setup
decides which model is available.

### API backend

```yaml
ai:
  backend: api
  provider: openai_compatible
  base_url: "https://api.example.com/v1"
  model: "your-model"
  api_key_env: "OPENAI_API_KEY"
```

Store the key in the environment, not in YAML:

```powershell
$env:OPENAI_API_KEY = "..."
```

## Output

Runtime data is local and ignored by Git:

```text
data/applied_jobs.json
data/application_history_baseline.json
data/job_decision_cache.json
data/manual_apply_queue.json
data/manual_apply_queue.xlsx
logs/
screenshots/
```

Only an employer receipt or equivalent verified submission evidence is marked
as submitted. A page that merely reached Review, a busy Send button, or a
network request without a receipt remains unconfirmed.

## Human intervention

The workflow distinguishes:

- **manual queue**: registration/login requirements, unsupported external
  forms, CAPTCHA handoff, or an unconfirmed application;
- **user-answer prompts**: a form asks for a truthful personal answer that is
  not present in the configured profile;
- **skip**: the role is outside the configured requirements or the page is
  unavailable.

The agent may keep an external page for later human completion. It must not
claim success after a CAPTCHA merely because the form looked complete.

## Privacy, terms, and limitations

- Use only with accounts and data you control.
- Review LinkedIn and employer-site terms before enabling automation.
- CAPTCHA/anti-bot mechanisms are not bypassed.
- A browser automation workflow cannot guarantee delivery to every ATS.
- Treat `data/`, `logs/`, screenshots, resumes, and generated letters as
  private personal data.

## Development

```powershell
pip install -e .[dev]
pytest
```

The current browser implementation is kept under `src/` for compatibility
with the validated workflow. The `linkedin_autoapply` package is the small
public interface and launcher seam; future refactors can replace the
implementation behind it without changing the user commands.

## License

MIT. See `LICENSE`.
