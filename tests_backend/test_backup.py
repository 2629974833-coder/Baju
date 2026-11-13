import importlib
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from backend.store import Store


def backup_module():
    assert importlib.util.find_spec("backend.backup"), "verified backup module is missing"
    return importlib.import_module("backend.backup")


def workspace(root):
    store = Store(root / "workspace.sqlite3")
    original = root / "assets" / "asset1" / "original.png"
    version = root / "assets" / "asset1" / "versions" / "v1" / "processed.png"
    version.parent.mkdir(parents=True)
    original.write_bytes(b"original-fixture")
    version.write_bytes(b"processed-fixture")
    (original.parent / "original.part").write_bytes(b"partial-fixture")
    (original.parent / ".download.json").write_text('{"offset":15}')
    (root / "sessions").mkdir()
    key = Fernet.generate_key()
    (root / "sessions" / ".key").write_bytes(key)
    (root / "sessions" / "fixture.enc").write_bytes(
        Fernet(key).encrypt(b"private synthetic cookie")
    )
    store.mutate(
        lambda state: state["assets"].append(
            {
                "id": "asset1",
                "kind": "image",
                "status": "downloaded",
                "title": "test",
                "_path": "assets/asset1/original.png",
                "versions": [{"id": "v1", "_path": "assets/asset1/versions/v1/processed.png"}],
            }
        )
    )
    return store


def test_complete_snapshot_verify_restore_preserves_database_media_sessions_and_partials(tmp_path):
    module = backup_module()
    root = tmp_path / "source"
    source = workspace(root)
    output = tmp_path / "snapshot"
    created = module.create_backup(root, output)
    assert created["verified"] and created["fileCount"] >= 7
    verified = module.verify_backup(output)
    assert verified["verified"] and verified["fileCount"] == created["fileCount"]
    restored = tmp_path / "restored"
    result = module.restore_backup(output, restored)
    assert result["verified"]
    assert Store(restored / "workspace.sqlite3").read() == source.read()
    for relative in (
        "assets/asset1/original.png",
        "assets/asset1/versions/v1/processed.png",
        "assets/asset1/original.part",
        "assets/asset1/.download.json",
        "sessions/.key",
        "sessions/fixture.enc",
    ):
        assert (root / relative).read_bytes() == (restored / relative).read_bytes()
    key = (restored / "sessions/.key").read_bytes()
    assert (
        Fernet(key).decrypt((restored / "sessions/fixture.enc").read_bytes())
        == b"private synthetic cookie"
    )
    assert (restored / "workspace.sqlite3").stat().st_mode & 0o777 == 0o600
    assert restored.stat().st_mode & 0o777 == 0o700


def test_online_owner_can_backup_but_unowned_lock_cannot_bypass_running_workspace(tmp_path):
    module = backup_module()
    operations = importlib.import_module("backend.operations")
    root = tmp_path / "source"
    workspace(root)
    with operations.WorkspaceLock(root) as owner:
        with pytest.raises(operations.WorkspaceBusy):
            module.create_backup(root, tmp_path / "blocked")
        unheld = operations.WorkspaceLock(root)
        with pytest.raises(ValueError):
            module.create_backup(root, tmp_path / "bypass", workspace_lock=unheld)
        created = module.create_backup(root, root / "backups" / "snapshot1", workspace_lock=owner)
        assert created["verified"]
    second = module.create_backup(root, root / "backups" / "snapshot2")
    assert second["fileCount"] == created["fileCount"]
    listed = module.list_backups(root)
    assert len(listed) == 2
    assert all(row["verified"] is None and row["verification"] == "not-checked" for row in listed)


def test_corruption_is_rejected_before_restoring_any_target(tmp_path):
    module = backup_module()
    root = tmp_path / "source"
    workspace(root)
    output = tmp_path / "snapshot"
    module.create_backup(root, output)
    (output / "assets/asset1/original.png").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="校验"):
        module.verify_backup(output)
    target = tmp_path / "restored"
    with pytest.raises(ValueError):
        module.restore_backup(output, target)
    assert not target.exists()


@pytest.mark.parametrize("path", ["../escape", "/tmp/escape", "assets/../escape", "assets\\escape"])
def test_manifest_paths_cannot_escape_backup_or_restore(tmp_path, path):
    module = backup_module()
    root = tmp_path / "source"
    workspace(root)
    output = tmp_path / "snapshot"
    module.create_backup(root, output)
    manifest_path = output / "manifest.json"
    data = json.loads(manifest_path.read_text())
    data["files"][0]["path"] = path
    manifest_path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        module.verify_backup(output)


