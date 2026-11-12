# Applicant context map

This directory is the progressive-disclosure context for form filling and
cover-letter generation.

- `resume_facts.md`: verified facts extracted from the user's resume;
- `user_qa_preferences.json`: deterministic answers the user has supplied;
- `job_requirements.md`: personal search requirements and exclusions.

The AI should read only the file needed for the current question. It must ask
the user when a truthful answer is missing and must never invent credentials,
work authorization, or experience.
