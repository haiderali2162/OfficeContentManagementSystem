# AGENTS.md --- Land Office Management System

These instructions apply to AI coding agents working in this repository.
Follow them for every task unless the user explicitly gives a more
specific instruction.

## 1. Project purpose and current technology

This is an existing Python desktop application for land registry,
property, and office workflows in Punjab, Pakistan.

Known technology: - Python - Flet **1.0.4** (verify the installed
version before using APIs) - SQLAlchemy 2.x - SQLite - pytest - openpyxl
for Excel work - cryptography for credential protection where applicable

Known project areas include: - `app.py` - `core/config.py` -
`core/database.py` - `core/models.py` - `core/area_engine.py` -
`services/` - `ui/pages/` - `ui/navigation.py` - `ui/app_shell.py` -
`data/`

The Client Manager and Area Calculator have already been implemented and
tested by the developer. The Area Calculator includes regional
measurement conventions, calculation history, and exports. Treat
existing functionality and user data as important.

This description is context, not a substitute for inspecting the actual
repository. File names, APIs, and implementation details may have
changed.

## 2. Highest-priority rules

1.  **Inspect before editing.** Read relevant files, identify
    dependencies, and understand the existing patterns before proposing
    or making changes.
2.  **Work incrementally.** Implement only the stage or task the user
    has authorized.
3.  **Preserve working features.** Do not rewrite, replace, rename, or
    refactor unrelated code.
4.  **Protect user data.** Never delete, recreate, reset, or overwrite
    the live database or user files unless the user explicitly approves
    the exact operation.
5.  **Do not invent facts.** Never fabricate official tax rates, legal
    requirements, government fees, API behavior, test results, or
    project features.
6.  **Test what you change.** Run relevant automated tests and report
    the actual commands and outcomes.
7.  **Stop at the task boundary.** After completing the authorized task
    and reporting results, wait for the user's approval before starting
    the next stage.

If an instruction is ambiguous and the choice could affect data,
architecture, legal calculations, or existing behavior, ask a focused
question before proceeding.

## 3. Mandatory workflow for every task

### Before implementation

1.  Read this file and any more-specific `AGENTS.md` files that apply to
    the target directory.
2.  Inspect repository status and existing changes. Do not overwrite the
    user's uncommitted work.
3.  Inspect the relevant source files, tests, models, and callers.
4.  Summarize:
    -   what currently exists;
    -   what is missing or incorrect;
    -   which files may need changes;
    -   the proposed implementation;
    -   database, dependency, and compatibility risks.
5.  For multi-step or schema-changing work, present a short plan and
    wait for approval unless the user has already clearly authorized
    implementation.

### During implementation

1.  Make the smallest coherent change that solves the authorized task.
2.  Follow the existing project architecture and naming conventions.
3.  Keep business logic in service/core modules rather than embedding it
    in UI event handlers.
4.  Avoid unrelated cleanup, formatting changes, dependency upgrades, or
    broad refactoring.
5.  Add or update tests for new behavior and bug fixes.
6.  Do not silently change existing behavior or data formats.
7.  Do not claim completion while known errors remain.

### After implementation

1.  Review the diff and ensure no unrelated files or secrets were
    changed.
2.  Run targeted tests first, then relevant regression tests.
3.  Run the application or a safe startup check when practical.
4.  Give the developer exact manual test steps and expected results.
5.  Report limitations and anything not tested.
6.  Stop and wait for approval before the next stage.

## 4. Flet compatibility

The project has used Flet **1.0.4**. Verify the installed version and
inspect the installed API or existing working code before choosing
controls and methods.

-   Prefer APIs compatible with the installed version.
-   Do not copy examples written for older Flet versions without
    checking them.
-   In this project, `ft.Button` is used; do not replace it with
    obsolete controls such as `ft.ElevatedButton`.
-   Check dialog, navigation, dropdown, file-picker, event-handler, and
    page-update APIs against the installed version.
-   Keep UI code consistent with existing page-building and navigation
    patterns.
-   Do not make controls decorative or inert when they appear to perform
    an action.
-   Handle user input validation and display understandable error
    messages.
-   Test UI changes in the actual application when possible.

## 5. Python and architecture rules

-   Use the project's existing virtual environment and dependency
    versions.
-   Follow the existing module structure; do not introduce a new
    architecture without approval.
