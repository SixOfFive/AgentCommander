# Workflow Recipes

Proven step-by-step patterns for common task types. Follow these sequences for reliable results.

## Recipe: Fetch and Process Data

When asked to get data from the web and do something with it:

1. `fetch` — get the raw data from the URL
2. `execute` (python) — parse, filter, transform the data
3. `write_file` — save results to a file if requested
4. `done` — present the findings to the user

**Key rule**: Always parse data with code (Python), never try to summarize raw JSON/XML directly.

## Recipe: Write and Run Code

When asked to write a script or program:

1. `write_file` — write the complete code file
2. `execute` (pip) — install any third-party dependencies detected
3. `execute` (python/js) — run the code
4. If fails → `debug` to diagnose, then fix with `write_file`, then re-execute
5. `done` — present the output and explain what the code does

**Key rule**: Always execute code after writing. Never call done with unverified code.

## Recipe: Multi-File Project

When asked to build a project with multiple files:

1. `plan` — break the project into files and dependencies
2. `write_file` — create each file (start with utilities, then main)
3. `execute` (pip/npm) — install dependencies
4. `execute` — run the main file or test suite
5. `review` — quality check the code
6. `done` — present the project structure and how to use it

**Key rule**: Write dependency files (utils, models) before files that import them.

## Recipe: Research and Summarize

When asked to research a topic or compare options:

1. `fetch` — get information from multiple sources (2-4 URLs)
2. `execute` (python) — extract and organize key facts
3. `done` — present a structured comparison or summary

**Key rule**: Fetch multiple sources for balanced information. Don't rely on a single URL.

## Recipe: Debug a Problem

When code fails or produces wrong output:

1. `read_file` — read the failing code
2. `execute` — reproduce the error
3. `debug` — send error + code to the debugger agent for diagnosis
4. `write_file` — apply the fix
5. `execute` — verify the fix works
6. `done` — explain what was wrong and how it was fixed

**Key rule**: Always reproduce the error before fixing. Never guess at fixes.

## Recipe: Data Pipeline

When asked to process CSV/JSON/database data:

1. `read_file` or `csv_read` — examine the input data structure
2. `execute` (pip) — install pandas or other data libraries
3. `write_file` — write the processing script
4. `execute` — run the pipeline
5. `done` — present results with statistics

**Key rule**: Always examine the data format first before writing processing code.

## Recipe: Web Scraping

When asked to extract information from websites:

1. `fetch` — try simple fetch first
2. If page needs JS → `browse` — use headless browser
3. `extract_text` or `execute` — parse the content
4. `write_file` — save extracted data
5. `done` — present the results

**Key rule**: Try fetch first. Only use browse for JavaScript-heavy pages.

## Recipe: File Modification

When asked to modify an existing file:

1. `read_file` — read the current content
2. `write_file` or `regex_replace` — make the changes
3. `read_file` — verify the changes
4. `execute` — test if it still works (for code files)
5. `done` — explain what was changed

**Key rule**: Always read before writing. Use regex_replace for targeted changes, write_file for rewrites.

## Anti-Patterns (Do NOT Do These)

- **Plan without execute**: Don't make a plan and then call done. Execute the plan.
- **Write without test**: Don't write code and call done. Execute it first.
- **Fetch without parse**: Don't dump raw web content. Process it with code.
- **Multiple list_dir**: Don't scan directories repeatedly. Use workspace_summary once.
- **Apologize instead of try**: Don't say "I can't". Use the tools available.
- **Ask permission**: Don't ask "shall I?" — just do it.
- **Echo the request**: Don't repeat what the user said. Do the work.
