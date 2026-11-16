"""Index disc directory metadata in SQLite without reading file contents."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from archive import ensure_explorer_shortcut, initialize, open_database


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = BASE_DIR / "data" / "catalog.sqlite3"
DISC_NAME = re.compile(r"BD[- ]?([0-9]{1,2})", re.IGNORECASE)


@dataclass
class Entry:
    parts: tuple[str, ...]
    is_folder: bool
    bytes: int
    modified_date: str

    @property
    def path(self):
        return "/".join(self.parts)


@dataclass
class Scan:
    number: int
    status: str
    entries: list[Entry]
    bytes: int
    file_count: int
    skipped_links: int


def filesystem_path(path: Path) -> str:
    absolute = str(path.absolute())
    if os.name != "nt" or absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def is_folder(info) -> bool:
    return stat.S_ISDIR(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_DIRECTORY
    )


def is_link(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def find_discs(root, burned_folder, pending_start, pending_end):
    discs = {}
    ignored_burned = 0
    overlapping_sources = 0
    for directory, status in ((root, "pending"), (root / burned_folder, "burned")):
        source = filesystem_path(directory)
        try:
            children = os.scandir(source)
        except FileNotFoundError:
            if status == "burned":
                continue
            raise
        seen = set()
        with children:
            for item in children:
                match = DISC_NAME.fullmatch(item.name)
                if not match:
                    if status == "burned":
                        ignored_burned += 1
                    continue
                number = int(match[1])
                if not 1 <= number <= 60:
                    if status == "burned":
                        ignored_burned += 1
                    continue
                if status == "pending" and not pending_start <= number <= pending_end:
                    continue
                info = item.stat(follow_symlinks=False)
                if not is_folder(info) or is_link(info):
                    if status == "burned":
                        ignored_burned += 1
                    continue
                if number in seen:
                    raise ValueError(f"{status} 目录下有多个 BD-{number}，请先确认盘号。")
                seen.add(number)
                if number in discs:
                    overlapping_sources += 1
                discs[number] = (item.path, status)
    return discs, ignored_burned, overlapping_sources


def scan_disc(number, path, status):
    entries = []
    bytes_total = file_count = skipped_links = 0
    pending = [(path, ())]
    while pending:
        directory, parent_parts = pending.pop()
        with os.scandir(directory) as children:
            for child in children:
                metadata = child.stat(follow_symlinks=False)
                parts = (*parent_parts, child.name)
                folder = is_folder(metadata)
                size = 0 if folder else metadata.st_size
                date = datetime.fromtimestamp(metadata.st_mtime).date().isoformat()
                entries.append(Entry(parts, folder, size, date))
                if folder:
                    if is_link(metadata):
                        skipped_links += 1
                    else:
                        pending.append((child.path, parts))
                else:
                    bytes_total += size
                    file_count += 1

    folder_sizes = {}
    for entry in reversed(entries):
        if entry.is_folder:
            entry.bytes = folder_sizes.get(entry.parts, 0)
        parent = entry.parts[:-1]
        folder_sizes[parent] = folder_sizes.get(parent, 0) + entry.bytes
    return Scan(number, status, entries, bytes_total, file_count, skipped_links)


def save_disc(db, scan):
    source_key = f"disc:{scan.number}"
    existing = db.execute(
        """
        SELECT status,
            EXISTS(SELECT 1 FROM relabels WHERE target_content_id = dc.id)
                AND NOT EXISTS(SELECT 1 FROM discs WHERE content_id = dc.id) AS retired
        FROM disc_content dc WHERE source_key = ?
        """,
        (source_key,),
    ).fetchone()
    if existing and (existing["retired"] or existing["status"] == "burned" and scan.status == "pending"):
        # Retain the history's displaced index and any known burned copy that
        # is absent from the burned source during this import.
        return None
    db.execute(
        """
        INSERT INTO disc_content(source_number, status, source_key, bytes, file_count)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
            status = excluded.status, bytes = excluded.bytes, file_count = excluded.file_count
        """,
        (scan.number, scan.status, source_key, scan.bytes, scan.file_count),
    )
    content_id = db.execute(
        "SELECT id FROM disc_content WHERE source_key = ?", (source_key,)
    ).fetchone()[0]
    db.execute(
        "UPDATE discs SET content_id = ? WHERE number = ? AND import_locked = 0",
        (content_id, scan.number),
    )

    before = {
        row["path"]: (row["id"], row["parent_id"], row["kind"], row["bytes"], row["modified_date"])
        for row in db.execute(
            """
            SELECT id, path, parent_id, kind, bytes, modified_date
            FROM file_nodes WHERE content_id = ?
            """,
            (content_id,),
        )
    }
    parents = {(): None}
    for entry in scan.entries:
        path = entry.path
        parent_id = parents[entry.parts[:-1]]
        kind = "folder" if entry.is_folder else "file"
        old = before.pop(path, None)
        metadata = (parent_id, kind, entry.bytes, entry.modified_date)
        if old is None:
            node_id = db.execute(
                """
                INSERT INTO file_nodes(content_id, path, name, parent_id, kind, bytes, modified_date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (content_id, path, entry.parts[-1], *metadata),
            ).lastrowid
        else:
            node_id = old[0]
            if old[1:] != metadata:
                db.execute(
                    """
                    UPDATE file_nodes SET parent_id = ?, kind = ?, bytes = ?, modified_date = ?
                    WHERE id = ?
                    """,
                    (*metadata, node_id),
                )
        if entry.is_folder:
            parents[entry.parts] = node_id

    # Deleting deepest paths first also works with a non-cascading parent reference.
    db.executemany(
        "DELETE FROM file_nodes WHERE id = ?",
        [(before[path][0],) for path in sorted(before, key=lambda path: path.count("/"), reverse=True)],
    )
    return content_id


