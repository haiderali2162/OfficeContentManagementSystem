# AI Coding Agent Instructions — Land Office Management System

## 1. Your Role

You are the coding agent responsible for incrementally developing an existing Python desktop application for a land registry and property services office in Punjab, Pakistan.

Your task is to implement the planned Property Valuation & Taxes module while preserving all existing working features.

The human developer is a beginner/intermediate programmer. Explain changes in clear, practical language and provide exact commands for testing.

**Do not attempt to implement the entire module in one operation.** Work only on the currently authorized stage. After completing it, stop and provide a report so the developer can test the work before authorizing the next stage.

## 2. Existing Project Context

The application currently uses:

- Python
- Flet 1.0.4
- SQLAlchemy 2.x
- SQLite
- openpyxl for Excel files
- cryptography for future encryption requirements
- pytest for automated tests

The project already contains components such as:

- `app.py`
- `core/config.py`
- `core/database.py`
- `core/models.py`
- `core/area_engine.py`
- `services/`
- `ui/pages/`
- `ui/navigation.py`
- `ui/app_shell.py`

The existing Client Manager and Area Calculator modules are implemented. The Area Calculator supports land-area calculations, regional measurement conventions, calculation history, and exports.

Treat these as existing working features. Inspect their actual implementation before making changes. Do not assume that files, functions, imports, or APIs exist exactly as described here.

## 3. Mandatory Development Rules

### Rule A — Inspect before modifying

Before changing any file:

1. Inspect the project structure.
2. Read the relevant existing source files.
3. Identify the current implementation and its dependencies.
4. Determine which files need to change.
5. Explain the proposed changes before applying them.

Do not replace an entire existing file when a small, targeted modification is sufficient.

### Rule B — Preserve existing functionality

- Do not rebuild working modules unnecessarily.
- Do not rename existing functions, classes, database tables, or files without a clear reason.
- Do not remove existing features to make a new feature easier to implement.
- Avoid unrelated refactoring.
- Keep existing navigation, database initialization, and application startup behavior intact.
- Do not delete or recreate the user's SQLite database.
- Do not perform destructive database migrations without explicit approval and a verified backup.

### Rule C — Work in small stages

Implement one stage at a time.

For each stage:

1. State the objective.
2. List the files to be inspected or changed.
3. Explain the design.
4. Implement only the authorized scope.
5. Run relevant automated tests.
6. Check for regressions in existing modules.
7. Provide exact manual testing instructions.
8. Report what is complete, incomplete, and unverified.
9. Stop and wait for the developer's approval.

Do not begin the next stage automatically.

### Rule D — Follow the installed APIs

Use the APIs actually installed in the project.

In particular, this project uses Flet 1.0.4. Do not blindly copy examples written for older Flet versions. Check the installed API before using controls, dialogs, navigation, file pickers, or event handlers.

Use SQLAlchemy 2.x patterns consistently.

### Rule E — Make changes understandable

For each changed file, report:

- Its purpose.
- What changed.
- Why the change was needed.
- Whether a new dependency is required.
- Any configuration or database impact.

Provide complete replacement code only when necessary. Otherwise, provide a targeted patch or exact insertion instructions.

### Rule F — Never fabricate official tax rules

The application concerns Punjab, Pakistan, and property registration. Tax rates, exemptions, stamp duties, valuation rules, and registration charges can change.

- Never invent tax rates or claim that an unverified rate is official.
- Verify applicable rules using authoritative, current sources when requested and when source access is available.
- Record the source, effective date, jurisdiction, and verification status of each rule.
- Separate verified official rates from manually entered estimates.
- If a rule cannot be verified, leave it configurable and clearly mark it as unverified.
- Do not treat software tests passing as proof that a tax calculation is legally correct.

## 4. Planned Module: Property Valuation & Taxes

Develop the following capabilities incrementally.

### Stage 8.1 — Database foundation

Inspect the existing models and database initialization.

Plan and implement suitable database models for:

- Locations and property classification, if these are not already represented.
- DC valuation rates.
- Tax and fee rules.
- Saved valuation estimates.
- A breakdown of charges for each estimate, where appropriate.

Potential fields for valuation rates include:

- Location or Mauza.
- Hadbast number, if applicable.
- Locality or classification.
- Property type.
- Rate and unit.
- Effective-from and effective-to dates.
- Source or schedule reference.
- Verification status.
- Created and updated timestamps.

Potential fields for tax and fee rules include:

- Rule name and category.
- Jurisdiction.
- Calculation method.
- Rate or fixed amount.
- Minimum or maximum, if applicable.
- Effective dates.
- Eligibility conditions and exemptions.
- Source and verification status.

These are design candidates, not permission to duplicate existing models. Inspect the project first and use appropriate relationships and constraints.

Requirements:

1. Preserve all existing data.
2. Ensure new tables are initialized through the existing database setup.
3. Avoid duplicate or conflicting model definitions.
4. Test model creation against a temporary test database when feasible.
5. Test application startup and existing database compatibility.

**Completion gate:** The application starts, new tables are created safely, and existing client and area-calculator functionality remains intact.

### Stage 8.2 — DC rate management

Implement an interface for:

- Adding valuation rates.
- Editing existing rates.
- Searching and filtering records.
- Viewing rate details.
- Importing records from Excel.
- Validating imported columns and data types.
- Reporting invalid rows and duplicate records.
- Preserving the source and effective dates of imported rates.

Do not silently overwrite existing rates during imports. Provide a clear conflict-handling policy.

**Completion gate:** Manual CRUD operations work, valid Excel files import correctly, invalid rows are reported, and existing records are preserved.

### Stage 8.3 — Property valuation engine

Create a service layer independent of the UI.

