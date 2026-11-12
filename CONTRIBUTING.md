# Contributing

Keep browser integrations behind small module interfaces and add offline HTML
fixtures for new form behavior. Never add real credentials, resumes, cookies,
or employer application data to tests.

Before submitting a change:

```powershell
pytest
python -m compileall -q main.py src scripts linkedin_autoapply
```

Changes that affect submission detection must include a fixture proving both a
confirmed receipt and an unconfirmed/no-receipt case.
