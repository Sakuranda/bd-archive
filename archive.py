"""The local HTTP and SQLite service for the disc catalog."""

import argparse
from contextlib import closing
from datetime import date, datetime
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import sqlite3
import tempfile
from urllib.parse import parse_qs, unquote, urlsplit
import webbrowser


PROJECT = Path(__file__).resolve().parent
DEFAULT_DB = PROJECT / 'data' / 'catalog.sqlite3'
DEFAULT_PORT = 8619
PAGE_SIZE = 100
MAX_BODY_BYTES = 128 * 1024
TREE_ORDER = "(kind = 'file'), name COLLATE NATURAL_NAME, id"
FILE_ITEM = """
    id, name, kind, path, bytes, modified_date, note, parent_id,
    EXISTS(SELECT 1 FROM file_nodes AS child WHERE child.parent_id = file_nodes.id)
        AS has_children
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS disc_content (
    id INTEGER PRIMARY KEY,
    source_number INTEGER NOT NULL CHECK(source_number BETWEEN 1 AND 60),
    status TEXT NOT NULL CHECK(status IN ('burned', 'pending')),
    source_key TEXT NOT NULL UNIQUE,
    bytes INTEGER NOT NULL DEFAULT 0 CHECK(bytes >= 0),
    file_count INTEGER NOT NULL DEFAULT 0 CHECK(file_count >= 0)
);
CREATE TABLE IF NOT EXISTS discs (
    number INTEGER PRIMARY KEY CHECK(number BETWEEN 1 AND 60),
    content_id INTEGER UNIQUE REFERENCES disc_content(id),
    note TEXT NOT NULL DEFAULT '',
    import_locked INTEGER NOT NULL DEFAULT 0 CHECK(import_locked IN (0, 1))
);
CREATE TABLE IF NOT EXISTS file_nodes (
    id INTEGER PRIMARY KEY,
    content_id INTEGER NOT NULL REFERENCES disc_content(id),
    parent_id INTEGER REFERENCES file_nodes(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('folder', 'file')),
    bytes INTEGER NOT NULL DEFAULT 0 CHECK(bytes >= 0),
    modified_date TEXT,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE(content_id, path)
);
CREATE INDEX IF NOT EXISTS file_children ON file_nodes(parent_id, content_id);
CREATE INDEX IF NOT EXISTS file_dates ON file_nodes(modified_date) WHERE kind = 'file';
CREATE TABLE IF NOT EXISTS explorer_nodes (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES explorer_nodes(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('folder', 'disc')),
    content_id INTEGER REFERENCES disc_content(id),
    note TEXT NOT NULL DEFAULT '',
    source_key TEXT UNIQUE,
    target_path TEXT NOT NULL DEFAULT '',
    CHECK((kind = 'folder' AND content_id IS NULL) OR (kind = 'disc' AND content_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS explorer_children ON explorer_nodes(parent_id);
CREATE TABLE IF NOT EXISTS relabels (
    id INTEGER PRIMARY KEY,
    source INTEGER NOT NULL REFERENCES discs(number),
    target INTEGER NOT NULL REFERENCES discs(number),
    source_content_id INTEGER NOT NULL REFERENCES disc_content(id),
    target_content_id INTEGER REFERENCES disc_content(id),
    source_note TEXT NOT NULL,
    target_note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class CatalogError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def natural_parts(value):
    return [int(part) if index % 2 else part for index, part in
            enumerate(re.split(r'([0-9]+)', value.casefold()))]


def natural_compare(left, right):
    left, right = natural_parts(left), natural_parts(right)
    return (left > right) - (left < right)


def open_database(path=DEFAULT_DB):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    connection.create_function('CASEFOLD', 1, str.casefold, deterministic=True)
    connection.create_collation('NATURAL_NAME', natural_compare)
    return connection


def initialize(connection):
    connection.execute('PRAGMA journal_mode = WAL')
    connection.executescript(SCHEMA)
    with connection:
        connection.executemany('INSERT OR IGNORE INTO discs(number) VALUES (?)',
                               ((number,) for number in range(1, 61)))
        connection.execute("""
            INSERT OR IGNORE INTO explorer_nodes(id, name, kind) VALUES (1, '文件夹', 'folder')
        """)


def ensure_explorer_shortcut(connection, parent_id, content_id, source_key, target_path=''):
    # A stable source key keeps edits and moves intact during re-imports.
    name = connection.execute('SELECT source_number FROM disc_content WHERE id = ?',
                              (content_id,)).fetchone()['source_number']
    connection.execute("""
        INSERT INTO explorer_nodes(parent_id, name, kind, content_id, source_key, target_path)
        VALUES (?, ?, 'disc', ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET target_path = excluded.target_path
    """, (parent_id, f'BD-{name}', content_id, source_key, target_path))
    return connection.execute('SELECT id FROM explorer_nodes WHERE source_key = ?',
                              (source_key,)).fetchone()['id']


def as_int(value, label, minimum=1, maximum=2**63 - 1):
    if isinstance(value, bool) or not (isinstance(value, int) or
                                      isinstance(value, str) and re.fullmatch(r'[0-9]{1,19}', value)):
        raise CatalogError(f'{label}填写有误。')
    number = int(value)
    if not minimum <= number <= maximum:
        raise CatalogError(f'{label}超出有效范围。')
    return number


def disc_number(value):
    return as_int(value, '光盘编号', maximum=60)


def item_id(value):
    return as_int(value, '条目编号')


def disk_rows(connection):
    return [dict(row) for row in connection.execute("""
        SELECT number, COALESCE(status, 'empty') AS status, COALESCE(bytes, 0) AS bytes,
               COALESCE(file_count, 0) AS file_count, note, content_id, source_number
        FROM discs LEFT JOIN disc_content ON content_id = disc_content.id ORDER BY number
    """)]


def overview(connection):
    discs = disk_rows(connection)
    burned = [item for item in discs if item['status'] == 'burned']
    pending = [item for item in discs if item['status'] == 'pending']
    return {
        'burned_count': len(burned),
        'burned_bytes': sum(item['bytes'] for item in burned),
        'burned_file_count': sum(item['file_count'] for item in burned),
        'pending_count': len(pending),
        'pending_bytes': sum(item['bytes'] for item in pending),
        'file_count': sum(item['file_count'] for item in discs),
        'discs': discs,
    }


def page_data(total, requested):
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(as_int(requested or 1, '页码'), pages)
    return {'total': total, 'pages': pages, 'page': page}


def disk_id(connection, query):
    if query.get('content'):
        content_id = item_id(query['content'])
        row = connection.execute("""
            SELECT source_number, number, status FROM disc_content
            LEFT JOIN discs ON discs.content_id = disc_content.id WHERE disc_content.id = ?
        """, (content_id,)).fetchone()
        if not row:
            raise CatalogError('这份光盘索引不存在。', 404)
        disc = row['number'] if row['number'] is not None else row['source_number']
        status = row['status'] if row['number'] is not None else 'replaced'
        return disc, content_id, status
    disc = disc_number(query.get('disc'))
    row = connection.execute("""
        SELECT content_id, COALESCE(status, 'empty') AS status FROM discs
        LEFT JOIN disc_content ON discs.content_id = disc_content.id WHERE number = ?
    """, (disc,)).fetchone()
    return disc, row['content_id'], row['status']


def file_item(row):
    return dict(row) | {'has_children': bool(row['has_children'])}


def tree(connection, query):
    disc, content_id, status = disk_id(connection, query)
    parent = item_id(query['parent']) if query.get('parent') else None
    if parent is not None and not connection.execute("""
        SELECT 1 FROM file_nodes WHERE id = ? AND content_id = ? AND kind = 'folder'
    """, (parent, content_id)).fetchone():
        raise CatalogError('这层文件夹不存在。', 404)
    params = (content_id, parent)
    total = connection.execute("""
        SELECT COUNT(*) FROM file_nodes WHERE content_id = ? AND parent_id IS ?
    """, params).fetchone()[0]
    result = page_data(total, query.get('page'))
    rows = connection.execute(f"""
        SELECT {FILE_ITEM} FROM file_nodes WHERE content_id = ? AND parent_id IS ?
        ORDER BY {TREE_ORDER} LIMIT ? OFFSET ?
    """, params + (PAGE_SIZE, (result['page'] - 1) * PAGE_SIZE))
    return result | {'items': [file_item(row) for row in rows], 'disc': disc,
                     'content_id': content_id, 'status': status}


def tree_item_page(connection, row, content_id):
    previous = connection.execute("""
        SELECT COUNT(*) FROM file_nodes WHERE content_id = ? AND parent_id IS ?
          AND ((kind = 'file'), name COLLATE NATURAL_NAME, id) < (?, ?, ?)
    """, (content_id, row['parent_id'], row['kind'] == 'file', row['name'], row['id'])).fetchone()[0]
    return previous // PAGE_SIZE + 1


def locate(connection, query):
    disc, content_id, status = disk_id(connection, query)
    row = connection.execute(f"""
        SELECT {FILE_ITEM} FROM file_nodes WHERE content_id = ? AND id = ?
    """, (content_id, item_id(query.get('id')))).fetchone()
    if row is None:
        raise CatalogError('在这张光盘里找不到该条目。', 404)
    item = file_item(row)
    page = tree_item_page(connection, row, content_id)
    folders = []
    while row['parent_id'] is not None:
        row = connection.execute('SELECT * FROM file_nodes WHERE id = ?',
                                 (row['parent_id'],)).fetchone()
        folders.append({'id': row['id'], 'name': row['name'],
                        'page': tree_item_page(connection, row, content_id)})
    return {'disc': disc, 'content_id': content_id, 'status': status, 'item': item,
            'folders': list(reversed(folders)), 'page': page}


def search_date(value):
    try:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise CatalogError('请输入有效的日期。') from exc


def search(connection, query):
    where = ["file_nodes.kind = 'file'"]
    params = []
    mode = query.get('mode', 'name')
    if mode == 'name':
        term = query.get('q', '').strip()
        if not term or len(term) > 500:
            raise CatalogError('请输入要搜索的文件名，最多 500 个字符。')
        where.append('instr(CASEFOLD(file_nodes.name), ?) > 0')
        params.append(term.casefold())
    elif mode == 'date':
        start = query.get('from') or query.get('date')
        end = query.get('to') or query.get('date')
        if not (start or end):
            raise CatalogError('请至少选择一个日期。')
        if start:
            where.append('file_nodes.modified_date >= ?')
            params.append(search_date(start))
        if end:
            where.append('file_nodes.modified_date <= ?')
            params.append(search_date(end))
        if start and end and params[0] > params[1]:
            raise CatalogError('结束日期应等于或晚于开始日期。')
    else:
        raise CatalogError('搜索方式无效。')
    matches = (' FROM file_nodes JOIN discs USING(content_id) WHERE '
               + ' AND '.join(where))
    total = connection.execute('SELECT COUNT(*)' + matches, params).fetchone()[0]
    result = page_data(total, query.get('page'))
    rows = connection.execute('''
        SELECT file_nodes.id, file_nodes.name, file_nodes.path, file_nodes.bytes,
               file_nodes.modified_date, file_nodes.note, file_nodes.parent_id,
               number AS disc, file_nodes.content_id
    ''' + matches + ''' ORDER BY number, file_nodes.path COLLATE NOCASE, file_nodes.id
        LIMIT ? OFFSET ?
    ''', params + [PAGE_SIZE, (result['page'] - 1) * PAGE_SIZE])
    return result | {'items': [dict(row) for row in rows]}


def explorer_folder(connection, folder_id):
    row = connection.execute("""
        SELECT id, parent_id, name, note FROM explorer_nodes WHERE id = ? AND kind = 'folder'
    """, (folder_id,)).fetchone()
    if row is None:
        raise CatalogError('这个文件夹不存在。', 404)
    return dict(row)


def explorer_ancestors(connection, folder):
    ancestors = [folder]
    while ancestors[-1]['parent_id'] is not None:
        ancestors.append(explorer_folder(connection, ancestors[-1]['parent_id']))
    return list(reversed(ancestors))


def explorer(connection, query):
    folder_id = item_id(query['folder']) if query.get('folder') else 1
    folder = explorer_folder(connection, folder_id)
    breadcrumbs = explorer_ancestors(connection, folder)
    rows = connection.execute("""
        SELECT explorer_nodes.id,
               CASE WHEN discs.number IS NOT NULL THEN
                   'BD-' || discs.number || substr(explorer_nodes.name, length('BD-' || source_number) + 1)
                   ELSE explorer_nodes.name END AS name,
               explorer_nodes.kind, explorer_nodes.note, target_path AS path,
               COALESCE(discs.number, source_number) AS disc,
               explorer_nodes.content_id, file_nodes.id AS node_id,
               CASE WHEN discs.number IS NULL AND source_number IS NOT NULL THEN 'replaced'
                   ELSE disc_content.status END AS status
        FROM explorer_nodes LEFT JOIN disc_content ON disc_content.id = explorer_nodes.content_id
        LEFT JOIN discs ON discs.content_id = explorer_nodes.content_id
        LEFT JOIN file_nodes ON file_nodes.content_id = explorer_nodes.content_id
                            AND file_nodes.path = explorer_nodes.target_path
        WHERE explorer_nodes.parent_id = ?
        ORDER BY (explorer_nodes.kind = 'disc'), name COLLATE NATURAL_NAME, explorer_nodes.id
    """, (folder_id,))
    return {'folder': folder, 'breadcrumbs': breadcrumbs, 'items': [dict(row) for row in rows]}


def set_note(connection, data):
    note = data.get('note')
    kind = data.get('kind')
    if not isinstance(note, str) or len(note) > 20000:
        raise CatalogError('备注须为文本，最多 20,000 个字符。')
    fields = {'disc': ('discs', 'number'), 'node': ('file_nodes', 'id'),
              'explorer': ('explorer_nodes', 'id')}
    if not isinstance(kind, str) or kind not in fields:
        raise CatalogError('备注对应的条目类型无效。')
    table, id_column = fields[kind]
    result = connection.execute(f'UPDATE {table} SET note = ? WHERE {id_column} = ?',
                                (note, item_id(data.get('id'))))
    if result.rowcount != 1:
        raise CatalogError('备注对应的条目不存在。', 404)
    return {'ok': True}


def clean_folder_name(name):
    if not isinstance(name, str):
        raise CatalogError('请输入文件夹名称。')
    name = name.strip()
    if not name or len(name) > 200 or name.endswith('.') or re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        raise CatalogError('请输入 1–200 个字符的有效文件夹名称。')
    return name


def sibling_names(connection, parent):
    return {row['name'].casefold() for row in connection.execute(
        'SELECT name FROM explorer_nodes WHERE parent_id = ?', (parent,))}


def new_folder(connection, data):
    parent = item_id(data.get('parent') or 1)
    explorer_folder(connection, parent)
    name = clean_folder_name(data.get('name'))
    if name.casefold() in sibling_names(connection, parent):
        raise CatalogError('此处已有同名条目。', 409)
    folder_id = connection.execute("""
        INSERT INTO explorer_nodes(parent_id, name, kind) VALUES (?, ?, 'folder')
    """, (parent, name)).lastrowid
    return {'ok': True, 'id': folder_id}


def available_name(name, used):
    if name.casefold() not in used:
        return name
    stem = f'{name} - 副本'
    candidate = stem
    suffix = 2
    while candidate.casefold() in used:
        candidate = f'{stem} ({suffix})'
        suffix += 1
    return candidate


def copy_explorer_row(connection, row, parent, name):
    pending = [(row, parent, name)]
    result = None
    while pending:
        row, parent, name = pending.pop()
        copy_id = connection.execute("""
            INSERT INTO explorer_nodes(parent_id, name, kind, content_id, note, target_path)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (parent, name, row['kind'], row['content_id'], row['note'], row['target_path'])).lastrowid
        if result is None:
            result = copy_id
        if row['kind'] == 'folder':
            children = connection.execute('SELECT * FROM explorer_nodes WHERE parent_id = ? ORDER BY id DESC',
                                          (row['id'],)).fetchall()
            pending.extend((child, copy_id, child['name']) for child in children)
    return result