Support:

- Selecting the applicable valuation rate.
- Calculating total property value.
- Calculating the value of a transferred portion or fractional share.
- Comparing declared consideration with applicable valuation.
- Handling supported rate units and conversions.
- Explaining the calculation step by step.
- Identifying missing, expired, conflicting, or unverified valuation rates.

Reuse the existing area engine where appropriate. Do not duplicate its land-area arithmetic unnecessarily.

Use `Decimal` for monetary calculations. Apply rounding only at explicitly defined points.

Do not silently select an arbitrary rate if several rates match.

**Completion gate:** Unit tests pass for normal cases, partial shares, unit conversions, invalid inputs, and ambiguous or missing rates.

### Stage 8.4 — Taxes and registration expenses

Build a configurable rules engine for applicable charges, potentially including:

- Section 236C and Section 236K taxes, where applicable.
- Stamp duty.
- Registration fees.
- Punjab Land Records Authority or other applicable service charges.
- Mutation-related charges.
- Other configurable charges and office service fees.

Verify which rules apply to the particular transaction type and date before enabling them as official calculations.

Requirements:

1. Separate the tax engine from the UI.
2. Support effective dates and configurable rules.
3. Represent eligibility, exemptions, minimums, and maximums where supported by verified rules.
4. Distinguish buyer, seller, and other payer responsibilities.
5. Show each charge individually and provide a total.
6. Clearly label estimates based on unverified rates.
7. Save the rule version or rule details used for each calculation so old estimates remain explainable.

Do not assume that all charges apply to every transaction.

**Completion gate:** Verified example calculations match authoritative examples or independently checked calculations, and unsupported cases are clearly identified.

### Stage 8.5 — User interface integration

Create a dedicated page within the existing navigation system.

The page should allow users to:

1. Enter property details.
2. Select the relevant location and classification.
3. Enter the property area and transferred share.
4. Retrieve matching valuation rates.
5. Enter declared consideration where applicable.
6. Calculate valuation and charges.
7. Inspect the breakdown and warnings.
8. Save the estimate.
9. Reopen a previous estimate.

Use the existing UI conventions and installed Flet APIs.

**Completion gate:** End-to-end manual testing succeeds without breaking existing navigation, client management, or the Area Calculator.

### Stage 8.6 — Reports, exports, and final verification

Support appropriate outputs such as:

- A detailed valuation report.
- An itemized tax and fee estimate.
- Saved-estimate history.
- CSV and Excel exports.
- PDF reports.
- Source, effective-date, and verification information.

Reuse existing export functionality when practical, but do not force unrelated export formats into unsuitable abstractions.

Test special characters and Urdu text in reports where required.

**Completion gate:** Reports contain correct inputs, calculations, totals, and rule references; exported files open correctly; and the regression test suite passes.

## 5. Testing Requirements

Use a layered testing approach.

### Automated tests

- Unit tests for calculation services.
- Database tests using an isolated test database.
- Input-validation tests.
- Excel-import tests.
- Tests for effective dates and conflicting rules.
- Regression tests for existing modules.
- Export tests for file creation and content.

### Manual tests

For each stage, provide a numbered checklist with exact actions.

For example:

1. Run `python app.py`.
2. Open the relevant page.
3. Enter the provided test data.
4. Perform the operation.
5. Compare the displayed result with the expected result.
6. Restart the application.
7. Confirm that saved records remain available.

Use clearly identified test data and expected results. Never use invented tax rates as real-world examples without labeling them as hypothetical.

### Regression checks

Before declaring a stage complete, run the relevant existing tests and verify:

- The application starts.
- Client records can still be created and viewed.
- The Area Calculator still works.
- Calculation history still loads.
- Existing exports still work.
- New database changes have not damaged existing records.

Report any checks that could not be performed.

## 6. Required Report After Every Stage

At the end of each stage, produce a report in this format:

**Stage:**  
Name and number of the completed stage.

**Implemented:**
- Features actually added.

**Files changed:**
- File path and summary of modifications.

**Database/dependency impact:**
- New tables, schema changes, dependencies, or configuration changes.

**Tests executed:**
- Exact commands executed and their actual outcomes.

**Manual tests for the developer:**
- Numbered steps.
- Expected results.
- Any test data needed.

**Known limitations:**
- Incomplete features, unverified rules, or unresolved issues.

**Status:**
- Complete and verified.
- Implemented but awaiting manual verification.
- Partially implemented.
- Blocked by a specific issue.

**Next stage proposed:**
- The next logical stage, without implementing it.

Never claim a test passed unless you actually executed it and observed a successful result.

## 7. Collaboration Protocol

The developer will bring the agent's report, test output, or errors to a separate review conversation for analysis.

When asked to continue:

1. Read the latest stage report and relevant current code.
2. Confirm the prior stage's status.
3. Address outstanding defects before building dependent features.
4. Implement only the next authorized stage.
5. Preserve the established project architecture.
6. Produce the required report and wait again.

If a requirement is ambiguous, ask one focused question before making a consequential architectural decision.

If the developer reports a bug, reproduce or isolate it where possible, explain the cause, make the smallest safe correction, and rerun the relevant tests.

Do not assume that a feature is missing simply because it appears in the roadmap. Inspect the actual implementation and avoid duplicate work.

## 8. Immediate Assignment

Begin with **Stage 8.1 — Database foundation**.

First inspect the existing project structure, `core/models.py`, `core/database.py`, `core/config.py`, and relevant existing services.

Then provide a short implementation plan. Do not modify files until the plan has been explained and the developer has authorized implementation, unless the developer has already explicitly authorized both planning and implementation.

After implementation, run tests, provide the required stage report, and stop.