class ExplorerSeed:
    def __init__(self, db):
        self.db = db
        self.folders = {(): 1}

    def folder(self, parts):
        key = tuple(part.casefold() for part in parts)
        if key in self.folders:
            return self.folders[key]
        parent_id = self.folder(parts[:-1])
        source_key = "import-folder:" + json.dumps(key, ensure_ascii=False)
        row = self.db.execute(
            "SELECT id FROM explorer_nodes WHERE source_key = ? AND kind = 'folder'",
            (source_key,),
        ).fetchone()
        if row is None:
            # Keep an existing folder independently created by the user as a
            # separate entry if its name already occupies this location.
            name = parts[-1]
            suffix = 2
            while self.db.execute(
                "SELECT 1 FROM explorer_nodes WHERE parent_id = ? AND CASEFOLD(name) = ?",
                (parent_id, name.casefold()),
            ).fetchone():
                name = f"{parts[-1]} ({suffix})"
                suffix += 1
            folder_id = self.db.execute(
                """
                INSERT INTO explorer_nodes(parent_id, name, kind, source_key)
                VALUES (?, ?, 'folder', ?)
                """,
                (parent_id, name, source_key),
            ).lastrowid
        else:
            folder_id = row["id"]
        self.folders[key] = folder_id
        return folder_id

    def shortcut(self, parts, content_id, disc_number, target_path):
        key = json.dumps((parts, target_path), ensure_ascii=False).encode("utf-8")
        suffix = hashlib.sha256(key).hexdigest()
        source_key = f"import-disc:{disc_number}:{suffix}"
        ensure_explorer_shortcut(
            self.db, self.folder(parts), content_id, source_key, target_path
        )

    def disc(self, scan, content_id, group):
        wrapper = next(
            (
                entry.parts[0] for entry in scan.entries
                if group and entry.is_folder and len(entry.parts) == 1
                and entry.parts[0].casefold() == group.casefold()
            ),
            None,
        )
        if group:
            self.shortcut((group,), content_id, scan.number, wrapper or "")
        root_file = not scan.entries
        for entry in scan.entries:
            if not entry.is_folder:
                root_file = root_file or len(entry.parts) == 1
                continue
            if wrapper and entry.parts[0] == wrapper:
                if len(entry.parts) == 1:
                    continue
                parts = (group, *entry.parts[1:])
            elif group and wrapper is None:
                parts = (group, *entry.parts)
            else:
                parts = entry.parts
            self.shortcut(parts, content_id, scan.number, entry.path)
        if root_file and (not group or wrapper):
            self.shortcut((), content_id, scan.number, "")


