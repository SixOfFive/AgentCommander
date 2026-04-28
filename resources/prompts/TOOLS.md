# Available Tools

These tools are executed by the **program** — not by the model. The model outputs a JSON decision, the program performs the action and returns the result.

## Web & Network

### fetch
Fetch a URL and return the content (HTML converted to markdown).
```json
{"action": "fetch", "url": "https://example.com/api/data"}
```
- Supports HTTP/HTTPS
- HTML pages are converted to markdown automatically
- JSON APIs return raw JSON
- Results are truncated to 50KB
- SSRF protection blocks private/internal IPs
- URLs with placeholder API keys (like `<API_KEY>`) are rejected

### http_request
Make an HTTP request with full control over method, headers, and body.
```json
{"action": "http_request", "url": "https://api.example.com/data", "method": "POST", "headers": {"Content-Type": "application/json"}, "body": "{\"key\": \"value\"}"}
```
- Methods: GET, POST, PUT, DELETE, PATCH
- Custom headers supported
- Request body as string

## Code Execution

### execute
Run code in the working directory. The program executes it and returns stdout/stderr.
```json
{"action": "execute", "language": "python", "input": "print('hello world')"}
```
**Languages:** python, javascript, bash, powershell

**Package installation:**
```json
{"action": "execute", "language": "pip", "input": "requests matplotlib pandas"}
{"action": "execute", "language": "npm", "input": "axios chart.js"}
```
- Python runs in an isolated `.ec_venv/` virtual environment
- Node.js uses local `node_modules/` (never global)
- Always install packages BEFORE importing them
- Code must be complete and runnable — no placeholders
- Stdout and stderr are captured and returned
- 60-second timeout per execution

## File Operations

All file operations are sandboxed to the conversation's working directory. Path traversal (`../../`) is blocked.

### read_file
Read a file's contents.
```json
{"action": "read_file", "path": "src/index.ts"}
```

### write_file
Create or overwrite a file. Parent directories are created automatically.
```json
{"action": "write_file", "path": "src/utils/helper.ts", "content": "export function add(a: number, b: number) { return a + b; }"}
```

### list_dir
List directory contents with file types and sizes.
```json
{"action": "list_dir", "path": "."}
```

### search
Search files for a text pattern. Returns matching lines with file paths and line numbers.
```json
{"action": "search", "pattern": "TODO|FIXME"}
```

### delete_file
Delete a file within the sandbox.
```json
{"action": "delete_file", "path": "temp/output.log"}
```

## Git Operations

### git
Run git commands in the working directory.

**Available commands:** init, status, add, commit, log, diff

```json
{"action": "git", "command": "init"}
{"action": "git", "command": "status"}
{"action": "git", "command": "add", "files": "."}
{"action": "git", "command": "commit", "message": "feat: initial commit"}
{"action": "git", "command": "log"}
{"action": "git", "command": "diff"}
```

## Process Management

### start_process
Start a long-running background process (e.g., dev server).
```json
{"action": "start_process", "command": "npm run dev"}
```

### read_env
Read variables from a `.env` file in the working directory.
```json
{"action": "read_env"}
```
- Returns key-value pairs from `.env`
- Does NOT modify `process.env`

## Free APIs (No Key Required)

These APIs can be used directly with the `fetch` tool:

| Service | URL | Returns |
|---------|-----|---------|
| Weather | `https://wttr.in/{City}?format=j1` | JSON weather data |
| Weather (simple) | `https://wttr.in/{City}?format=3` | One-line summary |
| Geocoding | `https://open-meteo.com/en/docs` | Coordinates, elevation |
| IP Geolocation | `https://ipapi.co/json/` | Location from IP |
| Exchange Rates | `https://open.er-api.com/v6/latest/USD` | Currency rates |
| Google News RSS | `https://news.google.com/rss/search?q={query}` | News headlines (XML) |
| Wikipedia | `https://en.wikipedia.org/api/rest_v1/page/summary/{title}` | Article summary |
| Random Facts | `https://uselessfacts.jsph.pl/api/v2/facts/random` | Random fact |
| Jokes | `https://official-joke-api.appspot.com/random_joke` | Random joke |
| Cat Facts | `https://catfact.ninja/fact` | Random cat fact |

## Browser Automation

Headless browser actions for interacting with web pages that require JavaScript rendering, form filling, clicking, or visual analysis.

### browse
Navigate to a URL and return the JS-rendered page text (unlike `fetch` which only gets raw HTML).
```json
{"action": "browse", "reasoning": "need JS-rendered content", "url": "https://example.com/dashboard"}
```
- Page stays open between actions — you can browse, then click, then extract
- Returns page text content (up to 50KB)
- Use this instead of `fetch` when the page needs JavaScript to render