def test_backup_and_restore_never_overwrite_existing_data_or_follow_symlinks(tmp_path):
    module = backup_module()
    root = tmp_path / "source"
    workspace(root)
    output = tmp_path / "snapshot"
    module.create_backup(root, output)
    with pytest.raises(ValueError):
        module.create_backup(root, output)
    with pytest.raises(ValueError):
        module.create_backup(root, root / "assets" / "backup")
    target = tmp_path / "restore"
    target.mkdir()
    (target / "keep").write_text("user data")
    with pytest.raises(ValueError):
        module.restore_backup(output, target)
    assert (target / "keep").read_text() == "user data"
    (root / "assets" / "unsafe").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="符号链接"):
        module.create_backup(root, tmp_path / "unsafe-snapshot")


def test_missing_referenced_file_is_not_a_complete_backup(tmp_path):
    module = backup_module()
    root = tmp_path / "source"
    workspace(root)
    (root / "assets/asset1/versions/v1/processed.png").unlink()
    with pytest.raises(ValueError, match="引用"):
        module.create_backup(root, tmp_path / "snapshot")


def test_missing_session_key_or_corrupt_version_schema_cannot_be_backed_up(tmp_path):
    module = backup_module()
    root = tmp_path / "source"
    store = workspace(root)
    (root / "sessions/.key").unlink()
    with pytest.raises(ValueError, match="密钥"):
        module.create_backup(root, tmp_path / "missing-key")
    (root / "sessions/.key").write_bytes(Fernet.generate_key())
    store.mutate(lambda state: state["assets"][0].update(versions=None))
    with pytest.raises(ValueError, match="版本"):
        module.create_backup(root, tmp_path / "bad-version")


def test_restore_to_empty_directory_is_atomic_and_running_target_is_not_replaced(tmp_path):
    module = backup_module()
    operations = importlib.import_module("backend.operations")
    root = tmp_path / "source"
    workspace(root)
    output = tmp_path / "snapshot"
    module.create_backup(root, output)
    target = tmp_path / "restored"
    target.mkdir()
    with operations.WorkspaceLock(target):
        with pytest.raises(ValueError):
            module.restore_backup(output, target)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert module.restore_backup(output, empty)["restored"]
    assert (empty / "workspace.sqlite3").is_file()


def test_backup_inventory_does_not_mark_a_mutated_backup_as_verified(tmp_path):
    module = backup_module()
    root = tmp_path / "source"
    workspace(root)
    output = root / "backups" / "one"
    module.create_backup(root, output)
    (output / "assets/asset1/original.png").write_bytes(b"later change")
    assert module.list_backups(root)[0]["verified"] is None
    with pytest.raises(ValueError):
        module.verify_backup(output)


def test_session_key_must_match_the_encrypted_sessions(tmp_path):
    module = backup_module()
    root = tmp_path / "source"
    workspace(root)
    (root / "sessions/.key").write_bytes(Fernet.generate_key())
    with pytest.raises(ValueError, match="会话"):
        module.create_backup(root, tmp_path / "snapshot")


def test_backup_and_restore_respect_target_storage_headroom(tmp_path):
    module = backup_module()
    operations = importlib.import_module("backend.operations")
    root = tmp_path / "source"
    workspace(root)
    low = operations.StorageGuard(
        tmp_path,
        1000,
        disk_usage=lambda _: SimpleNamespace(total=2000, used=1001, free=999),
    )
    output = tmp_path / "too-full"
    with pytest.raises(operations.StoragePressure):
        module.create_backup(root, output, storage_guard=low)
    assert not output.exists()
    snapshot = tmp_path / "snapshot"
    module.create_backup(root, snapshot)
    target = tmp_path / "restore-too-full"
    with pytest.raises(operations.StoragePressure):
        module.restore_backup(snapshot, target, storage_guard=low)
    assert not target.exists()


def test_low_storage_during_copy_discards_only_the_incomplete_backup(tmp_path):
    module = backup_module()
    operations = importlib.import_module("backend.operations")
    root = tmp_path / "source"
    store = workspace(root)
    large = root / "assets/asset1/large.part"
    large.write_bytes(b"x" * (3 * 1024**2))
    calls = []

    def free_space(_):
        calls.append(True)
        free = 100 * 1024**2 if len(calls) < 6 else 0
        return SimpleNamespace(total=200 * 1024**2, used=200 * 1024**2 - free, free=free)

    guard = operations.StorageGuard(tmp_path, 1000, disk_usage=free_space)
    output = tmp_path / "incomplete"
    with pytest.raises(operations.StoragePressure):
        module.create_backup(root, output, storage_guard=guard)
    assert not output.exists()
    assert not list(tmp_path.glob(".framekeep-backup-*"))
    assert large.stat().st_size == 3 * 1024**2 and store.read()["assets"][0]["id"] == "asset1"