def get_settings(db):
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS import_settings (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            settings_json TEXT NOT NULL
        )
        """
    )
    row = db.execute("SELECT settings_json FROM import_settings WHERE id = 1").fetchone()
    return json.loads(row[0]) if row else {}


def resolve_settings(args, stored):
    burned_folder = args.burned_folder or stored.get("burned_folder", "BD已刻录")
    if burned_folder in (".", "..") or any(char in burned_folder for char in "/\\:"):
        raise ValueError("已刻录目录必须是源目录下的单层文件夹名称。")
    start = args.pending_start if args.pending_start is not None else stored.get("pending_start", 1)
    end = args.pending_end if args.pending_end is not None else stored.get("pending_end", 60)
    if not 1 <= start <= end <= 60:
        raise ValueError("待刻录盘号范围必须在 1 至 60 内，且起始盘号不能大于结束盘号。")

    groups = args.group if args.group is not None else stored.get("groups", [])
    validated_groups = []
    by_disc = {}
    for name, first, last in groups:
        name = name.strip()
        first, last = int(first), int(last)
        if not name or len(name) > 200 or any(char in name for char in "/\\"):
            raise ValueError("分组名称必须是一个有效的文件夹名称。")
        if not 1 <= first <= last <= 60:
            raise ValueError("分组盘号范围必须在 1 至 60 内，且起始盘号不能大于结束盘号。")
        for number in range(first, last + 1):
            if number in by_disc:
                raise ValueError(f"BD-{number} 属于多个分组，请调整分组范围。")
            by_disc[number] = name
        validated_groups.append([name, first, last])
    return {
        "burned_folder": burned_folder,
        "pending_start": start,
        "pending_end": end,
        "groups": validated_groups,
    }, by_disc


def import_catalog(args):
    root = args.root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("备份源必须是目录。")
    database = args.database.expanduser().resolve()
    with closing(open_database(database)) as db:
        initialize(db)
        stored = get_settings(db)
        settings, groups = resolve_settings(args, stored)
        discs, ignored_burned, overlaps = find_discs(
            root, settings["burned_folder"], settings["pending_start"], settings["pending_end"]
        )
        if not discs:
            raise ValueError("找不到可导入的编号光盘目录。")
        scans = [
            scan_disc(number, path, status)
            for number, (path, status) in sorted(discs.items())
        ]
        with db:
            db.execute("BEGIN IMMEDIATE")
            explorer = ExplorerSeed(db)
            preserved = 0
            for scan in scans:
                content_id = save_disc(db, scan)
                if content_id is None:
                    preserved += 1
                else:
                    explorer.disc(scan, content_id, groups.get(scan.number))
            db.execute(
                """
                INSERT INTO import_settings(id, settings_json) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET settings_json = excluded.settings_json
                """,
                (json.dumps(settings, ensure_ascii=False),),
            )

    for status, label in (("burned", "已刻录"), ("pending", "待刻录")):
        matches = [scan for scan in scans if scan.status == status]
        print(
            f"{label} {len(matches)} 张，{sum(scan.file_count for scan in matches):,} 个文件，"
            f"{sum(scan.bytes for scan in matches):,} 字节。"
        )
    skipped_links = sum(scan.skipped_links for scan in scans)
    if skipped_links:
        print(f"保留了 {skipped_links} 个目录链接条目，并跳过遍历链接目标。")
    if ignored_burned:
        print(f"已刻录目录中有 {ignored_burned} 个未纳入编号光盘索引的条目。")
    if overlaps:
        print(f"{overlaps} 个盘号同时存在于两处，已选用已刻录目录中的副本。")
    if preserved:
        print(f"保留了 {preserved} 份已刻录或已换标的历史索引。")


def main():
    parser = argparse.ArgumentParser(description="将编号光盘的目录树导入本地 SQLite。")
    parser.add_argument("root", type=Path, help="含光盘子目录的备份源")
    parser.add_argument("--database", "--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--burned-folder", help="已刻录子目录名，默认 BD已刻录")
    parser.add_argument("--pending-start", type=int, help="待刻录起始盘号，默认 1")
    parser.add_argument("--pending-end", type=int, help="待刻录结束盘号，默认 60")
    parser.add_argument(
        "--group", nargs=3, action="append", metavar=("名称", "起始盘号", "结束盘号"),
        help="按盘号范围合并资源管理器中的同名文件夹，可重复使用",
    )
    args = parser.parse_args()
    try:
        import_catalog(args)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"导入失败。{exc}\n")


if __name__ == "__main__":
    main()