-   Use type hints for new public functions and service interfaces where
    practical.
-   Keep functions focused and avoid unnecessary abstractions.
-   Handle exceptions deliberately; do not hide errors with broad empty
    `except` blocks.
-   Do not log passwords, PINs, credentials, tokens, or other secrets.
-   Do not commit secrets, local database files, environment files, or
    personal data.
-   Add dependencies only when necessary, and explain why they are
    needed.
-   Do not upgrade packages as part of an unrelated feature.

## 6. Database and migration safety

The application uses SQLAlchemy 2.x and SQLite. Inspect
`core/database.py`, model imports, `Base`, `SessionLocal`, and database
initialization before changing persistence.

-   Preserve existing tables, columns, and records.
-   Prefer additive schema changes.
-   Understand that `Base.metadata.create_all()` creates missing tables
    but does not safely migrate existing table structures.
-   If existing columns or constraints must change, design an explicit,
    tested migration. Do not assume `create_all()` performs it.
-   Never drop or recreate the live database to make tests pass.
-   Never run destructive SQL against the live database.
-   Use temporary SQLite databases for model and migration tests.
-   Back up the database before any approved operation that could alter
    existing data.
-   Preserve foreign-key enforcement and use deliberate `ondelete`
    behavior.
-   Consider indexes and uniqueness constraints where they support
    actual lookup and integrity requirements.
-   Test relationships, persistence, deletion behavior, and
    compatibility with existing records.
-   Do not repurpose an existing model merely because some of its fields
    appear similar to a new feature.

## 7. Numeric, land-area, and financial calculations

-   Use `Decimal` for monetary and precision-sensitive calculations.
    Avoid binary `float` for currency, rates, shares, or exact area
    arithmetic.
-   For SQLAlchemy monetary/rate columns, consider
    `Numeric(precision, scale)` and choose precision deliberately.
-   Convert user-entered values safely; do not construct `Decimal` from
    an already-rounded float when avoidable.
-   Define rounding rules explicitly and apply rounding at documented
    points.
-   Validate zero denominators, negative values, missing values, and
    out-of-range inputs.
-   Explain calculation steps and units in results.
-   Reuse `core/area_engine.py` for land-area conversions rather than
    duplicating its arithmetic.
-   Do not silently assume a regional measurement convention. Show which
    convention was selected and preserve it with saved calculations.
-   Distinguish local conventions (for example, an 8-kanal acre
    convention) from other standards. Do not present a local convention
    as universal.

## 8. Pakistan/Punjab legal, tax, and government-fee accuracy

This application may be used for property valuation, registry estimates,
taxes, stamp duty, registration fees, mutation charges, and other
government or office charges.

-   Never invent a rate, exemption, threshold, legal interpretation, or
    government fee.
-   Do not describe an unverified value as an official current rate.
-   Verify time-sensitive rules against authoritative, current sources
    when the task requires legal or tax accuracy.
-   Record jurisdiction, source/reference, effective-from date,
    effective-to date where known, and verification status.
-   Default newly created tax/fee rules to **unverified** unless the
    application has a verified source and the user has approved the
    rule.
-   Clearly distinguish official/verified calculations from estimates
    and manually entered assumptions.
-   Account for transaction type, payer, eligibility, exemptions, and
    effective date only when supported by verified rules.
-   Preserve a snapshot of the rules and rates used in a saved estimate
    so later edits do not rewrite historical results.
-   Passing software tests does not prove legal correctness.
-   If current authoritative information cannot be established, leave
    the rule configurable and label it unverified.

## 9. Imports, exports, and files

-   Validate imported files before writing records.
-   Report invalid rows with useful row numbers and reasons.
-   Do not silently skip, overwrite, or merge duplicate records.
-   Provide a clear conflict policy and preserve original data.
-   Keep exports readable and include relevant units, dates, and
    calculation context.
-   Test that generated CSV, XLSX, ODS, and PDF files open and contain
    expected data when those formats are supported.
-   Handle Urdu text and fonts honestly; do not claim correct Urdu PDF
    rendering unless it has been tested with suitable fonts and text
    shaping.
-   Do not expose local paths, personal records, or secrets
    unnecessarily.

## 10. Security and privacy

