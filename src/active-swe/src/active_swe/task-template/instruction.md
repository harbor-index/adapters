### Role
You are an Expert Code Reviewer. Your mission is to find and fix CRITICAL bugs in the repo.

### Objective
Given the list of **files pending review**, identify the high-impact bugs and provide the corresponding fixes. Ignore all non-functional concerns, including style, formatting, and naming, to focus solely on functional correctness and system integrity.

**IMPORTANT**: There may be multiple bugs within the reviewed files. You need to identify and fix ALL bugs of the files pending review in the repo. Each bug fix should address a specific issue independently.

### Review Dimensions
The following are the primary bug categories to be reviewed:
**Logic & Computation:**
- **Wrong Condition / Inverted Logic**: Flawed logic in an existing conditional, such as inverted checks or incorrect operators.
- **Incomplete Conditional**: Missing a necessary branch or case (like a specific enum variant) within an existing `if/switch` structure.
- **Expression / Computation Error**: Mathematical, precision, operator precedence, or off-by-one errors in computations.

**Reference & Data Flow:**
- **Wrong Variable / Property / Key**: Referencing the incorrect variable, property, or dictionary key.
- **Wrong Method / API Call**: Invoking an incorrect API variant or a similarly named but wrong method.
- **Missing Propagation / Context Loss**: Failing to pass required parameters or context down through nested or chained calls.

**Domain & Business Flow:**
- **Business Rule Guard Missing**: Complete absence of a required domain, policy, or permission check.
- **Registry / Whitelist Incompleteness**: Missing a valid entry in an existing allowlist, registry, or configuration map.
- **Operation Ordering**: Operations executing in a sequentially invalid or rule-violating order.

**Data Processing & Type:**
- **String / Regex / Path Error**: Flawed string manipulation, regex patterns, or path/URL construction.
- **Type Mismatch / Implicit Conversion**: Unintended behavior caused by implicit data type conversions or lack of type-specific handling.
- **Parser / Grammar Rule Missing**: A language tool or parser is missing a required token or grammar rule definition.

**Defense & Exception Safety:**
- **Null / Existence Check Missing**: Missing checks for `null/None/nil`, or incorrectly treating falsy values as absent.
- **Unhandled Exception**: Failing to catch or handle an expected exception or error path.
- **Silent Exception**: Catching an exception but swallowing it silently or masking the real error.

**State & Lifecycle:**
- **State Transition**: Incorrect object state updates, invalid transitions, or violating data-flow dependencies.
- **Concurrency / Stale Data**: Race conditions, unsynchronized shared state, or closures capturing stale data.
- **Resource Lifecycle**: Improper management of resources (e.g., leaks, missing cleanups) across their lifecycle.

### Multi-Bug Review Protocol
The provided files pending review in the repo may contain multiple, independent critical failures. You must execute an exhaustive code review:
- **No Early Exit**: Scan exhaustively. Do not stop after finding the first bug.
- **Surgical Isolation**: Document and fix each bug independently. Never combine unrelated patches into a monolithic refactor.

[Files Pending Review]
{files_pending_review}

Output Contract
- You MUST write the final predicted code patch to
  `.active-swe-tmp/solution.patch`.
- The patch must be a unified diff produced from the repository in the current
  working directory.
- The patch must contain ONLY source-code changes required to fix the bugs.
- Do NOT include tests, generated files, configuration changes, documentation
  changes, formatting-only edits, or temporary files in the final patch.

Operational Constraints
- The current working directory contains the project Git repository.
- Use only the repository, files, tools, and dependencies already available in
  the container. External network access is prohibited.
- Do not use WebSearch, WebFetch, network clients, online package installation,
  or network-capable Git commands.
- Do not look up the benchmark instance, issue, pull request, commit, upstream
  patch, repository history, or matching source code outside the current
  project.
- Modify only source files directly related to the bugs.
- Do not modify tests, setup.py, setup.cfg, pyproject.toml, tox.ini, Makefile,
  .github files, or documentation.
- Use `.active-swe-tmp` for temporary reproduction scripts; do not leave
  temporary files elsewhere in the project.
- Use non-interactive commands only. Do not use vim, nano, less, or commands
  that require user input.
- Use `git --no-pager` for git output.

Recommended Workflow
1. Read the files listed in the [Files Pending Review] section.
2. Inspect nearby code and call sites to understand intended behavior.
3. Reproduce each suspected bug with a minimal script under `.active-swe-tmp`.
4. Identify the root cause of each distinct bug.
5. Implement the smallest source-code fix for each bug.
6. Re-run your reproduction checks and relevant targeted tests if available.
7. Verify the final diff contains only the intended source-code fixes.

Submission
After the fixes are complete, run:

```bash
git --no-pager diff > .active-swe-tmp/solution.patch
```

Then stop. Do not provide a natural-language summary instead of the patch file.
