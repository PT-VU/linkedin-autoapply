# Configuration

Start from `config.example.yaml`. Keep `config.yaml` local and uncommitted.

The most important sections are:

- `search`: base keywords, location, date window, page limit (`0` means all), and optional keyword expansion;
- `resume`: the only required user file;
- `user_requirements`: optional personal job-search requirements;
- `matching`: AI scoring threshold and rubric path;
- `ai`: local Codex or API backend;
- `auto_apply`: Easy Apply and experimental External Apply switches;
- `profile.notes`: optional extra answers/context supplied only when the form asks for information not present in the resume;
- `cover_letter`: template and generated-letter locations.

For a new user, leave `ai.model` empty when using Codex. This allows the local
Codex installation to select a supported model instead of making the public
project depend on one machine-specific model name.

Keyword feedback is rolling: each run records usable JD and match-rate data.
When expansion is enabled, the active list is extended toward
`keyword_expansion.target_count` using the configured candidate pool, while
low-yield terms can be replaced by the existing feedback optimizer.