-   Treat client identity and credential data as sensitive.
-   Never display or log stored passwords, PINs, or credential
    ciphertext unnecessarily.
-   Use the existing encryption design where credential storage is
    involved; inspect it before making changes.
-   Do not weaken authentication, encryption, access controls, or
    database protections to simplify development.
-   Do not send client data to external services unless the user
    explicitly authorizes it.
-   Use synthetic data in tests and examples.

## 11. Testing standards

Use pytest and the project's existing testing conventions.

Where relevant, test: - normal and boundary cases; - invalid and missing
input; - persistence and retrieval; - duplicate/conflicting records; -
date/effective-date selection; - rounding and Decimal precision; -
relationships and deletion behavior; - imports and exports; - error
handling; - regression behavior in existing modules.

Use isolated temporary databases and temporary files. Never use real
client data in tests.

Report tests truthfully: - Give the exact command executed. - State how
many tests passed, failed, or were skipped when available. - Include
relevant failure output or a concise diagnosis. - If a test was not run,
say so. - Never say "all tests passed" unless the test command actually
completed successfully.

## 12. Git and change management

-   Inspect `git status` before editing when Git is available.
-   Preserve the user's uncommitted changes.
-   Do not reset, clean, checkout over, or discard user changes.
-   Prefer a feature branch or a backup before significant database
    work.
-   Do not commit, push, publish, or deploy unless explicitly asked.
-   Keep changes focused and review the final diff.

## 13. Required completion report

After every authorized stage or task, use this report:

### Stage/task

Name and scope of the work.

### Existing implementation inspected

Relevant files and behavior found before editing.

### Implemented

Features actually implemented.

### Files changed

Each file path and a short explanation.

### Database/dependency impact

Tables, columns, migrations, dependencies, configuration, or data
impact.

### Tests executed

Exact commands and actual results.

### Manual verification

Numbered steps for the developer, with expected outcomes.

### Known limitations / outstanding work

Anything incomplete, unverified, or blocked.

### Status

Choose one: - Complete and automated/manual checks verified -
Implemented; awaiting developer manual verification - Partially
implemented - Blocked

### Next proposed stage

Describe the next logical task, but do not start it.

## 14. Current roadmap: Property Valuation & Taxes

This roadmap is guidance. Implement only the stage the user explicitly
authorizes.

### Stage 8.1 --- Database foundation

Inspect existing models and database initialization. Add appropriate
structured models for: - property locations; - DC valuation rates; - tax
and fee rules; - saved valuation estimates; - individual estimate charge
breakdowns.

Potential fields include location hierarchy, Mauza, optional
hadbast/locality, property classification, rate and explicit unit,
effective dates, source, verification status, rule calculation method,
monetary values, payer, and historical snapshots.

Requirements: - Optional client link with an intentional deletion
policy. - Preserve client/property details as estimate snapshots. - Use
`Numeric` for monetary and rate columns. - Default tax/fee rules to
unverified. - Preserve applied rate/rule snapshots in historical
estimates. - Do not seed invented official rates. - Test with temporary
SQLite databases. - Do not implement the UI, importer, valuation engine,
or tax formulas in this stage.

### Stage 8.2 --- DC rate management

Add/edit/search/filter DC rates and import from Excel with validation,
duplicate handling, and import reports.

### Stage 8.3 --- Property valuation engine

Build tested service-layer calculations for property value, transferred
shares, declared consideration comparisons, supported units, and
missing/ambiguous rate handling. Reuse the existing area engine.

### Stage 8.4 --- Taxes and registration expenses

Implement configurable, effective-dated rules for applicable taxes and
fees only after their applicability and rates are verified. Show
itemized charges, payer, assumptions, and warnings.

### Stage 8.5 --- User interface integration

Integrate property details, rate selection, valuation, charge
calculations, saved estimates, and history into the existing Flet
navigation and page structure.

### Stage 8.6 --- Reports and final verification

Add appropriate reports and exports, then run full regression and manual
tests.

## 15. Immediate task discipline

At the start of a new task, identify the exact authorized stage and its
completion criteria. If the user asks only for a plan, do not edit
files. If the user approves implementation, implement only that stage.
When finished, provide the completion report and stop.

The developer will review plans, reports, and test output with a
separate assistant conversation. Make reports clear enough that another
reviewer can understand exactly what changed without guessing.