def paste_explorer(connection, data):
    parent = item_id(data.get('parent') or 1)
    ancestors = {row['id'] for row in explorer_ancestors(connection, explorer_folder(connection, parent))}
    operation = data.get('operation')
    ids = data.get('ids')
    if operation not in ('copy', 'cut') or not isinstance(ids, list) or not 1 <= len(ids) <= 1000:
        raise CatalogError('请选择要粘贴的条目。')
    ids = list(dict.fromkeys(item_id(value) for value in ids))
    if set(ids) & ancestors:
        raise CatalogError('不能把文件夹粘贴到自身或子文件夹中。', 409)
    used_names = sibling_names(connection, parent)
    new_ids = []
    for identifier in ids:
        row = connection.execute('SELECT * FROM explorer_nodes WHERE id = ?', (identifier,)).fetchone()
        if row is None or row['parent_id'] is None:
            raise CatalogError('剪贴板中的条目已不存在。', 404)
        if operation == 'cut' and row['parent_id'] == parent:
            new_ids.append(identifier)
            continue
        name = available_name(row['name'], used_names)
        used_names.add(name.casefold())
        if operation == 'cut':
            connection.execute('UPDATE explorer_nodes SET parent_id = ?, name = ? WHERE id = ?',
                               (parent, name, identifier))
            new_ids.append(identifier)
        else:
            new_ids.append(copy_explorer_row(connection, row, parent, name))
    return {'ok': True, 'ids': new_ids}