### screenshot
Take a screenshot of the current page (or navigate to a URL first). The screenshot is automatically sent to the Vision Agent for analysis.
```json
{"action": "screenshot", "reasoning": "capture visual state", "url": "https://example.com"}
```
- Returns a PNG screenshot
- Screenshot auto-saved to `.ec_images/` in working directory
- Vision Agent can describe what it sees

### click
Click an element by CSS selector on the current page.
```json
{"action": "click", "reasoning": "click login button", "input": "button.login-btn"}
```

### type_text
Type text into an input field by CSS selector.
```json
{"action": "type_text", "reasoning": "fill search box", "path": "input#search", "input": "EngineCommander"}
```

### extract_text
Extract text content from a specific element by CSS selector.
```json
{"action": "extract_text", "reasoning": "get price", "input": "span.price"}
```

### evaluate_js
Run JavaScript on the current page and return the result.
```json
{"action": "evaluate_js", "reasoning": "count items", "input": "document.querySelectorAll('.item').length"}
```

**Browser workflow example:**
1. `browse` → open the page
2. `type_text` → fill in a search box
3. `click` → click the search button
4. `extract_text` → get the results
5. `screenshot` → capture visual state for Vision Agent

## Image Generation

### generate_image
Generate an image from a text description using Stable Diffusion or ComfyUI.
```json
{"action": "generate_image", "reasoning": "create a visual", "input": "a futuristic city skyline at sunset, cyberpunk style, highly detailed"}
```
- The input should be a detailed image prompt
- The generated image is saved to `.ec_images/` in the working directory
- Returns the file path on success
- Requires `imageGeneration.enabled: true` in config.json
- If not configured, returns an error explaining how to set it up

**Prompt tips for better results:**
- Be specific: "a red 2024 Toyota Camry on a mountain road at golden hour" not "a car"
- Include style: "digital art", "photorealistic", "watercolor", "3D render"
- Include quality: "highly detailed", "4K", "professional photography"

## File Management

### rename_file
Rename or move a file within the workspace.
```json
{"action": "rename_file", "path": "old_name.py", "input": "new_name.py"}
```

### copy_file
Copy a file to a new location. Creates destination directories automatically.
```json
{"action": "copy_file", "path": "src/utils.py", "input": "src/utils_backup.py"}
```

### move_file
Move a file to a new location.
```json
{"action": "move_file", "path": "temp/output.csv", "input": "results/output.csv"}
```

### merge_files
Concatenate multiple files into one output file.
```json
{"action": "merge_files", "input": "file1.py file2.py file3.py", "path": "merged.py"}
```

## Data & Query Tools

### json_query
Query a JSON file using dot-path notation.
```json
{"action": "json_query", "path": "data.json", "input": "users.0.name"}
```
- Supports array indices (`items.3`), `.length` for array size, `.keys` for object keys
- Returns formatted JSON for objects/arrays

### csv_read
Read a CSV file (first 50 rows).
```json
{"action": "csv_read", "path": "data.csv"}
```

### sql_query
Execute a SQL query against a SQLite database.
```json
{"action": "sql_query", "path": "data.db", "input": "SELECT * FROM users LIMIT 10"}
```

## Search & Replace

### regex_replace
Find and replace text in a file using regex.
```json
{"action": "regex_replace", "path": "config.py", "input": "DEBUG = True|||DEBUG = False"}
```
- Format: `pattern|||replacement` or `pattern → replacement`
- Uses JavaScript regex syntax with global+multiline flags

### multi_patch
Apply targeted edits to multiple files atomically.
```json
{"action": "multi_patch", "input": "[{\"path\":\"app.py\",\"line\":10,\"end_line\":12,\"content\":\"new line 10\\nnew line 11\"}]"}
```
- Input is a JSON array of patches
- Each patch: `{path, line, end_line?, content}`
- Lines are 1-indexed

## System & Environment

### env_check
Check available tools and runtime versions (Python, Node, Git, pip, etc.).
```json
{"action": "env_check"}
```

### disk_usage
Show workspace disk usage breakdown.
```json
{"action": "disk_usage"}
```

### count_lines
Count lines/words/chars in a file or all source files in a directory.
```json
{"action": "count_lines", "path": "src/"}
```

### hash_file
Get MD5 and SHA256 checksums of a file.
```json
{"action": "hash_file", "path": "output.zip"}
```

### watch_process
Check if a background process is still running, or list running processes.
```json
{"action": "watch_process", "input": "12345"}
```

### download_file
Download a URL directly to a file in the workspace.
```json
{"action": "download_file", "url": "https://example.com/data.csv", "path": "data.csv"}
```
- Follows redirects automatically
- 60-second timeout

## Completion

### done
Signal that the task is complete. Include the final answer in the `input` field.
```json
{"action": "done", "reasoning": "task complete", "input": "Here is the result: ..."}
```
