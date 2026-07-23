import argparse
import csv
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from main import (
    Config,
    OutputWriter,
    StateFile,
    _headers,
    collect_files,
    compute_sha256,
    finalize_upload,
    init_upload,
    load_state,
    main,
    parse_args,
    save_state,
    upload_one,
    upload_part,
    upload_task,
    validate_args,
)


class TestHeaders:
    def test_with_key(self):
        assert _headers("abc123") == {"x-api-key": "abc123"}

    def test_without_key(self):
        assert _headers(None) == {}

    def test_empty_string(self):
        assert _headers("") == {}


class TestComputeSha256:
    def test_returns_hex_digest(self, tmp_path):
        f = tmp_path / "file.bin"
        f.write_bytes(b"hello world")
        result = compute_sha256(f)
        assert len(result) == 64
        assert result == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        result = compute_sha256(f)
        assert len(result) == 64
        assert result == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def test_deterministic(self, tmp_path):
        f = tmp_path / "file.bin"
        f.write_bytes(b"\x00" * 1024)
        assert compute_sha256(f) == compute_sha256(f)

    def test_large_file_deterministic(self, tmp_path):
        f = tmp_path / "large.bin"
        f.write_bytes(b"ab" * (4 * 1024 * 1024))
        result = compute_sha256(f)
        assert len(result) == 64
        assert result == compute_sha256(f)