def relabel(connection, data):
    source, target = disc_number(data.get('source')), disc_number(data.get('target'))
    if source == target:
        raise CatalogError('原编号与新编号应当不同。')
    old = connection.execute('SELECT content_id, note FROM discs WHERE number = ?', (source,)).fetchone()
    newer = connection.execute('SELECT content_id, note FROM discs WHERE number = ?', (target,)).fetchone()
    if old['content_id'] is None:
        raise CatalogError('原编号暂无可转移的光盘索引。', 409)
    created_at = datetime.now().astimezone().isoformat(timespec='seconds')
    result = connection.execute("""
        INSERT INTO relabels(source, target, source_content_id, target_content_id,
                             source_note, target_note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (source, target, old['content_id'], newer['content_id'], old['note'], newer['note'], created_at))
    connection.execute("UPDATE discs SET content_id = NULL, note = '', import_locked = 1 WHERE number = ?",
                       (source,))
    connection.execute('UPDATE discs SET content_id = ?, note = ?, import_locked = 1 WHERE number = ?',
                       (old['content_id'], old['note'], target))
    return {'ok': True, 'id': result.lastrowid, 'source': source, 'target': target,
            'overwrote': newer['content_id'] is not None}


def relabel_history(connection):
    return {'items': [dict(row) | {'overwrote': row['target_content_id'] is not None}
                      for row in connection.execute('SELECT * FROM relabels ORDER BY id DESC')]}


READ_ROUTES = {
    '/api/health': lambda connection, query: {'service': 'bd-archive'},
    '/api/overview': lambda connection, query: overview(connection),
    '/api/tree': tree,
    '/api/locate': locate,
    '/api/search': search,
    '/api/explorer': explorer,
    '/api/relabels': lambda connection, query: relabel_history(connection),
}

WRITE_ROUTES = {
    '/api/note': set_note,
    '/api/explorer/folder': new_folder,
    '/api/explorer/paste': paste_explorer,
    '/api/relabel': relabel,
}


class CatalogServer(ThreadingHTTPServer):
    allow_reuse_address = os.name != 'nt'
    allow_reuse_port = False

    def __init__(self, port, database):
        self.database = database
        self.static = (PROJECT / 'static').resolve()
        super().__init__(('127.0.0.1', port), CatalogHandler)

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class CatalogHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        # Query strings may contain personal filenames.
        pass

    def send_data(self, status, media_type, body, head=False):
        self.send_response(status)
        self.send_header('Content-Type', media_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'; "
                         "frame-ancestors 'none'")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def send_json(self, status, data, head=False):
        self.send_data(status, 'application/json; charset=utf-8',
                       json.dumps(data, ensure_ascii=False).encode('utf-8'), head=head)

    def verify_local(self):
        port = self.server.server_port
        accepted = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if port == 80:
            accepted.update({'127.0.0.1', 'localhost'})
        host = self.headers.get('Host', '').lower()
        origin = self.headers.get('Origin')
        if host not in accepted or origin is not None and origin != f'http://{host}':
            raise CatalogError('请从本机地址访问。', 403)
        if self.headers.get('Sec-Fetch-Site') == 'cross-site' and self.path.startswith('/api/'):
            raise CatalogError('请从本站访问接口。', 403)

    def read_json(self):
        if self.headers.get_content_type() != 'application/json':
            raise CatalogError('请求格式须为 JSON。', 415)
        length = as_int(self.headers.get('Content-Length'), '请求长度')
        if length > MAX_BODY_BYTES:
            raise CatalogError('请求内容过大。', 413)
        try:
            data = json.loads(self.rfile.read(length).decode('utf-8'))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise CatalogError('无法读取请求中的 JSON。') from exc
        if not isinstance(data, dict):
            raise CatalogError('请求须为 JSON 对象。')
        return data

    def send_static(self, path, head):
        relative = 'index.html' if path == '/' else unquote(path).lstrip('/')
        asset = (self.server.static / relative).resolve()
        if not asset.is_relative_to(self.server.static) or not asset.is_file():
            raise CatalogError('页面不存在。', 404)
        media_type = mimetypes.guess_type(asset.name)[0] or 'application/octet-stream'
        if media_type.startswith('text/') or media_type in ('application/javascript', 'application/json'):
            media_type += '; charset=utf-8'
        self.send_data(200, media_type, asset.read_bytes(), head=head)

    def dispatch(self, method):
        head = method == 'HEAD'
        try:
            self.verify_local()
            url = urlsplit(self.path)
            if method in ('GET', 'HEAD') and not url.path.startswith('/api/'):
                self.send_static(url.path, head)
                return
            routes = WRITE_ROUTES if method == 'POST' else READ_ROUTES
            if url.path not in routes:
                raise CatalogError('接口不存在。', 404)
            if method == 'POST':
                data = self.read_json()
            else:
                data = {key: values[-1] for key, values in
                        parse_qs(url.query, keep_blank_values=True, max_num_fields=32).items()}
            with closing(open_database(self.server.database)) as connection, connection:
                connection.execute('BEGIN IMMEDIATE' if method == 'POST' else 'BEGIN')
                payload = routes[url.path](connection, data)
            self.send_json(200, payload, head)
        except CatalogError as exc:
            self.send_json(exc.status, {'error': str(exc)}, head)
        except (ValueError, OverflowError):
            self.send_json(400, {'error': '请求内容有误。'}, head)
        except sqlite3.OperationalError as exc:
            if 'locked' in str(exc).lower():
                self.send_json(503, {'error': '目录正在保存，请稍后重试。'}, head)
            else:
                self.send_json(500, {'error': '无法读取或保存数据库。'}, head)
        except (sqlite3.Error, OSError) as exc:
            if not isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                self.send_json(500, {'error': '无法读取或保存目录。'}, head)

    def do_GET(self):
        self.dispatch('GET')

    def do_HEAD(self):
        self.dispatch('HEAD')

    def do_POST(self):
        self.dispatch('POST')


def check_database(connection):
    integrity = connection.execute('PRAGMA quick_check').fetchall()
    if [row[0] for row in integrity] != ['ok'] or connection.execute('PRAGMA foreign_key_check').fetchone():
        raise RuntimeError('SQLite integrity check failed.')


def backup_database(source, destination):
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if source == destination or destination.exists() and source.samefile(destination):
        raise RuntimeError('Choose a backup path different from the working database.')
    if any(Path(f'{destination}{suffix}').exists() for suffix in ('-wal', '-shm', '-journal')):
        raise RuntimeError('The backup destination is in use by a SQLite session.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix='.catalog-backup-', dir=destination.parent, delete=False) as temp:
        placeholder = Path(temp.name)
    try:
        with closing(open_database(source)) as connection, closing(sqlite3.connect(placeholder)) as target:
            connection.backup(target)
            target.execute('PRAGMA journal_mode = DELETE')
            check_database(target)
        os.replace(placeholder, destination)
    finally:
        placeholder.unlink(missing_ok=True)


def has_running_catalog(port):
    try:
        with closing(HTTPConnection('127.0.0.1', port, timeout=1)) as connection:
            connection.request('GET', '/api/health')
            response = connection.getresponse()
            return response.status == 200 and json.loads(response.read(4096)).get('service') == 'bd-archive'
    except (OSError, ValueError, AttributeError, HTTPException):
        return False


def main():
    parser = argparse.ArgumentParser(description='Local BD catalog')
    parser.add_argument('--db', type=Path, default=DEFAULT_DB, help='SQLite catalog path')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help=f'local port (default {DEFAULT_PORT})')
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument('--open', action='store_true', help='open the catalog in the default browser')
    commands.add_argument('--backup', type=Path, metavar='PATH', help='write a standalone SQLite backup')
    commands.add_argument('--check', action='store_true', help='check SQLite integrity')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('Port must be between 0 and 65535.')
    try:
        if args.backup or args.check:
            if not args.db.is_file():
                parser.error('The catalog database does not exist.')
            if args.backup:
                backup_database(args.db, args.backup)
                print(f'SQLite backup verified: {args.backup}')
            else:
                with closing(open_database(args.db)) as connection:
                    check_database(connection)
                print('SQLite integrity: ok')
            return
        with closing(open_database(args.db)) as connection:
            initialize(connection)
        try:
            server = CatalogServer(args.port, args.db)
        except OSError:
            if args.open and has_running_catalog(args.port):
                webbrowser.open(f'http://127.0.0.1:{args.port}/')
                return
            raise
        with server:
            url = f'http://127.0.0.1:{server.server_port}/'
            print(f'BD Archive  {url}', flush=True)
            if args.open:
                webbrowser.open(url)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
