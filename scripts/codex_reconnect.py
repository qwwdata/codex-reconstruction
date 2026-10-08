#!/usr/bin/env python3
"""Local Windows Codex proxy repair. Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
import copy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid


class RepairError(Exception):
    """An actionable failure, without echoing configuration or log contents."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_config(data: bytes) -> dict:
    try:
        return tomllib.loads(data.decode("utf-8-sig"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        raise RepairError("Config must be valid UTF-8 TOML; no changes made.") from None


def visible_lines(lines: list[str]):
    """Ignore fake assignments/table headers inside TOML multiline strings."""
    multiline = None
    for index, line in enumerate(lines):
        visible = multiline is None
        pos = 0
        while pos < len(line):
            if multiline:
                if line.startswith(multiline, pos):
                    pos += 3
                    multiline = None
                elif multiline == '"""' and line[pos] == "\\":
                    pos += 2
                else:
                    pos += 1
            elif line[pos] == "#":
                break
            elif line.startswith('"""', pos) or line.startswith("'''", pos):
                multiline = line[pos:pos + 3]
                pos += 3
            elif line[pos] in "\"'":
                quote = line[pos]
                pos += 1
                while pos < len(line):
                    if quote == '"' and line[pos] == "\\":
                        pos += 2
                    elif line[pos] == quote:
                        pos += 1
                        break
                    else:
                        pos += 1
            else:
                pos += 1
        if visible:
            yield index, line


def leaf_path(value: dict) -> tuple[str, ...]:
    path = []
    while isinstance(value, dict) and len(value) == 1:
        key, value = next(iter(value.items()))
        path.append(key)
    return tuple(path)


def patch_system_proxy(original: bytes) -> bytes:
    """Change exactly one semantic field; reject ambiguous layouts."""
    before = parse_config(original)
    features = before.get("features", {})
    if not isinstance(features, dict):
        raise RepairError("features must be a TOML table.")
    if "respect_system_proxy" in features and type(features["respect_system_proxy"]) is not bool:
        raise RepairError("respect_system_proxy must be a boolean.")
    if features.get("respect_system_proxy") is True:
        return original
    text = original.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    current = ()
    first_table = len(lines)
    feature_table = None
    target_line = None
    target_match = None
    for index, line in visible_lines(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            first_table = min(first_table, index)
            if stripped.startswith("[["):
                current = ("<array-table>",)
                continue
            try:
                current = leaf_path(tomllib.loads(stripped + "\n__probe__ = 0"))[:-1]
            except tomllib.TOMLDecodeError:
                raise RepairError("Unsupported table layout; use the manual instructions.") from None
            if current == ("features",):
                feature_table = index
            continue
        match = re.match(r"^(\s*[^=]+?\s*=\s*)(true|false)(\s*(?:#.*)?)(\r?\n)?$", line)
        if not match:
            continue
        try:
            key = match.group(1).split("=", 1)[0].strip()
            assignment = leaf_path(tomllib.loads(key + " = 0"))
        except tomllib.TOMLDecodeError:
            continue
        if current + assignment == ("features", "respect_system_proxy"):
            target_line, target_match = index, match
    if target_line is not None:
        lines[target_line] = target_match.group(1) + "true" + target_match.group(3) + (target_match.group(4) or "")
    elif feature_table is not None:
        if not lines[feature_table].endswith(("\n", "\r")):
            lines[feature_table] += newline
        lines.insert(feature_table + 1, "respect_system_proxy = true" + newline)
    elif "features" not in before:
        suffix = "" if not text or text.endswith("\n") else newline
        lines.append(suffix + newline + "[features]" + newline + "respect_system_proxy = true" + newline)
    else:
        # Extend an implicit table defined by root dotted keys. Inline tables
        # cannot be extended; semantic validation below rejects that case.
        if first_table and not lines[first_table - 1].endswith("\n"):
            lines[first_table - 1] += newline
        lines.insert(first_table, "features.respect_system_proxy = true" + newline)
    candidate = ("\ufeff" if original.startswith(b"\xef\xbb\xbf") else "") + "".join(lines)
    result = candidate.encode("utf-8")
    expected = copy.deepcopy(before)
    expected.setdefault("features", {})["respect_system_proxy"] = True
    try:
        valid = parse_config(result) == expected
    except RepairError:
        valid = False
    if not valid:
        raise RepairError("Cannot safely edit this TOML layout. Use the manual instructions; no changes made.")
    return result


def run_codex(executable: str, arguments: list[str]) -> str:
    try:
        process = subprocess.run([executable, *arguments], capture_output=True,
                                 encoding="utf-8", errors="replace", timeout=20,
                                 creationflags=0x08000000 if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired):
        raise RepairError("Cannot run Codex. Pass --codex with the desktop app's actual codex.exe path.") from None
    if process.returncode:
        raise RepairError("Codex command failed. Check the executable/config locally; raw output is hidden.")
    return process.stdout


def feature_info(output: str) -> dict:
    for line in output.splitlines():
        match = re.match(r"^respect_system_proxy\s+(.+?)\s+(true|false)\s*$", line.strip())
        if match:
            return {"available": match.group(1).strip() != "removed",
                    "stage": match.group(1).strip(), "enabled": match.group(2) == "true"}
    return {"available": False, "stage": "not listed", "enabled": None}


def windows_proxy() -> dict:
    if os.name != "nt":
        return {"platform": "non-Windows", "enabled": False, "static_proxy": False, "pac": False}
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            def read(name, default):
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    return default
            # Report presence, not addresses, credentials or PAC URLs.
            enabled = bool(read("ProxyEnable", 0))
            static = bool(str(read("ProxyServer", "")).strip())
            pac = bool(str(read("AutoConfigURL", "")).strip())
            return {"platform": "Windows", "enabled": enabled, "static_proxy": static, "pac": pac}
    except OSError:
        raise RepairError("Cannot read the current user's Windows proxy settings.") from None


def require_system_proxy(feature: dict, proxy: dict):
    if not feature["available"]:
        raise RepairError("This Codex executable does not support respect_system_proxy; no changes made.")
    if proxy.get("platform") != "Windows" or not proxy["enabled"] or not proxy["static_proxy"]:
        raise RepairError("This automatic repair requires an enabled Windows static system proxy. PAC-only/direct networks need separate diagnosis.")


def config_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def read_config(path: Path) -> bytes:
    if not path.is_file():
        raise RepairError("Config file not found. Pass --config for the actual user config.toml.")
    return path.read_bytes()


def atomic_write(path: Path, data: bytes):
    fd, temporary = tempfile.mkstemp(prefix=".codex-reconnect-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def apply_patch(path: Path, original: bytes, candidate: bytes, *, dry_run: bool) -> dict:
    if candidate == original:
        return {"status": "already_enabled", "changed": False}
    if dry_run:
        return {"status": "dry_run", "changed": False, "planned_change": "features.respect_system_proxy = true"}
    if path.read_bytes() != original:
        raise RepairError("Config changed during diagnosis; run again after closing Codex/settings editors.")
    backup_dir = path.parent / "reconnect-backups"
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    backup = backup_dir / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".toml.bak")
    # Backups may contain secrets. Store locally; never send them to GitHub.
    with backup.open("xb") as stream:
        stream.write(original)
    shutil.copymode(path, backup)
    metadata = {"config_path": str(path.resolve()), "original_sha256": digest(original),
                "patched_sha256": digest(candidate), "operation": "respect_system_proxy"}
    manifest = Path(str(backup) + ".json")
    manifest.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    if path.read_bytes() != original:
        raise RepairError("Config changed before writing; it was left untouched. A local backup was retained.")
    atomic_write(path, candidate)
    if path.read_bytes() != candidate:
        raise RepairError("Config verification failed. Inspect the local backup before continuing.")
    return {"status": "applied", "changed": True, "backup": str(backup),
            "next_step": "Fully quit Codex, including its background process, then reopen and test the affected chat."}


def restore_backup(path: Path, backup: Path, *, dry_run: bool) -> dict:
    try:
        metadata = json.loads(Path(str(backup) + ".json").read_text(encoding="utf-8"))
        original = backup.read_bytes()
    except (OSError, ValueError):
        raise RepairError("Backup or its .json manifest cannot be read.") from None
    if not isinstance(metadata, dict) or metadata.get("operation") != "respect_system_proxy" or metadata.get("config_path") != str(path.resolve()):
        raise RepairError("Backup belongs to a different operation/config file.")
    if digest(original) != metadata.get("original_sha256"):
        raise RepairError("Backup checksum mismatch; no changes made.")
    parse_config(original)
    current = read_config(path)
    if current == original:
        return {"status": "already_restored", "changed": False}
    if digest(current) != metadata.get("patched_sha256"):
        raise RepairError("Config changed since the repair. Restore refused to preserve later edits; merge the backup manually.")
    if dry_run:
        return {"status": "restore_dry_run", "changed": False}
    if path.read_bytes() != current:
        raise RepairError("Config changed before restore; no changes made.")
    atomic_write(path, original)
    return {"status": "restored", "changed": True, "next_step": "Fully quit and reopen Codex."}


def summarize_logs(database: Path, *, minutes: int, thread_id: str | None) -> dict:
    if not database.is_file():
        raise RepairError("Log database not found. Pass --logs for the actual logs_2.sqlite.")
    cutoff = int(time.time()) - minutes * 60
    with tempfile.TemporaryDirectory(prefix="codex-reconnect-logs-") as directory:
        snapshot = Path(directory) / database.name
        # Best-effort live snapshot, including WAL. Never change the original DB.
        shutil.copyfile(database, snapshot)
        wal = Path(str(database) + "-wal")
        if wal.exists():
            try:
                shutil.copyfile(wal, Path(str(snapshot) + "-wal"))
            except FileNotFoundError:
                pass
        try:
            with closing(sqlite3.connect(snapshot)) as connection:
                query = "SELECT target, feedback_log_body FROM logs WHERE ts >= ?"
                parameters = [cutoff]
                if thread_id:
                    query += " AND thread_id = ?"
                    parameters.append(thread_id)
                query += " ORDER BY ts DESC, id DESC LIMIT 10001"
                rows = connection.execute(query, parameters).fetchall()
        except sqlite3.Error:
            raise RepairError("Unsupported log schema or inconsistent live snapshot. Fully quit Codex and retry.") from None
    counts = {"websocket_attempts": 0, "stream_retry_events": 0, "http_fallbacks": 0,
              "timeout_events": 0, "tls_unexpected_eof_events": 0,
              "http_transport_events": 0, "websocket_transport_events": 0}
    for target, body in rows[:10000]:
        target, body = str(target or ""), str(body or "")
        low = body.lower()
        if "responses_websocket" in target and "connecting to websocket" in low:
            counts["websocket_attempts"] += 1
        if ("retrying sampling request" in low or
                ("stream disconnected" in low and re.search(r"retrying|retry\s+\d+", low))):
            counts["stream_retry_events"] += 1
        if "falling back to http" in low:
            counts["http_fallbacks"] += 1
        if "timed out" in low or "timeout" in low:
            counts["timeout_events"] += 1
        if "close_notify" in low or "unexpected eof" in low:
            counts["tls_unexpected_eof_events"] += 1
        if 'transport="responses_http"' in body:
            counts["http_transport_events"] += 1
        if 'transport="responses_websocket"' in body:
            counts["websocket_transport_events"] += 1
    return {"scope": "selected chat" if thread_id else "all chats/processes in this log database",
            "window_minutes": minutes, "scanned_events": min(len(rows), 10000),
            "truncated": len(rows) > 10000, "counts": counts,
            "limitations": "Event counts, not unique failures or proof of success. Live snapshot is best effort; verify an actual completed reply."}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["diagnose", "apply-system-proxy", "restore", "check-logs"], nargs="?", default="diagnose")
    parser.add_argument("--config", type=Path, default=config_home() / "config.toml")
    parser.add_argument("--codex", default=shutil.which("codex"), help="Actual app-bundled codex.exe (or matching CLI)")
    parser.add_argument("--dry-run", action="store_true", help="Do not write config or backups")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--logs", type=Path, default=config_home() / "logs_2.sqlite")
    parser.add_argument("--minutes", type=int, choices=range(1, 1441), metavar="1..1440", default=10)
    parser.add_argument("--thread-id", help="Filter to the affected chat; never print its ID")
    args = parser.parse_args(argv)
    try:
        path = args.config.expanduser().resolve()
        if args.command == "check-logs":
            report = summarize_logs(args.logs.expanduser(), minutes=args.minutes, thread_id=args.thread_id)
        elif args.command == "restore":
            if not args.backup:
                raise RepairError("restore requires --backup with the .toml.bak path printed by apply.")
            report = restore_backup(path, args.backup.expanduser(), dry_run=args.dry_run)
        else:
            original = read_config(path)
            data = parse_config(original)
            if not isinstance(data.get("features", {}), dict):
                raise RepairError("features must be a TOML table.")
            if not args.codex:
                raise RepairError("Codex not found on PATH. Pass --codex with the desktop app's actual codex.exe path.")
            version = run_codex(args.codex, ["--version"]).strip()
            # Report only recognizable version syntax, never arbitrary executable output.
            version_match = re.search(r"codex-cli\s+[\w.+-]+", version)
            feature = feature_info(run_codex(args.codex, ["features", "list"]))
            proxy = windows_proxy()
            if args.command == "diagnose":
                report = {"codex_version": version_match.group(0) if version_match else "unrecognized",
                          "respect_system_proxy": feature, "windows_proxy": proxy,
                          "proxy_environment_variables_present": [key for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy") if os.environ.get(key)],
                          "config_flag_enabled": data.get("features", {}).get("respect_system_proxy") is True,
                          "root_provider": "built-in openai" if data.get("model_provider", "openai") == "openai" else "custom/other provider",
                          "selected_profile_present": bool(data.get("profile")),
                          "note": "Local settings only; no model request or connectivity test was sent."}
            else:
                require_system_proxy(feature, proxy)
                report = apply_patch(path, original, patch_system_proxy(original), dry_run=args.dry_run)
                report["feature_stage"] = feature["stage"]
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (RepairError, OSError) as error:
        message = str(error) if isinstance(error, RepairError) else "Local file access failed. Check permissions; no raw file contents were printed."
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