class TestConfig:
    def test_env_file(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("PILLOWS_API_KEY=envkey123\nBASE_URL=https://custom.api\n")
        monkeypatch.chdir(tmp_path)
        config = Config()
        assert config.get("PILLOWS_API_KEY") == "envkey123"
        assert config.get("BASE_URL") == "https://custom.api"

    def test_toml_file(self, tmp_path, monkeypatch):
        toml = tmp_path / "pillows-uploader.toml"
        toml.write_text('[pillows-uploader]\napikey = "tomlkey"\nchunk_size = "65536"\n')
        monkeypatch.chdir(tmp_path)
        config = Config()
        assert config.get("apikey") == "tomlkey"
        assert config.get("chunk_size") == "65536"

    def test_explicit_config_path(self, tmp_path, monkeypatch):
        env = tmp_path / "myconfig.env"
        env.write_text("MY_KEY=explicit\n")
        monkeypatch.chdir(tmp_path)
        config = Config(str(env))
        assert config.get("MY_KEY") == "explicit"

    def test_missing_config_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = Config()
        assert config.get("anything") is None

    def test_env_file_comments_and_quotes(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text('# comment\nKEY="quoted"\nKEY2=noquotes\n')
        monkeypatch.chdir(tmp_path)
        config = Config()
        assert config.get("KEY") == "quoted"
        assert config.get("KEY2") == "noquotes"

    def test_toml_tool_section(self, tmp_path, monkeypatch):
        toml = tmp_path / "pillows-uploader.toml"
        toml.write_text('[tool.pillows-uploader]\napikey = "toolkey"\nbase_url = "https://tool.api"\n')
        monkeypatch.chdir(tmp_path)
        config = Config()
        assert config.get("apikey") == "toolkey"
        assert config.get("base_url") == "https://tool.api"

    def test_toml_root_section(self, tmp_path, monkeypatch):
        toml = tmp_path / "pillows-uploader.toml"
        toml.write_text('[pillows-uploader]\napikey = "rootkey"\n')
        monkeypatch.chdir(tmp_path)
        config = Config()
        assert config.get("apikey") == "rootkey"

    def test_missing_toml_module(self, tmp_path, monkeypatch):
        toml = tmp_path / "pillows-uploader.toml"
        toml.write_text('[pillows-uploader]\napikey = "key"\n')
        monkeypatch.chdir(tmp_path)
        with patch("main.tomllib", None):
            config = Config()
            assert config.get("apikey") is None

    def test_env_file_skips_blank_lines(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text('\n\nKEY=value\n\n')
        monkeypatch.chdir(tmp_path)
        config = Config()
        assert config.get("KEY") == "value"

    def test_config_explicit_toml_path(self, tmp_path, monkeypatch):
        toml = tmp_path / "custom.toml"
        toml.write_text('[pillows-uploader]\napikey = "tomlpath"\n')
        monkeypatch.chdir(tmp_path)
        config = Config(str(toml))
        assert config.get("apikey") == "tomlpath"


class TestStateFile:
    def test_new_file_is_empty(self, tmp_path):
        state = StateFile(tmp_path / "state")
        assert state.get("anything") is None

    def test_plain_text_backward_compat(self, tmp_path):
        state_file = tmp_path / "state"
        state_file.write_text("/a/file1.mp3\n/file2.wav\n")
        state = StateFile(state_file)
        assert state.get("/a/file1.mp3")["path"] == "/a/file1.mp3"
        assert state.get("/file2.wav")["path"] == "/file2.wav"

    def test_jsonl_format(self, tmp_path):
        state_file = tmp_path / "state"
        state_file.write_text(
            '{"path": "/a.mp3", "size": 100, "sha256": "abc"}\n'
            '{"path": "/b.wav", "size": 200, "sha256": "def"}\n'
        )
        state = StateFile(state_file)
        assert state.get("/a.mp3")["size"] == 100
        assert state.get("/b.wav")["sha256"] == "def"

    def test_record_and_persist(self, tmp_path):
        state_file = tmp_path / "state"
        state = StateFile(state_file)
        state.record("/a.mp3", size=100, sha256="abc", url="https://example.com/a")
        entries = []
        with open(state_file) as f:
            for line in f:
                entries.append(json.loads(line.strip()))
        assert len(entries) == 1
        assert entries[0]["path"] == "/a.mp3"
        assert entries[0]["url"] == "https://example.com/a"

    def test_atomic_write(self, tmp_path):
        state_file = tmp_path / "state"
        state = StateFile(state_file)
        state.record("/a.mp3", size=100)
        assert not state_file.with_suffix(".tmp").exists()

    def test_overwrites_entry(self, tmp_path):
        state_file = tmp_path / "state"
        state = StateFile(state_file)
        state.record("/a.mp3", size=100, url="old")
        state.record("/a.mp3", size=100, url="new")
        assert state.get("/a.mp3")["url"] == "new"

    def test_mixed_plain_and_jsonl(self, tmp_path):
        state_file = tmp_path / "state"
        state_file.write_text(
            "/plain/path.mp3\n"
            '{"path": "/json/path.wav", "size": 200}\n'
        )
        state = StateFile(state_file)
        assert state.get("/plain/path.mp3")["path"] == "/plain/path.mp3"
        assert state.get("/json/path.wav")["size"] == 200

    def test_record_persists_to_disk(self, tmp_path):
        state_file = tmp_path / "state"
        state = StateFile(state_file)
        state.record("/a.mp3", size=100, parts_uploaded=3)
        with open(state_file) as f:
            lines = f.readlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["parts_uploaded"] == 3


class TestOutputWriter:
    def test_csv_streaming(self, tmp_path):
        out = tmp_path / "out.csv"
        with OutputWriter("csv", str(out)) as writer:
            writer.write({"file_path": "/a.mp3", "pillows_su_link": "https://x.com/a"})
            writer.write({"file_path": "/b.wav", "pillows_su_link": "https://x.com/b"})
        with open(out) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 2
        assert rows[0]["file_path"] == "/a.mp3"

    def test_ndjson_streaming(self, tmp_path):
        out = tmp_path / "out.ndjson"
        with OutputWriter("ndjson", str(out)) as writer:
            writer.write({"file_path": "/a.mp3", "pillows_su_link": "https://x.com/a"})
        with open(out) as f:
            line = f.readline()
            obj = json.loads(line)
        assert obj["file_path"] == "/a.mp3"

    def test_json_buffered(self, tmp_path):
        out = tmp_path / "out.json"
        with OutputWriter("json", str(out)) as writer:
            writer.write({"file_path": "/a.mp3", "pillows_su_link": "https://x.com/a"})
        with open(out) as f:
            data = json.load(f)
        assert len(data) == 1
        assert data[0]["file_path"] == "/a.mp3"

    def test_html_buffered(self, tmp_path):
        out = tmp_path / "out.html"
        with OutputWriter("html", str(out)) as writer:
            writer.write({"file_path": "/a.mp3", "pillows_su_link": "https://x.com/a"})
        content = out.read_text()
        assert "<table>" in content
        assert "/a.mp3" in content

    def test_xlsx_requires_openpyxl(self, tmp_path):
        out = tmp_path / "out.xlsx"
        with patch("main.openpyxl", None):
            writer = OutputWriter("xlsx", str(out))
            with pytest.raises(RuntimeError, match="openpyxl is required"):
                writer.write({"file_path": "/a.mp3", "pillows_su_link": "https://x.com/a"})

    def test_json_multiple_results(self, tmp_path):
        out = tmp_path / "out.json"
        with OutputWriter("json", str(out)) as writer:
            writer.write({"file_path": "/a.mp3", "pillows_su_link": "https://x.com/a"})
            writer.write({"file_path": "/b.wav", "pillows_su_link": "https://x.com/b"})
        with open(out) as f:
            data = json.load(f)
        assert len(data) == 2
        assert data[0]["file_path"] == "/a.mp3"

    def test_html_contains_links(self, tmp_path):
        out = tmp_path / "out.html"
        with OutputWriter("html", str(out)) as writer:
            writer.write({"file_path": "/a.mp3", "pillows_su_link": "https://x.com/a"})
        content = out.read_text()
        assert "<a href='https://x.com/a'>" in content

    def test_ndjson_multiple_lines(self, tmp_path):
        out = tmp_path / "out.ndjson"
        with OutputWriter("ndjson", str(out)) as writer:
            writer.write({"file_path": "/a.mp3", "pillows_su_link": "https://x.com/a"})
            writer.write({"file_path": "/b.wav", "pillows_su_link": "https://x.com/b"})
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[1])["file_path"] == "/b.wav"

    def test_csv_header_matches_fields(self, tmp_path):
        out = tmp_path / "out.csv"
        with OutputWriter("csv", str(out)) as writer:
            writer.write({"file_path": "/a.mp3", "pillows_su_link": "https://x.com/a"})
        with open(out) as f:
            header = f.readline().strip()
        assert header == "file_path,pillows_su_link"


class TestParseArgs:
    def test_defaults(self):
        args = parse_args([])
        assert args.paths == ["./downloads"]
        assert args.output is None
        assert args.api_key is None
        assert args.base_url is None
        assert args.chunk_size is None
        assert args.concurrency is None
        assert args.chunk_concurrency is None
        assert args.retries is None
        assert args.part_retries is None
        assert args.backoff is None
        assert args.timeout is None
        assert args.dry_run is False
        assert args.verbose is False
        assert args.quiet is False
        assert args.resume is False
        assert args.no_csv is False
        assert args.delete is False
        assert args.no_progress is False
        assert args.min_size is None
        assert args.max_size is None
        assert args.format is None

    def test_custom_paths(self):
        args = parse_args(["song.mp3", "album/"])
        assert args.paths == ["song.mp3", "album/"]

    def test_flags(self):
        args = parse_args(["--dry-run", "-v", "--resume", "--no-csv", "--delete", "--no-progress", "-q"])
        assert args.dry_run is True
        assert args.verbose is True
        assert args.resume is True
        assert args.no_csv is True
        assert args.delete is True
        assert args.no_progress is True
        assert args.quiet is True

    def test_options(self):
        args = parse_args(
            [
                "-o", "out.csv", "-k", "mykey", "-c", "4", "-r", "5",
                "--backoff", "3", "--timeout", "60", "--chunk-size", "1048576",
                "--min-size", "100", "--max-size", "9999", "--state-file", ".my_state",
                "--chunk-concurrency", "2", "--part-retries", "4", "--format", "json",
            ]
        )
        assert args.output == "out.csv"
        assert args.api_key == "mykey"
        assert args.concurrency == 4
        assert args.chunk_concurrency == 2
        assert args.retries == 5
        assert args.part_retries == 4
        assert args.backoff == 3
        assert args.timeout == 60
        assert args.chunk_size == 1048576
        assert args.min_size == 100
        assert args.max_size == 9999
        assert args.state_file == ".my_state"
        assert args.format == "json"

    def test_ext_filter(self):
        args = parse_args(["--ext", ".mp3", ".wav"])
        assert args.ext == [".mp3", ".wav"]

    def test_base_url(self):
        args = parse_args(["--base-url", "https://custom.api"])
        assert args.base_url == "https://custom.api"

    def test_no_progress_default(self):
        args = parse_args([])
        assert args.no_progress is False

    def test_no_progress_flag(self):
        args = parse_args(["--no-progress"])
        assert args.no_progress is True

    def test_quiet_flag(self):
        args = parse_args(["-q"])
        assert args.quiet is True

    def test_format_default(self):
        args = parse_args([])
        assert args.format is None

    def test_completions_bash(self, capsys):
        result = main(["--completions", "bash"])
        assert result == 0
        out = capsys.readouterr().out
        assert "COMPREPLY" in out
        assert "_pillows_upload" in out

    def test_completions_zsh(self, capsys):
        result = main(["--completions", "zsh"])
        assert result == 0
        out = capsys.readouterr().out
        assert "#compdef" in out

    def test_completions_fish(self, capsys):
        result = main(["--completions", "fish"])
        assert result == 0
        out = capsys.readouterr().out
        assert "complete -c pillows-upload" in out

    def test_completions_invalid_shell(self):
        with pytest.raises(SystemExit):
            main(["--completions", "unknown"])


class TestCollectFiles:
    def test_collects_files_in_dir(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello")
        (tmp_path / "b.mp3").write_bytes(b"\x00" * 100)
        result = collect_files([str(tmp_path)], None, 0, 0, False)
        assert len(result) == 2

    def test_collects_single_file(self, tmp_path):
        f = tmp_path / "song.mp3"
        f.write_bytes(b"\x00" * 50)
        result = collect_files([str(f)], None, 0, 0, False)
        assert len(result) == 1
        assert result[0] == f

    def test_extension_filter(self, tmp_path):
        (tmp_path / "a.mp3").write_bytes(b"\x00" * 10)
        (tmp_path / "b.wav").write_bytes(b"\x00" * 10)
        (tmp_path / "c.txt").write_text("hi")
        result = collect_files([str(tmp_path)], [".mp3"], 0, 0, False)
        assert len(result) == 1
        assert result[0].name == "a.mp3"

    def test_extension_without_dot(self, tmp_path):
        (tmp_path / "a.mp3").write_bytes(b"\x00" * 10)
        (tmp_path / "b.wav").write_bytes(b"\x00" * 10)
        result = collect_files([str(tmp_path)], ["wav"], 0, 0, False)
        assert len(result) == 1
        assert result[0].name == "b.wav"

    def test_min_size_filter(self, tmp_path):
        (tmp_path / "small.txt").write_text("hi")
        (tmp_path / "big.txt").write_text("x" * 100)
        result = collect_files([str(tmp_path)], None, 50, 0, False)
        assert len(result) == 1
        assert result[0].name == "big.txt"

    def test_max_size_filter(self, tmp_path):
        (tmp_path / "small.txt").write_text("hi")
        (tmp_path / "big.txt").write_text("x" * 100)
        result = collect_files([str(tmp_path)], None, 0, 50, False)
        assert len(result) == 1
        assert result[0].name == "small.txt"

    def test_nonexistent_path_skipped(self, tmp_path):
        result = collect_files([str(tmp_path / "nope")], None, 0, 0, False)
        assert result == []

    def test_directories_only_contain_files(self, tmp_path):
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "nested.txt").write_text("x")
        (tmp_path / "top.txt").write_text("y")
        result = collect_files([str(tmp_path)], None, 0, 0, False)
        assert len(result) == 2

    def test_sorted_output(self, tmp_path):
        (tmp_path / "c.txt").write_text("1")
        (tmp_path / "a.txt").write_text("2")
        (tmp_path / "b.txt").write_text("3")
        result = collect_files([str(tmp_path)], None, 0, 0, False)
        names = [f.name for f in result]
        assert names == sorted(names)

    def test_empty_dir(self, tmp_path):
        result = collect_files([str(tmp_path)], None, 0, 0, False)
        assert result == []

    def test_extension_normalization(self, tmp_path):
        (tmp_path / "a.mp3").write_bytes(b"\x00" * 10)
        (tmp_path / "b.MP3").write_bytes(b"\x00" * 10)
        result = collect_files([str(tmp_path)], ["mp3"], 0, 0, False)
        assert len(result) == 2

    def test_extension_with_dot_normalization(self, tmp_path):
        (tmp_path / "a.wav").write_bytes(b"\x00" * 10)
        result = collect_files([str(tmp_path)], [".wav"], 0, 0, False)
        assert len(result) == 1

    def test_nonexistent_path_skipped_verbose(self, tmp_path, capsys):
        result = collect_files([str(tmp_path / "nope")], None, 0, 0, True)
        assert result == []
        captured = capsys.readouterr()
        assert "not a file or directory" in captured.out


class TestState:
    def test_load_nonexistent(self, tmp_path):
        assert load_state(tmp_path / "nope") == {}

    def test_load_existing(self, tmp_path):
        sf = tmp_path / "state"
        sf.write_text("/a/file1.mp3\n/file2.wav\n")
        result = load_state(sf)
        assert result == {"/a/file1.mp3": {}, "/file2.wav": {}}

    def test_load_ignores_blank_lines(self, tmp_path):
        sf = tmp_path / "state"
        sf.write_text("/a.mp3\n\n\n/b.wav\n")
        result = load_state(sf)
        assert result == {"/a.mp3": {}, "/b.wav": {}}

    def test_save_and_load(self, tmp_path):
        sf = tmp_path / "state"
        save_state(sf, {"/b.mp3", "/a.wav"})
        result = load_state(sf)
        assert result == {"/a.wav": {}, "/b.mp3": {}}

    def test_save_is_sorted(self, tmp_path):
        sf = tmp_path / "state"
        save_state(sf, {"/c", "/a", "/b"})
        lines = sf.read_text().splitlines()
        assert lines == ["/a", "/b", "/c"]


class TestInitUpload:
    @patch("main.requests.post")
    def test_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"success": True, "message": {"id": "task123"}}
        mock_post.return_value = mock_resp

        session = MagicMock()
        session.post.return_value = mock_resp
        result = init_upload(session, "https://api.test", "file.mp3", 1024, "key1", 30)
        assert result == "task123"
        session.post.assert_called_once()

    @patch("main.requests.post")
    def test_failure(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"success": False, "message": "bad request"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        session = MagicMock()
        session.post.return_value = mock_resp
        with pytest.raises(RuntimeError, match="init failed"):
            init_upload(session, "https://api.test", "file.mp3", 1024, None, 30)


class TestUploadPart:
    @patch("main.requests.put")
    def test_success(self, mock_put):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"success": True}
        mock_put.return_value = mock_resp

        session = MagicMock()
        session.put.return_value = mock_resp
        upload_part(session, "https://api.test", "task1", "f.mp3", b"data", 1, "key1", 120)
        session.put.assert_called_once()

    @patch("main.requests.put")
    def test_failure(self, mock_put):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"success": False}
        mock_resp.text = "error"
        mock_put.return_value = mock_resp

        session = MagicMock()
        session.put.return_value = mock_resp
        with pytest.raises(RuntimeError, match="part 1 failed"):
            upload_part(session, "https://api.test", "task1", "f.mp3", b"data", 1, None, 120)


