import argparse
import csv
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

import requests
from tqdm import tqdm

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

try:
    import openpyxl
except ImportError:
    openpyxl = None

BASE_URL = "https://api.pillows.su"
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 30
UPLOAD_TIMEOUT = 120
DONE_TIMEOUT = 300
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 2
DEFAULT_PART_RETRIES = 2
DEFAULT_CHUNK_CONCURRENCY = 1
__version__ = "0.1.0"


def _headers(api_key: str | None) -> dict:
    return {"x-api-key": api_key} if api_key else {}


def init_upload(session, base_url, fname, fsize, api_key, timeout):
    resp = session.post(
        f"{base_url}/api/upload/init",
        json={"fileName": fname, "fileSize": fsize},
        headers=_headers(api_key),
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        raise RuntimeError(f"init failed: {payload.get('message')}")
    return payload["message"]["id"]


def upload_part(session, base_url, task_id, fname, chunk, part_no, api_key, timeout):
    files = {"file": (fname, chunk, "application/octet-stream")}
    data = {"part": part_no}
    resp = session.put(
        f"{base_url}/api/upload/{task_id}/part",
        files=files,
        data=data,
        headers=_headers(api_key),
        timeout=timeout,
    )
    resp.raise_for_status()
    if not resp.json().get("success"):
        raise RuntimeError(f"part {part_no} failed: {resp.text}")


def finalize_upload(session, base_url, task_id, api_key, timeout):
    resp = session.get(
        f"{base_url}/api/upload/{task_id}/done",
        headers=_headers(api_key),
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        raise RuntimeError(f"done failed: {payload.get('message')}")
    return payload["message"]["id"]


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class StateFile:
    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, dict] = {}
        self.lock = Lock()
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    self.entries[entry["path"]] = entry
                except json.JSONDecodeError:
                    self.entries[line] = {"path": line}

    def get(self, path: str) -> dict | None:
        return self.entries.get(path)

    def record(self, path: str, **kwargs):
        with self.lock:
            self.entries[path] = {"path": path, **kwargs}
            self._write()

    def _write(self):
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w") as f:
            for entry in self.entries.values():
                f.write(json.dumps(entry) + "\n")
        os.replace(tmp, self.path)


class Config:
    def __init__(self, explicit_path: str | None = None):
        self.data: dict[str, str] = {}
        if explicit_path:
            p = Path(explicit_path)
            if p.suffix == ".toml":
                self._load_toml(explicit_path)
            else:
                self._load_env_file(p)
        else:
            self._load_env()
            self._load_toml()

    def _load_env(self):
        self._load_env_file(Path(".env"))

    def _load_env_file(self, path: Path):
        if not path.is_file():
            return
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                self.data[key.strip()] = value.strip().strip('"').strip("'")

    def _load_toml(self, explicit_path: str | None = None):
        path = Path(explicit_path) if explicit_path else Path("pillows-uploader.toml")
        if not path.is_file() or tomllib is None:
            return
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
            sections = []
            if "pillows-uploader" in data:
                sections.append(data["pillows-uploader"])
            if "tool" in data and isinstance(data["tool"], dict) and "pillows-uploader" in data["tool"]:
                sections.append(data["tool"]["pillows-uploader"])
            for section in sections:
                if isinstance(section, dict):
                    for k, v in section.items():
                        if isinstance(v, (str, int, float, bool)):
                            self.data[k] = str(v)
        except Exception:
            pass

    def get(self, key: str, default=None):
        return self.data.get(key, default)


class OutputWriter:
    def __init__(self, fmt: str, path: str):
        self.fmt = fmt
        self.path = path
        self._file = None
        self._writer = None
        self._results = []

    def __enter__(self):
        if self.fmt in ("csv", "ndjson"):
            self._file = open(self.path, "w", newline="")
            if self.fmt == "csv":
                self._writer = csv.DictWriter(self._file, fieldnames=["file_path", "pillows_su_link"])
                self._writer.writeheader()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._file:
            self._file.close()
        if exc_type is None and self.fmt not in ("csv", "ndjson"):
            self._flush_buffered()

    def write(self, result: dict):
        if self.fmt == "csv":
            self._writer.writerow({"file_path": result["file_path"], "pillows_su_link": result["pillows_su_link"]})
            self._file.flush()
        elif self.fmt == "ndjson":
            self._file.write(json.dumps(result) + "\n")
            self._file.flush()
        else:
            if self.fmt == "xlsx" and openpyxl is None:
                raise RuntimeError("openpyxl is required for xlsx output. Install with: pip install openpyxl")
            self._results.append(result)

    def _flush_buffered(self):
        if self.fmt == "json":
            with open(self.path, "w") as f:
                json.dump(self._results, f, indent=2)
        elif self.fmt == "html":
            with open(self.path, "w") as f:
                f.write("<html><body><table><tr><th>file_path</th><th>pillows_su_link</th></tr>")
                for r in self._results:
                    f.write(f"<tr><td>{r['file_path']}</td><td><a href='{r['pillows_su_link']}'>{r['pillows_su_link']}</a></td></tr>")
                f.write("</table></body></html>")
        elif self.fmt == "xlsx":
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(["file_path", "pillows_su_link"])
            for r in self._results:
                ws.append([r["file_path"], r["pillows_su_link"]])
            wb.save(self.path)


def print_completions(shell: str) -> None:
    options = [
        "-o", "-k", "--base-url", "--chunk-size", "-c", "--chunk-concurrency", "-r", "--part-retries",
        "--backoff", "--timeout", "--dry-run", "-v", "-q", "--version", "--resume", "--state-file", "--no-csv",
        "--delete", "--no-progress", "--completions", "--config", "--ext", "--min-size", "--max-size",
        "--format", "paths",
    ]

    if shell == "bash":
        print("COMPREPLY=()")
        opts = " ".join(options)
        print(f'opts="{opts}"')
        print('_pillows_upload() {')
        print('    local cur="${COMP_WORDS[COMP_CWORD]}"')
        print('    COMPREPLY=( $(compgen -W "${opts}" -- "${cur}") )')
        print('    return 0')
        print("}")
        print("complete -F _pillows_upload pillows-upload")
    elif shell == "zsh":
        print("#compdef pillows-upload")
        print("_pillows_upload() {")
        print('    local -a args')
        print('    args=(" \\')
        for opt in options:
            print(f"        '{opt}' \\")
        print('        "*:file:_files"')
        print("    )")
        print('    _arguments $args')
        print("}")
        print("(( ${+functions[compdef]} )) && compdef _pillows_upload pillows-upload")
    elif shell == "fish":
        print("complete -c pillows-upload")
        for opt in options:
            if opt == "paths":
                print("    -r")
            elif opt.startswith("--"):
                print(f"    -l {opt[2:]}")
            elif opt.startswith("-"):
                print(f"    -s {opt[1:]}")
    else:
        print(f"Unknown shell: {shell}", file=sys.stderr)
        sys.exit(1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Upload files to pillows.su via chunked upload API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("paths", nargs="*", default=["./downloads"], help="files or directories to upload (default: ./downloads)")
    p.add_argument("-o", "--output", default=None, help="output path (default: upload_map.csv)")
    p.add_argument("-k", "--api-key", default=None, help="API key (default: PILLOWS_API_KEY env var)")
    p.add_argument("--base-url", default=None, help=f"API base URL (default: {BASE_URL})")
    p.add_argument("--chunk-size", type=int, default=None, help=f"chunk size in bytes (default: {DEFAULT_CHUNK_SIZE})")
    p.add_argument("-c", "--concurrency", type=int, default=None, help="parallel file uploads (default: 1)")
    p.add_argument("--chunk-concurrency", type=int, default=None, help="parallel chunk uploads per file (default: 1)")
    p.add_argument("-r", "--retries", type=int, default=None, help=f"retry count per file (default: {DEFAULT_RETRIES})")
    p.add_argument("--part-retries", type=int, default=None, help=f"retry count per chunk (default: {DEFAULT_PART_RETRIES})")
    p.add_argument("--backoff", type=int, default=None, help=f"exponential backoff base (default: {DEFAULT_BACKOFF})")
    p.add_argument("--timeout", type=int, default=None, help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT})")
    p.add_argument("--dry-run", action="store_true", help="simulate uploads without sending data")
    p.add_argument("-v", "--verbose", action="store_true", help="print detailed output")
    p.add_argument("-q", "--quiet", action="store_true", help="suppress all non-error output")
    p.add_argument("--version", action="version", version=f"pilllows-uploader {__version__}", help="show version and exit")
    p.add_argument("--resume", action="store_true", help="skip files already uploaded (requires state file)")
    p.add_argument("--state-file", default=None, help="state file for resume (default: .upload_state)")
    p.add_argument("--no-csv", action="store_true", help="skip writing the output CSV")
    p.add_argument("--delete", action="store_true", help="delete local files after successful upload")
    p.add_argument("--no-progress", action="store_true", help="disable progress bars")
    p.add_argument("--completions", choices=["bash", "zsh", "fish"], help="print shell completions")
    p.add_argument("--config", default=None, help="config file path (default: .env or pillows-uploader.toml in current dir)")
    p.add_argument("--ext", nargs="*", help="only upload files with these extensions (e.g. .mp3 .wav)")
    p.add_argument("--min-size", type=int, default=None, help="skip files smaller than N bytes")
    p.add_argument("--max-size", type=int, default=None, help="skip files larger than N bytes (0 = no limit)")
    p.add_argument("--format", choices=["csv", "json", "ndjson", "html", "xlsx"], default=None, help="output format (default: csv)")
    return p.parse_args(argv)


def collect_files(paths, extensions, min_size, max_size, verbose):
    ext_set = {e if e.startswith(".") else f".{e}" for e in extensions} if extensions else None
    min_sz = min_size if min_size is not None else 0
    max_sz = max_size if max_size is not None else 0
    files = []
    for p in paths:
        path = Path(p)
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = sorted(path.rglob("*"))
        else:
            if verbose:
                print(f"Skipping {p}: not a file or directory")
            continue

        for f in candidates:
            if not f.is_file():
                continue
            if ext_set and f.suffix.lower() not in ext_set:
                continue
            size = f.stat().st_size
            if size < min_sz:
                if verbose:
                    print(f"Skipping {f}: too small ({size} < {min_sz})")
                continue
            if max_sz > 0 and size > max_sz:
                if verbose:
                    print(f"Skipping {f}: too large ({size} > {max_sz})")
                continue
            files.append(f)
    return sorted(files)


def validate_args(args: argparse.Namespace) -> None:
    if args.chunk_size is not None and args.chunk_size <= 0:
        raise ValueError("--chunk-size must be greater than 0")
    if args.concurrency is not None and args.concurrency < 1:
        raise ValueError("--concurrency must be at least 1")
    if args.chunk_concurrency is not None and args.chunk_concurrency < 1:
        raise ValueError("--chunk-concurrency must be at least 1")
    if args.retries is not None and args.retries < 0:
        raise ValueError("--retries must be 0 or greater")
    if args.part_retries is not None and args.part_retries < 0:
        raise ValueError("--part-retries must be 0 or greater")
    if args.backoff is not None and args.backoff < 1:
        raise ValueError("--backoff must be at least 1")
    if args.timeout is not None and args.timeout <= 0:
        raise ValueError("--timeout must be greater than 0")
    if args.min_size is not None and args.min_size < 0:
        raise ValueError("--min-size must be 0 or greater")
    if args.max_size is not None and args.max_size < 0:
        raise ValueError("--max-size must be 0 or greater")
    if (
        args.min_size is not None
        and args.max_size is not None
        and args.max_size > 0
        and args.max_size < args.min_size
    ):
        raise ValueError("--max-size must be greater than or equal to --min-size")


def upload_one(
    fpath: Path,
    base_url: str,
    api_key: str | None,
    chunk_size: int,
    retries: int,
    part_retries: int,
    backoff: int,
    dry_run: bool,
    verbose: bool,
    timeout: int,
    progress: bool = True,
    session=None,
    state: StateFile | None = None,
    chunk_concurrency: int = 1,
) -> dict:
    size = fpath.stat().st_size
    sha256 = compute_sha256(fpath)
    abs_path = str(fpath.resolve())
    total_parts = max(1, (size + chunk_size - 1) // chunk_size)

    if verbose:
        print(f"  Size: {size} bytes")

    if dry_run:
        return {
            "file_path": abs_path,
            "pillows_su_link": f"https://pillows.su/f/DRY_RUN_{fpath.name}",
            "size": size,
            "sha256": sha256,
            "elapsed": 0,
            "parts_uploaded": 0,
            "retries": 0,
        }

    cached = state.get(abs_path) if state else None
    if (
        cached
        and cached.get("url")
        and cached.get("size") == size
        and cached.get("sha256") == sha256
        and cached.get("parts_uploaded", 0) >= total_parts
    ):
        if verbose:
            print(f"  Skipping (unchanged): {cached.get('url')}")
        return {
            "file_path": abs_path,
            "pillows_su_link": cached.get("url", ""),
            "size": size,
            "sha256": sha256,
            "elapsed": 0,
            "parts_uploaded": cached.get("parts_uploaded", 0),
            "retries": 0,
        }

    attempt = 0
    start = time.time()
    total_retries = 0

    while True:
        try:
            task_id = init_upload(session or requests, base_url, fpath.name, size, api_key, timeout)
            if verbose:
                print(f"  Task ID: {task_id}")

            part_no = 0
            chunks = []
            with open(fpath, "rb") as fh:
                while True:
                    chunk = fh.read(chunk_size)
                    if not chunk:
                        break
                    part_no += 1
                    chunks.append((part_no, chunk))

            skip_parts = 0
            if cached and cached.get("parts_uploaded"):
                skip_parts = cached["parts_uploaded"]
                chunks = [(p, c) for p, c in chunks if p > skip_parts]
                if verbose and chunks:
                    print(f"  Resuming from part {skip_parts + 1}")

            pbar = None
            if progress and not verbose:
                pbar = tqdm(
                    total=size,
                    desc=fpath.name,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    leave=False,
                    initial=skip_parts * chunk_size,
                )

            uploaded_parts = skip_parts

            def _upload_chunk(part_data):
                p, chunk = part_data
                part_attempt = 0
                while True:
                    try:
                        upload_part(session or requests, base_url, task_id, fpath.name, chunk, p, api_key, timeout)
                        if pbar:
                            pbar.update(len(chunk))
                        return True, 0
                    except (requests.RequestException, RuntimeError):
                        part_attempt += 1
                        if part_attempt >= part_retries:
                            return False, part_attempt
                        wait = backoff ** part_attempt
                        time.sleep(wait)
                return False, 0

            if chunk_concurrency > 1 and len(chunks) > 1:
                with ThreadPoolExecutor(max_workers=chunk_concurrency) as pool:
                    futures = {pool.submit(_upload_chunk, pd): pd for pd in chunks}
                    for future in as_completed(futures):
                        ok, retries = future.result()
                        uploaded_parts += 1 if ok else 0
                        total_retries += retries
                        if not ok:
                            raise RuntimeError(f"part {futures[future][0]} failed after {part_retries} attempts")
            else:
                for p, chunk in chunks:
                    part_attempt = 0
                    while True:
                        try:
                            upload_part(session or requests, base_url, task_id, fpath.name, chunk, p, api_key, timeout)
                            uploaded_parts += 1
                            if pbar:
                                pbar.update(len(chunk))
                            break
                        except (requests.RequestException, RuntimeError) as e:
                            part_attempt += 1
                            total_retries += 1
                            if part_attempt >= part_retries:
                                raise RuntimeError(f"part {p} failed after {part_retries} attempts: {e}") from e
                            wait = backoff ** part_attempt
                            if verbose:
                                print(f"  Part {p} retry {part_attempt}/{part_retries} after {wait}s: {e}")
                            time.sleep(wait)

            if pbar:
                pbar.close()

            file_id = finalize_upload(session or requests, base_url, task_id, api_key, timeout)
            elapsed = time.time() - start
            url = f"https://pillows.su/f/{file_id}"

            if state:
                state.record(abs_path, size=size, sha256=sha256, parts_uploaded=uploaded_parts, url=url)

            return {
                "file_path": abs_path,
                "pillows_su_link": url,
                "size": size,
                "sha256": sha256,
                "elapsed": round(elapsed, 2),
                "parts_uploaded": uploaded_parts,
                "retries": total_retries,
            }
        except (requests.RequestException, RuntimeError) as e:
            attempt += 1
            total_retries += 1
            if attempt >= retries:
                raise RuntimeError(f"Failed after {retries} attempts: {e}") from e
            wait = backoff ** attempt
            if verbose:
                print(f"  Retry {attempt}/{retries} after {wait}s: {e}")
            time.sleep(wait)
        finally:
            if pbar:
                pbar.close()


def load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return {}
    with open(state_file) as f:
        return {line.strip(): {} for line in f if line.strip()}


def save_state(state_file: Path, uploaded: set[str]) -> None:
    with open(state_file, "w") as f:
        for path in sorted(uploaded):
            f.write(path + "\n")


def upload_task(fpath: Path, args: argparse.Namespace, uploaded: set[str]) -> dict | None:
    try:
        result = upload_one(
            fpath,
            base_url=args.base_url,
            api_key=args.api_key,
            chunk_size=getattr(args, "chunk_size", DEFAULT_CHUNK_SIZE),
            retries=getattr(args, "retries", DEFAULT_RETRIES),
            part_retries=getattr(args, "part_retries", DEFAULT_PART_RETRIES),
            backoff=getattr(args, "backoff", DEFAULT_BACKOFF),
            dry_run=args.dry_run,
            verbose=getattr(args, "verbose", False),
            timeout=getattr(args, "timeout", DEFAULT_TIMEOUT),
            progress=not getattr(args, "no_progress", False),
            session=getattr(args, "_session", None),
            state=getattr(args, "_state", None),
            chunk_concurrency=getattr(args, "chunk_concurrency", 1),
        )
        return {"file_path": result["file_path"], "pillows_su_link": result["pillows_su_link"]}
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.completions:
        print_completions(args.completions)
        return 0

    try:
        validate_args(args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    config = Config(args.config)

    base_url = args.base_url or config.get("base_url") or BASE_URL
    api_key = args.api_key or os.environ.get("PILLOWS_API_KEY") or config.get("api_key")
    if not api_key and not args.dry_run:
        print("Error: API key required. Set PILLOWS_API_KEY, use -k, or add it to config.", file=sys.stderr)
        return 1
    args.api_key = api_key

    state_path = args.state_file if args.state_file is not None else ".upload_state"
    chunk_size = args.chunk_size if args.chunk_size is not None else DEFAULT_CHUNK_SIZE
    concurrency = args.concurrency if args.concurrency is not None else 1
    chunk_concurrency = args.chunk_concurrency if args.chunk_concurrency is not None else DEFAULT_CHUNK_CONCURRENCY
    retries = args.retries if args.retries is not None else DEFAULT_RETRIES
    part_retries = args.part_retries if args.part_retries is not None else DEFAULT_PART_RETRIES
    backoff = args.backoff if args.backoff is not None else DEFAULT_BACKOFF
    timeout = args.timeout if args.timeout is not None else DEFAULT_TIMEOUT
    min_size = args.min_size if args.min_size is not None else 0
    max_size = args.max_size if args.max_size is not None else 0
    fmt = args.format if args.format is not None else "csv"
    if args.no_csv:
        fmt = "none"
    output_path = args.output if args.output is not None else f"upload_map.{fmt if fmt != 'none' else 'csv'}"

    args.chunk_size = chunk_size
    args.concurrency = concurrency
    args.chunk_concurrency = chunk_concurrency
    args.retries = retries
    args.part_retries = part_retries
    args.backoff = backoff
    args.timeout = timeout
    args.min_size = min_size
    args.max_size = max_size
    args.format = fmt
    args.base_url = base_url
    args.output = output_path
    args.state_file = state_path
    if args.quiet:
        args.verbose = False

    files = collect_files(args.paths, args.ext, args.min_size, args.max_size, not args.quiet)
    if not files:
        if not args.quiet:
            print("Error: no files found matching criteria")
        return 1

    state = StateFile(Path(args.state_file)) if args.resume else None
    uploaded = set()
    if state:
        for path, entry in state.entries.items():
            if entry.get("url") or (len(entry) == 1 and entry.get("path")):
                uploaded.add(path)

    if args.resume and uploaded:
        before = len(files)
        files = [f for f in files if str(f.resolve()) not in uploaded]
        skipped = before - len(files)
        if not args.quiet and skipped:
            print(f"Resume: skipped {skipped} already uploaded")

    if not files:
        if not args.quiet:
            print("Nothing to upload")
        return 0

    show_output = not args.quiet
    if show_output:
        print(f"Found {len(files)} file(s) to upload.")

    progress = not args.no_progress and not args.quiet
    results = []
    errors = 0
    sessions = [requests.Session() for _ in range(max(args.concurrency, 1))]
    overall_start = time.time()

    def do_upload(fpath: Path, session) -> dict | None:
        if show_output:
            print(f"Uploading: {fpath.name}")
        try:
            result = upload_one(
                fpath,
                base_url=args.base_url,
                api_key=args.api_key,
                chunk_size=args.chunk_size,
                retries=args.retries,
                part_retries=args.part_retries,
                backoff=args.backoff,
                dry_run=args.dry_run,
                verbose=args.verbose,
                timeout=args.timeout,
                progress=progress,
                session=session,
                state=state,
                chunk_concurrency=args.chunk_concurrency,
            )
            if show_output:
                if args.verbose:
                    mbps = (result["size"] / 1024 / 1024) / result["elapsed"] if result["elapsed"] > 0 else 0
                    print(f"  OK -> {result['pillows_su_link']} ({result['elapsed']}s, {mbps:.2f} MB/s, {result['retries']} retries)")
                else:
                    print(f"  OK -> {result['pillows_su_link']}")
            return result
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            return None

    try:
        if args.concurrency > 1:
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                futures = {}
                for i, fpath in enumerate(files):
                    session = sessions[i % len(sessions)]
                    fut = pool.submit(do_upload, fpath, session)
                    futures[fut] = fpath
                for future in as_completed(futures):
                    try:
                        result = future.result()
                        if result:
                            results.append(result)
                        else:
                            errors += 1
                    except Exception as e:
                        errors += 1
                        print(f"  ERROR: {e}", file=sys.stderr)
        else:
            session = sessions[0] if sessions else None
            for fpath in files:
                result = do_upload(fpath, session)
                if result:
                    results.append(result)
                else:
                    errors += 1
    except KeyboardInterrupt:
        if show_output:
            print("\nInterrupted - saving progress...")
        if state:
            for r in results:
                state.record(r["file_path"], size=r["size"], sha256=r["sha256"], parts_uploaded=r["parts_uploaded"], url=r["pillows_su_link"])
        return 130

    for s in sessions:
        s.close()

    if fmt != "none" and results:
        out = Path(args.output)
        writer = OutputWriter(fmt, str(out))
        with writer:
            for r in results:
                writer.write(r)
        if show_output:
            print(f"\nOutput: {out.resolve()}")
    elif show_output and not results:
        print("\nNo results to write.")

    if args.delete:
        for r in results:
            p = Path(r["file_path"])
            if p.exists():
                p.unlink()
                if show_output and args.verbose:
                    print(f"Deleted: {p}")

    if show_output:
        overall_elapsed = time.time() - overall_start
        total_bytes = sum(r["size"] for r in results)
        avg_mbps = (total_bytes / 1024 / 1024) / overall_elapsed if overall_elapsed > 0 else 0
        separator = "=" * 50
        print(f"\n{separator}")
        print(f"Done. Uploaded: {len(results)}  Errors: {errors}")
        print(f"Total: {total_bytes / 1024 / 1024:.2f} MB in {overall_elapsed:.2f}s ({avg_mbps:.2f} MB/s)")
        print(separator)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
