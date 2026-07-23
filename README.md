# pilllows-uploader

Bulk upload files to [pillows.su](https://pillows.su) via the chunked upload API.

Current version: 0.1.0

## Shell Completions

Generate completions for your shell:

```bash
# Bash
eval "$(pillows-upload --completions bash)" >> ~/.bashrc

# Zsh
eval "$(pillows-upload --completions zsh)" >> ~/.zshrc

# Fish
pillows-upload --completions fish > ~/.config/fish/completions/pillows-upload.fish
```

## Install

```bash
pip install git+https://github.com/edideaur/pilllows-uploader.git
```

Or install manually:

```bash
git clone https://github.com/edideaur/pilllows-uploader.git
cd pilllows-uploader
pip install -r requirements.txt
```

## Usage

```bash
pillows-upload [OPTIONS] [PATHS...]
```

**PATHS** - files or directories to upload. Defaults to `./downloads`.

```bash
# Upload everything in ./downloads
pillows-upload

# Upload specific files
pillows-upload song.mp3 image.png

# Upload a directory
pillows-upload ~/Music
```

## Options

| Flag | Description |
|------|-------------|
| `-o, --output` | Output path (default: `upload_map.csv`) |
| `-k, --api-key` | API key (default: `PILLOWS_API_KEY` env var) |
| `--base-url` | API base URL (default: `https://api.pillows.su`) |
| `--chunk-size` | Chunk size in bytes (default: `8388608`) |
| `-c, --concurrency` | Parallel file uploads (default: `1`) |
| `--chunk-concurrency` | Parallel chunk uploads per file (default: `1`) |
| `-r, --retries` | Retry count per file (default: `3`) |
| `--part-retries` | Retry count per chunk (default: `2`) |
| `--backoff` | Exponential backoff base (default: `2`) |
| `--timeout` | HTTP timeout in seconds (default: `30`) |
| `--dry-run` | Simulate uploads without sending data |
| `-v, --verbose` | Print detailed output |
| `-q, --quiet` | Suppress all non-error output |
| `--version` | Show version and exit |
| `--resume` | Skip files already uploaded (uses state file) |
| `--state-file` | State file path for resume (default: `.upload_state`) |
| `--no-csv` | Skip writing the output file |
| `--delete` | Delete local files after successful upload |
| `--no-progress` | Disable progress bars |
| `--completions SHELL` | Print shell completions (`bash`, `zsh`, or `fish`) |
| `--config PATH` | Config file path (default: `.env` or `pillows-uploader.toml` in current dir) |
| `--ext` | Only upload files with these extensions (e.g. `.mp3 .wav`) |
| `--min-size` | Skip files smaller than N bytes |
| `--max-size` | Skip files larger than N bytes (`0` = no limit) |
| `--format` | Output format: `csv`, `json`, `ndjson`, `html`, `xlsx` (default: `csv`) |

## Examples

```bash
# Upload only mp3 and wav files, verbose output
pillows-upload --ext .mp3 .wav -v

# Dry run to see what would be uploaded
pillows-upload --dry-run -v

# Upload with 4 parallel workers
pillows-upload -c 4

# Resume a previous upload session
pillows-upload --resume

# Disable progress bars
pillows-upload --no-progress

# Delete files after upload
pillows-upload --delete

# Skip files under 1MB
pillows-upload --min-size 1048576

# Use a custom API key
pillows-upload -k YOUR_API_KEY
```

## Config File

Set defaults in `.env` or `pillows-uploader.toml` in the current directory so you don't have to repeat flags.

**.env**
```bash
PILLOWS_API_KEY=your-key
BASE_URL=https://api.pillows.su
CHUNK_SIZE=8388608
```

**pillows-uploader.toml**
```toml
[pillows-uploader]
api_key = "your-key"
base_url = "https://api.pillows.su"
chunk_size = "8388608"
```

CLI flags always override config file values.

## Output Formats

Use `--format` to choose the output type:

```bash
pillows-upload --format ndjson
```

| Format | Description |
|--------|-------------|
| `csv` | Standard CSV with header (default) |
| `json` | JSON array written after all uploads complete |
| `ndjson` | JSON Lines - one result object per line, streamed as uploads finish |
| `html` | Simple HTML table |
| `xlsx` | Excel workbook (requires `openpyxl`) |

Use `--no-csv` to skip writing any output file.

## State File

The state file (default: `.upload_state`) tracks uploaded files using JSON Lines. Each entry stores the file path, size, SHA-256, parts uploaded, and final URL. This enables:

- **Resume** - re-run the same command and already-uploaded files are skipped
- **Hash cache** - unchanged files are skipped automatically
- **Partial resume** - if an upload is interrupted, remaining chunks resume from the last successful part (assumes the API handles idempotent/duplicate part uploads)

## Exit Codes

- `0` - all files uploaded successfully
- `1` - one or more uploads failed or no files found
- `130` - interrupted by user (Ctrl+C), progress saved to state file

## Input Validation

The CLI validates all numeric inputs before starting:

- `--chunk-size` must be > 0
- `--concurrency` must be >= 1
- `--chunk-concurrency` must be >= 1
- `--retries` must be >= 0
- `--part-retries` must be >= 0
- `--backoff` must be >= 1
- `--timeout` must be > 0
- `--min-size` must be >= 0
- `--max-size` must be 0 or >= --min-size

## Library Usage

You can import and use `upload_files` directly in your Python app:

```python
from main import upload_files

results = upload_files(
    paths=["./downloads"],
    api_key="your-api-key",
    extensions=[".mp3", ".wav"],
    concurrency=4,
    output="results.json",
    output_format="json",
    resume=True,
    delete=False,
)

for r in results:
    print(r["pillows_su_link"])
```

Available imports:

- `upload_files` - high-level convenience function
- `upload_one` - upload a single Path
- `StateFile` - JSON Lines state persistence with resume support
- `Config` - `.env` and `pillows-uploader.toml` config loader
- `OutputWriter` - streaming CSV, NDJSON, JSON, HTML, XLSX writer

## Getting an API key

1. Pay for pillows premium (10$/month, crypto only)
2. If you run or edit a music tracker, join the trackerhub music discord, then ask for pillows premium in their channel (you must have the editors role) or DM Fragger, otherwise e-mail their contact address and ask for one.