class TestFinalizeUpload:
    @patch("main.requests.get")
    def test_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"success": True, "message": {"id": "file99"}}
        mock_get.return_value = mock_resp

        session = MagicMock()
        session.get.return_value = mock_resp
        result = finalize_upload(session, "https://api.test", "task1", "key1", 300)
        assert result == "file99"

    @patch("main.requests.get")
    def test_failure(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"success": False, "message": "timeout"}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        session = MagicMock()
        session.get.return_value = mock_resp
        with pytest.raises(RuntimeError, match="done failed"):
            finalize_upload(session, "https://api.test", "task1", None, 300)


class TestUploadOne:
    def test_dry_run(self, tmp_path):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        result = upload_one(f, "https://api.test", None, 8192, 3, 2, 2, True, False, 30, progress=False)
        assert result["pillows_su_link"] == "https://pillows.su/f/DRY_RUN_test.mp3"
        assert "file_path" in result
        assert "size" in result

    @patch("main.finalize_upload")
    @patch("main.upload_part")
    @patch("main.init_upload")
    def test_single_chunk(self, mock_init, mock_part, mock_finalize, tmp_path):
        f = tmp_path / "small.mp3"
        f.write_bytes(b"\x00" * 100)
        mock_init.return_value = "task1"
        mock_finalize.return_value = "file1"

        result = upload_one(f, "https://api.test", "key", 8192, 3, 2, 2, False, False, 30, progress=False)
        assert result["pillows_su_link"] == "https://pillows.su/f/file1"
        mock_init.assert_called_once()
        mock_part.assert_called_once()
        mock_finalize.assert_called_once()

    @patch("main.finalize_upload")
    @patch("main.upload_part")
    @patch("main.init_upload")
    def test_multi_chunk(self, mock_init, mock_part, mock_finalize, tmp_path):
        f = tmp_path / "big.mp3"
        f.write_bytes(b"\x00" * 100)
        mock_init.return_value = "task1"
        mock_finalize.return_value = "file1"

        result = upload_one(f, "https://api.test", "key", 30, 3, 2, 2, False, False, 30, progress=False)
        assert result["pillows_su_link"] == "https://pillows.su/f/file1"
        assert mock_part.call_count == 4

    @patch("main.time.sleep")
    @patch("main.finalize_upload")
    @patch("main.upload_part")
    @patch("main.init_upload")
    def test_retries_on_failure(self, mock_init, mock_part, mock_finalize, mock_sleep, tmp_path):
        f = tmp_path / "retry.mp3"
        f.write_bytes(b"\x00" * 10)
        mock_init.return_value = "task1"
        mock_finalize.side_effect = [RuntimeError("timeout"), "file1"]

        result = upload_one(f, "https://api.test", "key", 8192, 3, 2, 2, False, False, 30, progress=False)
        assert result["pillows_su_link"] == "https://pillows.su/f/file1"
        assert mock_finalize.call_count == 2

    @patch("main.time.sleep")
    @patch("main.finalize_upload")
    @patch("main.upload_part")
    @patch("main.init_upload")
    def test_gives_up_after_max_retries(self, mock_init, mock_part, mock_finalize, mock_sleep, tmp_path):
        f = tmp_path / "fail.mp3"
        f.write_bytes(b"\x00" * 10)
        mock_init.return_value = "task1"
        mock_finalize.side_effect = RuntimeError("always fails")

        with pytest.raises(RuntimeError, match="Failed after 3 attempts"):
            upload_one(f, "https://api.test", "key", 8192, 3, 2, 2, False, False, 30, progress=False)
        assert mock_finalize.call_count == 3

    @patch("main.finalize_upload")
    @patch("main.upload_part")
    @patch("main.init_upload")
    def test_skips_unchanged_file(self, mock_init, mock_part, mock_finalize, tmp_path):
        f = tmp_path / "song.mp3"
        f.write_bytes(b"\x00" * 100)
        sha = compute_sha256(f)
        state = StateFile(tmp_path / "state")
        state.record(str(f.resolve()), size=100, sha256=sha, parts_uploaded=4, url="https://pillows.su/f/abc")

        result = upload_one(f, "https://api.test", "key", 8192, 3, 2, 2, False, False, 30, progress=False, state=state)
        assert result["pillows_su_link"] == "https://pillows.su/f/abc"
        mock_init.assert_not_called()

    @patch("main.finalize_upload")
    @patch("main.upload_part")
    @patch("main.init_upload")
    def test_resumes_incomplete_upload(self, mock_init, mock_part, mock_finalize, tmp_path):
        f = tmp_path / "big.mp3"
        f.write_bytes(b"\x00" * 100)
        state = StateFile(tmp_path / "state")
        state.record(str(f.resolve()), size=100, sha256=compute_sha256(f), parts_uploaded=2, url="")

        mock_init.return_value = "task1"
        mock_finalize.return_value = "file1"

        result = upload_one(f, "https://api.test", "key", 30, 3, 2, 2, False, False, 30, progress=False, state=state)
        assert result["pillows_su_link"] == "https://pillows.su/f/file1"
        assert mock_part.call_count == 2

    @patch("main.finalize_upload")
    @patch("main.upload_part")
    @patch("main.init_upload")
    def test_chunk_concurrency_flag_accepted(self, mock_init, mock_part, mock_finalize, tmp_path):
        f = tmp_path / "big.mp3"
        f.write_bytes(b"\x00" * 100)
        mock_init.return_value = "task1"
        mock_finalize.return_value = "file1"

        with patch("main.ThreadPoolExecutor") as MockPool:
            mock_pool = MagicMock()
            MockPool.return_value.__enter__ = MagicMock(return_value=mock_pool)
            MockPool.return_value.__exit__ = MagicMock(return_value=False)

            fake_futures = {}
            for pd in [(1, b"\x00" * 30), (2, b"\x00" * 30), (3, b"\x00" * 30), (4, b"\x00" * 10)]:
                fut = MagicMock()
                fut.result.return_value = (True, 0)
                fake_futures[fut] = pd

            mock_pool.submit = MagicMock(side_effect=lambda fn, pd: fake_futures.keys().__iter__().__next__())
            with patch("main.as_completed", return_value=fake_futures.keys()):
                result = upload_one(f, "https://api.test", "key", 30, 3, 2, 2, False, False, 30, progress=False, chunk_concurrency=2)
        assert result["pillows_su_link"] == "https://pillows.su/f/file1"

    @patch("main.finalize_upload")
    @patch("main.upload_part")
    @patch("main.init_upload")
    def test_upload_one_saves_state_on_success(self, mock_init, mock_part, mock_finalize, tmp_path):
        f = tmp_path / "song.mp3"
        f.write_bytes(b"\x00" * 100)
        mock_init.return_value = "task1"
        mock_finalize.return_value = "file1"
        state = StateFile(tmp_path / "state")

        result = upload_one(f, "https://api.test", "key", 8192, 3, 2, 2, False, False, 30, progress=False, state=state)
        assert result["pillows_su_link"] == "https://pillows.su/f/file1"
        entry = state.get(str(f.resolve()))
        assert entry["url"] == "https://pillows.su/f/file1"
        assert entry["size"] == 100

    @patch("main.finalize_upload")
    @patch("main.upload_part")
    @patch("main.init_upload")
    def test_hash_mismatch_does_not_skip(self, mock_init, mock_part, mock_finalize, tmp_path):
        f = tmp_path / "song.mp3"
        f.write_bytes(b"\x00" * 100)
        state = StateFile(tmp_path / "state")
        state.record(str(f.resolve()), size=999, sha256="wrong", parts_uploaded=4, url="https://old.url")

        mock_init.return_value = "task1"
        mock_finalize.return_value = "file1"

        result = upload_one(f, "https://api.test", "key", 8192, 3, 2, 2, False, False, 30, progress=False, state=state)
        assert result["pillows_su_link"] == "https://pillows.su/f/file1"
        mock_init.assert_called_once()


