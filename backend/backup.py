"""Verified private directory snapshots and offline atomic restore.

The CLI obtains the same workspace lease as the service. An already-quiescent
service may pass its held lease to create_backup; it must block all new writes.
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from cryptography.fernet import Fernet, InvalidToken

from backend.operations import StorageGuard, WorkspaceLock

FORMAT = "framekeep-workspace-backup"
MAX_MANIFEST_BYTES = 32 * 1024**2
MAX_FILES = 100_000
_EXCLUDED = {"backups", WorkspaceLock.filename, "workspace.sqlite3-wal", "workspace.sqlite3-shm"}


def _relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("备份包含不安全的文件路径")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("备份包含不安全的文件路径") from None
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {".", ".."} for part in path.parts)
        or path.as_posix() != value
        or any(ord(char) < 32 for char in value)
        or not path.parts
        or path.parts[0] in _EXCLUDED
        or value == "manifest.json"
    ):
        raise ValueError("备份包含不安全的文件路径")
    return value


def _files(root, *, source=False):
    result = []
    for current, directories, names in os.walk(root, followlinks=False):
        parent = Path(current)
        if parent == root and source:
            directories[:] = [name for name in directories if name not in _EXCLUDED]
            names = [name for name in names if name not in _EXCLUDED]
        for name in directories:
            if (parent / name).is_symlink():
                raise ValueError("备份不接受符号链接目录")
        for name in names:
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if relative == "manifest.json" and not source:
                continue
            _relative_path(relative)
            if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError("备份不接受符号链接或特殊文件")
            result.append((relative, path))
            if len(result) > MAX_FILES:
                raise ValueError("备份文件数量超过本版本上限")
    return sorted(result)


def _read_database(path):
    try:
        with contextlib.closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("备份数据库校验失败")
            row = connection.execute("SELECT data FROM workspace WHERE id=1").fetchone()
            state = json.loads(row[0]) if row else None
        if (
            not isinstance(state, dict)
            or state.get("mode") != "real"
            or state.get("version") != 1
            or any(
                not isinstance(state.get(key), list) for key in ("assets", "jobs", "scans", "sites")
            )
            or not isinstance(state.get("settings"), dict)
        ):
            raise ValueError("备份数据库格式不受支持")
        return state
    except (sqlite3.Error, json.JSONDecodeError, TypeError, UnicodeError):
        raise ValueError("备份数据库校验失败") from None


def _check_references(state, entries):
    by_path = {item["path"]: item for item in entries}
    for asset in state["assets"]:
        if not isinstance(asset, dict):
            raise ValueError("备份素材记录无效")
        versions = asset.get("versions", [])
        if not isinstance(versions, list):
            raise ValueError("备份版本记录无效")
        records = [asset, *versions]
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("备份版本记录无效")
            relative = record.get("_path")
            if not relative:
                continue
            relative = _relative_path(relative)
            if not relative.startswith("assets/") or relative not in by_path:
                raise ValueError("备份缺少数据库引用的原件或处理版本")
            digest = record.get("sha256")
            if digest and digest != by_path[relative]["sha256"]:
                raise ValueError("备份文件与数据库引用的哈希校验不符")
    session_files = [
        name for name in by_path if name.startswith("sessions/") and name.endswith(".enc")
    ]
    if session_files and "sessions/.key" not in by_path:
        raise ValueError("备份缺少浏览器会话密钥")


def _check_sessions(folder, entries):
    session_files = [
        entry["path"]
        for entry in entries
        if entry["path"].startswith("sessions/") and entry["path"].endswith(".enc")
    ]
    if not session_files:
        return
    try:
        cipher = Fernet((folder / "sessions/.key").read_bytes())
        for relative in session_files:
            cipher.decrypt((folder / relative).read_bytes())
    except (ValueError, InvalidToken, OSError):
        raise ValueError("备份浏览器会话与密钥不匹配或文件损坏") from None


def _copy_file(source, target, storage_guard):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    digest, size = hashlib.sha256(), 0
    with os.fdopen(descriptor, "rb") as incoming:
        before = os.fstat(incoming.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("备份不接受特殊文件")
        with target.open("xb") as outgoing:
            os.fchmod(outgoing.fileno(), 0o600)
            while chunk := incoming.read(1024 * 1024):
                storage_guard.check(len(chunk))
                outgoing.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        after = os.fstat(incoming.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or size != after.st_size:
            raise ValueError("备份期间文件发生变化，请停止写入后重试")
    return {"size": size, "sha256": digest.hexdigest()}


def _hash_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("备份不接受特殊文件")
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _load_manifest(folder):
    path = folder / "manifest.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("找不到有效的备份清单")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("version") != 1:
            raise ValueError("备份格式不受支持")
        datetime.fromisoformat(data["createdAt"])
        entries = data["files"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES:
            raise ValueError("备份清单文件数量无效")
        paths = set()
        for entry in entries:
            relative = _relative_path(entry["path"])
            if relative in paths or type(entry["size"]) is not int or entry["size"] < 0:
                raise ValueError("备份清单文件记录无效")
            paths.add(relative)
            if not isinstance(entry["sha256"], str) or not re.fullmatch(
                r"[a-f0-9]{64}", entry["sha256"]
            ):
                raise ValueError("备份清单哈希无效")
        if "workspace.sqlite3" not in paths:
            raise ValueError("备份清单缺少数据库")
        return data
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError):
        raise ValueError("备份清单损坏或格式无效") from None


def _metadata(folder, manifest, verified):
    return {
        "id": folder.name,
        "createdAt": manifest["createdAt"],
        "fileCount": len(manifest["files"]),
        "totalBytes": sum(item["size"] for item in manifest["files"]),
        "verified": verified,
        "verification": "verified" if verified else "not-checked",
    }


def verify_backup(backup_dir):
    folder = Path(backup_dir)
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("备份目录无效")
    folder = folder.resolve()
    try:
        manifest = _load_manifest(folder)
        actual = dict(_files(folder))
        expected = {item["path"]: item for item in manifest["files"]}
        if set(actual) != set(expected):
            raise ValueError("备份文件清单校验失败，有文件缺失或额外文件")
        for relative, path in actual.items():
            entry = expected[relative]
            if path.stat().st_size != entry["size"] or _hash_file(path) != entry["sha256"]:
                raise ValueError("备份文件大小或哈希校验失败")
        _check_references(_read_database(folder / "workspace.sqlite3"), manifest["files"])
        _check_sessions(folder, manifest["files"])
        return _metadata(folder, manifest, True)
    except OSError:
        raise ValueError("无法读取备份文件，请检查权限和磁盘状态") from None


def _target_guard(parent, guard, min_free_bytes):
    if guard is not None and guard.data_dir.stat().st_dev == parent.stat().st_dev:
        return guard
    reserve = guard.min_free_bytes if guard is not None else min_free_bytes
    return StorageGuard(parent, min_free_bytes=reserve)


def create_backup(
    data_dir, output_dir, *, workspace_lock=None, storage_guard=None, min_free_bytes=512 * 1024**2
):
    root = Path(data_dir).resolve()
    requested = Path(output_dir).absolute()
    output = requested.resolve()
    if not (root / "workspace.sqlite3").is_file():
        raise ValueError("工作区数据库不存在")
    if requested.is_relative_to(root) and not output.is_relative_to(root):
        raise ValueError("备份目标不能通过符号链接离开工作区")
    if output == root or root.is_relative_to(output):
        raise ValueError("备份目录不能覆盖工作区")
    if output.is_relative_to(root) and (
        output.relative_to(root).parts[0] != "backups" or len(output.relative_to(root).parts) < 2
    ):
        raise ValueError("工作区内的备份必须位于 backups 子目录")
    if output.exists() or requested.is_symlink():
        raise ValueError("备份目标已存在，请选择新目录")
    if workspace_lock is not None:
        if (
            not isinstance(workspace_lock, WorkspaceLock)
            or not workspace_lock.held
            or workspace_lock.data_dir != root
        ):
            raise ValueError("在线备份必须使用本工作区当前持有的锁")
        lease = contextlib.nullcontext(workspace_lock)
    else:
        lease = WorkspaceLock(root)
    with lease:
        files = _files(root, source=True)
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        guard = _target_guard(output.parent, storage_guard, min_free_bytes)
        wal = root / "workspace.sqlite3-wal"
        required = sum(path.stat().st_size for _, path in files)
        if wal.is_file():
            required += wal.stat().st_size
        guard.check(required)
        output.mkdir(mode=0o700)
        published = False
        try:
            with tempfile.TemporaryDirectory(
                prefix=".framekeep-backup-", dir=output.parent
            ) as temporary:
                stage = Path(temporary)
                snapshot = stage / "workspace.sqlite3"
                with contextlib.closing(
                    sqlite3.connect((root / "workspace.sqlite3").as_uri() + "?mode=ro", uri=True)
                ) as source:
                    with contextlib.closing(sqlite3.connect(snapshot)) as destination:
                        page_size = source.execute("PRAGMA page_size").fetchone()[0]
                        source.backup(
                            destination,
                            pages=64,
                            progress=lambda _status, remaining, _total: guard.check(
                                remaining * page_size
                            ),
                        )
                        destination.execute("PRAGMA journal_mode=DELETE")
                snapshot.chmod(0o600)
                entries = [
                    {
                        "path": "workspace.sqlite3",
                        "size": snapshot.stat().st_size,
                        "sha256": _hash_file(snapshot),
                    }
                ]
                for relative, path in files:
                    if relative != "workspace.sqlite3":
                        entries.append(
                            {"path": relative, **_copy_file(path, stage / relative, guard)}
                        )
                _check_references(_read_database(snapshot), entries)
                manifest = {
                    "format": FORMAT,
                    "version": 1,
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                    "sourceRoot": str(root),
                    "files": sorted(entries, key=lambda entry: entry["path"]),
                }
                manifest_path = stage / "manifest.json"
                serialized = json.dumps(manifest, ensure_ascii=True, indent=2)
                if len(serialized) > MAX_MANIFEST_BYTES:
                    raise ValueError("备份清单大小超过本版本上限")
                guard.check(len(serialized))
                with manifest_path.open("x", encoding="utf-8") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(serialized)
                    stream.flush()
                    os.fsync(stream.fileno())
                verify_backup(stage)
                os.rename(stage, output)
                published = True
                result = _metadata(output, manifest, True)
                if output.is_relative_to(root):
                    result["path"] = output.relative_to(root).as_posix()
                return result
        except OSError:
            raise ValueError("备份写入失败，请检查剩余空间、权限和目标目录") from None
        finally:
            if not published:
                with contextlib.suppress(OSError):
                    output.rmdir()


def list_backups(data_dir):
    root = Path(data_dir).resolve()
    folder = root / "backups"
    if folder.is_symlink() or not folder.is_dir():
        return []
    result = []
    for child in folder.iterdir():
        if child.is_symlink() or not child.is_dir() or child.name.startswith("."):
            continue
        try:
            manifest = _load_manifest(child)
            result.append(
                {**_metadata(child, manifest, None), "path": child.relative_to(root).as_posix()}
            )
        except (ValueError, OSError):
            continue
    return sorted(result, key=lambda row: row["createdAt"], reverse=True)


def restore_backup(backup_dir, data_dir, *, storage_guard=None, min_free_bytes=512 * 1024**2):
    backup = Path(backup_dir).resolve()
    requested = Path(data_dir).absolute()
    target = requested.resolve()
    verify_backup(backup)
    manifest = _load_manifest(backup)
    original = (
        Path(manifest["sourceRoot"]).resolve()
        if isinstance(manifest.get("sourceRoot"), str)
        else None
    )
    if (
        requested.is_symlink()
        or target.is_relative_to(backup)
        or backup.is_relative_to(target)
        or (original is not None and target.is_relative_to(original))
        or (target.exists() and (not target.is_dir() or any(target.iterdir())))
    ):
        raise ValueError("恢复必须使用备份及原工作区之外的新空目录")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    guard = _target_guard(target.parent, storage_guard, min_free_bytes)
    guard.check(sum(entry["size"] for entry in manifest["files"]))
    try:
        with tempfile.TemporaryDirectory(
            prefix=".framekeep-restore-", dir=target.parent
        ) as temporary:
            stage = Path(temporary)
            with WorkspaceLock(stage):
                for entry in manifest["files"]:
                    copied = _copy_file(backup / entry["path"], stage / entry["path"], guard)
                    if copied != {key: entry[key] for key in ("size", "sha256")}:
                        raise ValueError("恢复复制期间备份校验失败")
                _check_references(_read_database(stage / "workspace.sqlite3"), manifest["files"])
                _check_sessions(stage, manifest["files"])
                # POSIX rename replaces only an empty directory. A running service's
                # persistent lock file makes the target non-empty, closing that race.
                os.rename(stage, target)
            return {**_metadata(backup, manifest, True), "restored": True}
    except OSError:
        raise ValueError("恢复写入失败；目标必须保持为空且有足够空间") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Framekeep 私有完整备份与离线恢复")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--data-dir", required=True)
    create.add_argument("--output", required=True)
    create.add_argument(
        "--min-free-bytes",
        type=int,
        default=int(os.getenv("FRAMEKEEP_MIN_FREE_BYTES", 512 * 1024**2)),
    )
    verify = commands.add_parser("verify")
    verify.add_argument("--backup", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--data-dir", required=True)
    restore.add_argument(
        "--min-free-bytes",
        type=int,
        default=int(os.getenv("FRAMEKEEP_MIN_FREE_BYTES", 512 * 1024**2)),
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            result = create_backup(args.data_dir, args.output, min_free_bytes=args.min_free_bytes)
        elif args.command == "verify":
            result = verify_backup(args.backup)
        else:
            result = restore_backup(args.backup, args.data_dir, min_free_bytes=args.min_free_bytes)
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
