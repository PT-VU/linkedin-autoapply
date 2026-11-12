# Workflow

The packaged project follows the current production sequence:

```text
start
  -> connect to Edge/CDP and verify LinkedIn login
  -> iterate configured keywords
  -> collect one result page
  -> resolve stable LinkedIn Job IDs and skip confirmed duplicates
  -> score eligible non-Easy-Apply roles with the AI supervisor
  -> process Easy Apply forms
  -> optionally open External Apply forms
  -> verify employer receipt or route the case to the manual queue
  -> persist the application ledger and continue
```

The page-by-page order is deliberate: a single failed form cannot hide later
keywords, and the user can inspect the queue while the rest of the run moves
forward.

## Current implementation reused

The implementation copied into this distribution is based on the latest local
workflow, not the old Claude-only prototype. The old prototype is retained as
documentation inspiration only. In particular, the current version's stable
Job ID identity, scoring cache, Excel queue, resume-upload safety checks,
Codex supervisor, and external receipt diagnostics take precedence.

## External Apply

When enabled, the agent opens eligible external pages and uses Edge/CDP. It
records page metadata, required fields, file-upload evidence, submit attempts,
network evidence, challenge signals, and employer receipts. Any of the
following routes the job to the manual queue:

- CAPTCHA/reCAPTCHA or bot-risk challenge;
- login or registration requirement;
- unsupported widget or client-side validation;
- unknown required answer;
- unknown or missing employer receipt after the bounded retry budget.