class TestUploadTask:
    def test_success(self, tmp_path):
        f = tmp_path / "ok.mp3"
        f.write_bytes(b"\x00" * 10)
        args = argparse.Namespace(
            base_url="https://api.test",
            api_key=None,
            chunk_size=8192,
            retries=3,
            part_retries=2,
            backoff=2,
            dry_run=True,
            verbose=False,
            timeout=30,
            no_progress=False,
            chunk_concurrency=1,
        )
        result = upload_task(f, args, set())
        assert result is not None
        assert "pillows_su_link" in result
        assert "file_path" in result

    def test_failure_returns_none(self, tmp_path):
        f = tmp_path / "bad.mp3"
        f.write_bytes(b"\x00" * 10)
        args = argparse.Namespace(
            base_url="https://api.test",
            api_key=None,
            chunk_size=8192,
            retries=1,
            part_retries=2,
            backoff=2,
            dry_run=False,
            verbose=False,
            timeout=30,
            no_progress=False,
            chunk_concurrency=1,
        )
        with patch("main.init_upload", side_effect=RuntimeError("nope")):
            result = upload_task(f, args, set())
        assert result is None


class TestMain:
    def test_no_files_returns_1(self, tmp_path):
        result = main([str(tmp_path / "nonexistent")])
        assert result == 1

    def test_dry_run_writes_csv(self, tmp_path):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        csv_out = tmp_path / "out.csv"
        result = main(
            [
                str(f),
                "--dry-run",
                "-o", str(csv_out),
                "--no-csv",
            ]
        )
        assert result == 0
        assert not csv_out.exists()

    def test_dry_run_with_csv(self, tmp_path):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        csv_out = tmp_path / "out.csv"
        result = main(
            [
                str(f),
                "--dry-run",
                "-o", str(csv_out),
            ]
        )
        assert result == 0
        assert csv_out.exists()
        with open(csv_out) as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["file_path"] == str(f.resolve())

    def test_resume_skips_uploaded(self, tmp_path):
        f1 = tmp_path / "a.mp3"
        f2 = tmp_path / "b.mp3"
        f1.write_bytes(b"\x00" * 10)
        f2.write_bytes(b"\x00" * 10)
        state = tmp_path.parent / "test_state"
        state.write_text(str(f1.resolve()) + "\n")

        csv_out = tmp_path / "out.csv"
        result = main(
            [
                str(tmp_path),
                "--dry-run",
                "--resume",
                "--state-file", str(state),
                "-o", str(csv_out),
            ]
        )
        assert result == 0
        with open(csv_out) as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["file_path"] == str(f2.resolve())

    @patch("main.Path.unlink")
    def test_delete_removes_files(self, mock_unlink, tmp_path):
        f = tmp_path / "del.mp3"
        f.write_bytes(b"\x00" * 10)
        csv_out = tmp_path / "out.csv"
        result = main(
            [
                str(f),
                "--dry-run",
                "--delete",
                "-o", str(csv_out),
            ]
        )
        assert result == 0
        mock_unlink.assert_called_once()

    def test_ext_filter(self, tmp_path):
        (tmp_path / "a.mp3").write_bytes(b"\x00" * 10)
        (tmp_path / "b.txt").write_text("hi")
        csv_out = tmp_path / "out.csv"
        result = main(
            [
                str(tmp_path),
                "--ext", ".mp3",
                "--dry-run",
                "-o", str(csv_out),
            ]
        )
        assert result == 0
        with open(csv_out) as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        assert len(rows) == 1

    def test_min_size_filter(self, tmp_path):
        (tmp_path / "small.txt").write_text("hi")
        (tmp_path / "big.txt").write_text("x" * 100)
        csv_out = tmp_path / "out.csv"
        result = main(
            [
                str(tmp_path),
                "--min-size", "50",
                "--dry-run",
                "-o", str(csv_out),
            ]
        )
        assert result == 0
        with open(csv_out) as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        assert len(rows) == 1

    def test_quiet_suppresses_output(self, tmp_path, capsys):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        main([str(f), "--dry-run", "--quiet"])
        captured = capsys.readouterr()
        assert "Found" not in captured.out
        assert "Uploading" not in captured.out

    def test_format_json(self, tmp_path):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        json_out = tmp_path / "out.json"
        result = main(
            [
                str(f),
                "--dry-run",
                "--format", "json",
                "-o", str(json_out),
            ]
        )
        assert result == 0
        with open(json_out) as fh:
            data = json.load(fh)
        assert len(data) == 1
        assert "pillows_su_link" in data[0]

    def test_format_ndjson(self, tmp_path):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        ndjson_out = tmp_path / "out.ndjson"
        result = main(
            [
                str(f),
                "--dry-run",
                "--format", "ndjson",
                "-o", str(ndjson_out),
            ]
        )
        assert result == 0
        lines = ndjson_out.read_text().strip().splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["file_path"] == str(f.resolve())

    def test_verbose_shows_timing(self, tmp_path, capsys):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        main([str(f), "--dry-run", "-v"])
        captured = capsys.readouterr()
        assert "OK" in captured.out
        assert "MB/s" in captured.out

    def test_version_flag(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["--version"])
        assert exc_info.value.code == 0

    def test_validate_args_rejects_bad_chunk_size(self):
        import argparse
        args = argparse.Namespace(chunk_size=0, concurrency=None, chunk_concurrency=None, retries=None, part_retries=None, backoff=None, timeout=None, min_size=None, max_size=None)
        with pytest.raises(ValueError, match="--chunk-size must be greater than 0"):
            validate_args(args)

    def test_validate_args_rejects_negative_concurrency(self):
        import argparse
        args = argparse.Namespace(chunk_size=None, concurrency=-1, chunk_concurrency=None, retries=None, part_retries=None, backoff=None, timeout=None, min_size=None, max_size=None)
        with pytest.raises(ValueError, match="--concurrency must be at least 1"):
            validate_args(args)

    def test_validate_args_rejects_bad_timeout(self):
        import argparse
        args = argparse.Namespace(chunk_size=None, concurrency=None, chunk_concurrency=None, retries=None, part_retries=None, backoff=None, timeout=-1, min_size=None, max_size=None)
        with pytest.raises(ValueError, match="--timeout must be greater than 0"):
            validate_args(args)

    def test_validate_args_rejects_max_lt_min(self):
        import argparse
        args = argparse.Namespace(chunk_size=None, concurrency=None, chunk_concurrency=None, retries=None, part_retries=None, backoff=None, timeout=None, min_size=100, max_size=50)
        with pytest.raises(ValueError, match="--max-size must be greater than or equal to --min-size"):
            validate_args(args)

    def test_summary_shows_total_bytes_and_speed(self, tmp_path, capsys):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 1000)
        main([str(f), "--dry-run"])
        captured = capsys.readouterr()
        assert "Uploaded: 1" in captured.out
        assert "MB" in captured.out
        assert "MB/s" in captured.out

    def test_validation_error_returns_1(self, tmp_path):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        result = main([str(f), "--chunk-size", "0"])
        assert result == 1

    def test_chunk_concurrency_flag(self, tmp_path):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        csv_out = tmp_path / "out.csv"
        result = main(
            [
                str(f),
                "--dry-run",
                "--chunk-concurrency", "2",
                "-o", str(csv_out),
            ]
        )
        assert result == 0
        assert csv_out.exists()

    def test_quiet_suppresses_delete_message(self, tmp_path, capsys):
        f = tmp_path / "del.mp3"
        f.write_bytes(b"\x00" * 10)
        main([str(f), "--dry-run", "--delete", "--quiet"])
        captured = capsys.readouterr()
        assert "Deleted" not in captured.out

    def test_format_default_creates_csv(self, tmp_path):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        csv_out = tmp_path / "out.csv"
        main([str(f), "--dry-run", "-o", str(csv_out)])
        assert csv_out.exists()

    def test_format_json_creates_json(self, tmp_path):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        json_out = tmp_path / "out.json"
        main([str(f), "--dry-run", "--format", "json", "-o", str(json_out)])
        assert json_out.exists()

    def test_no_csv_skips_output(self, tmp_path):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        csv_out = tmp_path / "out.csv"
        result = main([str(f), "--dry-run", "--no-csv", "-o", str(csv_out)])
        assert result == 0
        assert not csv_out.exists()

    def test_ext_normalization_uppercase(self, tmp_path):
        (tmp_path / "a.MP3").write_bytes(b"\x00" * 10)
        (tmp_path / "b.txt").write_text("hi")
        csv_out = tmp_path / "out.csv"
        result = main(
            [
                str(tmp_path),
                "--ext", "mp3",
                "--dry-run",
                "-o", str(csv_out),
            ]
        )
        assert result == 0
        with open(csv_out) as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        assert len(rows) == 1
        assert "a.MP3" in rows[0]["file_path"]

    def test_quiet_with_delete_no_output(self, tmp_path, capsys):
        f = tmp_path / "del.mp3"
        f.write_bytes(b"\x00" * 10)
        main([str(f), "--dry-run", "--delete", "--quiet"])
        captured = capsys.readouterr()
        assert "Uploading" not in captured.out
        assert "Deleted" not in captured.out
        assert "ERROR" not in captured.out

    def test_version_flag_exits_zero(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["--version"])
        assert exc_info.value.code == 0

    def test_part_retries_validates(self, tmp_path):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        result = main([str(f), "--part-retries", "-1"])
        assert result == 1

    def test_backoff_validates(self, tmp_path):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        result = main([str(f), "--backoff", "0"])
        assert result == 1
