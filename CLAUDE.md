# Project
Stock Audit

# Stack
- Python-Django, UI-HTMX, Alpine.js, and DaisyUI, UV (dependency management),SqliteDb(database)
- sperate enviroment for testing, production & developement
- See `docs/project.md` for full project scope and requirements
- See `docs/plan.md` for the current phase's plan

# Workflow

Plan → Build → Test, one phase at a time.
- Plan: read `docs/project.md` and write/update `docs/plan.md` for the next phase only, before finalising use skill -grill-me & confirm. 
- Build: implement only that phase
- Test: write and run tests before moving to the next phase
- End of each phase: update "Current phase" (in 1 line short) below before starting the next

# Current phase
Phase 8.1 done. Table moved into the reusable package `../django-datatable` (edit it there, not here). Next: continue the pilot (docs/pilot-checklist.md), then plan fixes from it.

# Use skills
- for Front-end & screens use skills- frontend-design,HTMX & daisyui.

# Reusable components
- For any tabular data (filters, search, sort, pages, CSV) use the `django-datatable` package (`from django_datatable import DataTable, RowsTable, Column, table_response`). Do not hand-write tables.
- It is installed from GitHub (saurabhtiki/django-datatable), pinned by `rev` in `pyproject.toml`; never from a local folder. Never edit a copy inside this app: change the package repo, push, then bump `rev` and run `uv lock --upgrade-package django-datatable && uv sync`. How to use it and its options (density_switch, column_toggle, sticky_header, ...): see the repo's `README.md`.

# Coding conventions
- Naming: snake_case for functions/variables, PascalCase for classes, descriptive names (no `df1`, `temp`, `x`)
-specific, user-facing error messages — no bare `except:`
- Log errors before raising or displaying them.
- for better UX give proper tooltips.
- After finishing a phase, run `/code-review` or `/simplify` and check output against this list

# Answers & Plan
- keep your language for Answers & Plan short & simple as layman can understand, give small examples so user can understand better.