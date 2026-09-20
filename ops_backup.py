"""Backup / restore for the trajectory database and knowledge index.

SQLite and the JSON index are both small; a ``.backup`` copy plus a tar archive
is enough, and the last N archives are kept. A restore drill is documented in
``docs/runbook.md`` — a backup that has never been restored is not a backup.
"""

from __future__ import annotations

import shutil
import sqlite3
import tarfile
import time
from pathlib import Path

import rdconfig

KEEP = 7


def backup(settings: rdconfig.Settings | None = None, keep: int = KEEP) -> int:
    settings = settings or rdconfig.Settings.from_env()
    data_dir = settings.resolve(settings.data_dir)
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    created: list[Path] = []

    db_path = data_dir / "trajectories.sqlite3"
    if db_path.exists():
        target = backup_dir / f"trajectories-{stamp}.sqlite3"
        source = sqlite3.connect(db_path)
        destination = sqlite3.connect(target)
        with destination:
            source.backup(destination)
        source.close()
        destination.close()
        created.append(target)

    index = settings.resolve(settings.kb_index)
    if index.exists():
        target = backup_dir / f"kb-index-{stamp}.tar.gz"
        with tarfile.open(target, "w:gz") as tar:
            tar.add(index, arcname=index.name)
        created.append(target)

    if not created:
        print("没有可备份的内容（轨迹库与索引都不存在）")
        return 0

    for path in created:
        print(f"已备份 {path} ({path.stat().st_size} bytes)")

    archives = sorted(backup_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in archives[keep:]:
        stale.unlink()
        print(f"已清理旧备份 {stale.name}")
    return 0


def restore(archive: Path | str, settings: rdconfig.Settings | None = None) -> int:
    settings = settings or rdconfig.Settings.from_env()
    archive = Path(archive)
    data_dir = settings.resolve(settings.data_dir)
    if archive.suffix == ".sqlite3":
        target = data_dir / "trajectories.sqlite3"
        shutil.copy2(archive, target)
        print(f"已恢复 {target}")
        return 0
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(settings.resolve(settings.kb_index).parent)
    print(f"已恢复索引目录: {settings.resolve(settings.kb_index).parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(backup())
