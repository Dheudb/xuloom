"""Bounded, integrity-checked data backups and offline transactional restore."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import time
import uuid
import zipfile

from xuloom import __version__
from xuloom.core.storage import atomic_json

MAX_BYTES = 4 * 1024**3
MAX_FILES = 100000
ROOTS = ('history', 'workspaces', 'workbench.json')


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def safe_path(root, name):
    parts = PurePosixPath(name).parts
    if not parts or '\\' in name or any(p in ('..', '.') or ':' in p or p.endswith((' ', '.'))
        or p.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM' + str(i) for i in range(1, 10)), *('LPT' + str(i) for i in range(1, 10))}
        for p in parts) or name.startswith('/'):
        raise ValueError('Invalid archive path')
    target = root.joinpath(*parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('Archive path escapes destination')
    for probe in (target, *target.parents):
        if probe == root.parent:
            break
        if probe.is_symlink() or (probe.exists() and getattr(probe.lstat(), 'st_file_attributes', 0) & 1024):
            raise ValueError('Linked files are not supported in maintenance archives')
    return target


def inventory(root):
    result = {}
    total = 0
    for name in ROOTS:
        source = root / name
        if not source.exists():
            continue
        candidates = source.rglob('*') if source.is_dir() else [source]
        for item in candidates:
            relative = item.relative_to(root).as_posix()
            safe_path(root, relative)
            if item.is_file():
                status = item.stat()
                result[relative] = (status.st_size, status.st_mtime_ns)
                total += status.st_size
                if len(result) > MAX_FILES or total > MAX_BYTES:
                    raise ValueError('Backup exceeds 100000 files or 4 GiB')
    return result


def portable_config(cfg):
    """Only connection metadata is portable; secrets and executable plugin settings are excluded."""
    from xuloom.core.model_catalog import connections
    allowed = {'id', 'name', 'provider', 'model', 'models', 'base_url', 'network_mode', 'model_context', 'reasoning_effort'}
    return {'model_connections': [{k: v for k, v in c.items() if k in allowed} for c in connections(cfg, defaults=False)],
            'active_model_connection': cfg.get('active_model_connection', '')}


def backup(root, output, cfg=None, pricing=None):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.is_relative_to(root):
        raise ValueError('Choose a backup location outside the application data directory')
    before = inventory(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.xuloom-backup-', dir=output.parent)
    os.close(fd)
    manifest = {'format': 'xuloom-backup-1', 'version': __version__, 'created': time.time(),
                'dataRoot': str(root), 'files': {}, 'connectionSettings': portable_config(cfg or {}), 'pricing': pricing or {}}
    try:
        with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in before:
                source = safe_path(root, name)
                checksum = digest(source)
                archive.write(source, name)
                if checksum != digest(source):
                    raise ValueError('Data changed during backup; retry after stopping tasks')
                manifest['files'][name] = checksum
            if before != inventory(root):
                raise ValueError('Data changed during backup; retry after stopping tasks')
            archive.writestr('BACKUP.json', json.dumps(manifest, ensure_ascii=False))
        # Verify the bytes actually archived, not only the source before/after write.
        inspect_backup(temp)
        os.replace(temp, output)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return {'path': str(output), 'files': len(before), 'sha256': digest(output), 'version': __version__}


def inspect_backup(path, destination=None):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [i.filename for i in entries]
        if len(entries) > MAX_FILES + 1 or len(set(n.casefold() for n in names)) != len(names):
            raise ValueError('Duplicate or excessive archive entries')
        if sum(i.file_size for i in entries) > MAX_BYTES or archive.getinfo('BACKUP.json').file_size > 32 * 1024**2:
            raise ValueError('Backup exceeds size limit')
        manifest = json.loads(archive.read('BACKUP.json'))
        if manifest.get('format') != 'xuloom-backup-1' or set(names) != set(manifest['files']) | {'BACKUP.json'}:
            raise ValueError('Invalid backup manifest')
        for entry in entries:
            if entry.filename == 'BACKUP.json':
                continue
            if stat.S_ISLNK(entry.external_attr >> 16) or PurePosixPath(entry.filename).parts[0] not in ROOTS:
                raise ValueError('Unsupported backup entry')
            target = safe_path(Path(destination or tempfile.gettempdir()) / 'validated-backup', entry.filename)
            checksum = hashlib.sha256()
            if destination:
                target = safe_path(Path(destination), entry.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source:
                writer = target.open('xb') if destination else None
                try:
                    for chunk in iter(lambda: source.read(1024 * 1024), b''):
                        checksum.update(chunk)
                        if writer:
                            writer.write(chunk)
                finally:
                    if writer:
                        writer.close()
            if checksum.hexdigest() != manifest['files'][entry.filename]:
                raise ValueError('Backup integrity check failed')
    return manifest


def _rewrite_paths(stage, old, new):
    """Rebase owned workspace paths in metadata, leaving chat text and external projects untouched."""
    old_owned, new_owned = str(Path(old) / 'workspaces'), str(new / 'workspaces')
    path_keys = {'cwd', 'directory', 'defaultDirectory', 'path', 'workspace', 'project', 'projectPath', 'session_cwd',
                 'working_directory', 'standaloneDirectories', 'projects'}
    hashes = {}
    def rewrite(value, key=''):
        if isinstance(value, dict):
            return {(os.path.normcase(rewrite(k, 'path')) if key == 'projectMeta' else k): rewrite(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [rewrite(v, key) for v in value]
        if isinstance(value, str) and key in path_keys and (os.path.normcase(value) == os.path.normcase(old_owned)
                or os.path.normcase(value).startswith(os.path.normcase(old_owned + os.sep))):
            updated = new_owned + value[len(old_owned):]
            identity = value.lower() if os.name == 'nt' else value
            target = updated.lower() if os.name == 'nt' else updated
            hashes[hashlib.sha256(identity.encode()).hexdigest()[:16]] = hashlib.sha256(target.encode()).hexdigest()[:16]
            return updated
        return value
    files = list((stage / 'history').rglob('*.json')) + [stage / 'workbench.json']
    for file in files:
        if file.is_file():
            try:
                original = json.loads(file.read_text(encoding='utf-8-sig'))
            except (ValueError, UnicodeError):
                continue
            updated = rewrite(original)
            if updated != original:
                atomic_json(file, updated)
    for before, after in hashes.items():
        source, target = stage / 'history' / before, stage / 'history' / after
        if before != after and source.exists():
            if target.exists():
                raise ValueError('Restored workspace identity collision')
            source.rename(target)
    # UI keys contain the workspace hash followed by the session ID.
    preferences = stage / 'workbench.json'
    if preferences.exists():
        data = preferences.read_text(encoding='utf-8')
        for before, after in hashes.items():
            data = data.replace('"' + before + ':', '"' + after + ':')
        preferences.write_text(data, encoding='utf-8')


def restore(root, archive_path, *, confirmed=False):
    """Offline only. Preserve replaced data in a sibling rollback directory; never replay tasks."""
    if not confirmed:
        raise ValueError('Restore requires explicit confirmation')
    from xuloom.core.session_lock import SessionLock
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    leases = []
    stage = Path(tempfile.mkdtemp(prefix='.xuloom-restore-', dir=root.parent))
    rollback = root.parent / ('xuloom-data-rollback-' + uuid.uuid4().hex)
    moved, installed = [], []
    try:
        # Taking every existing OS lease excludes writers, including detached workers.
        gate = SessionLock(root / 'session-locks', 'maintenance')
        gate.acquire()
        leases.append(gate)
        for marker in sorted((root / 'session-locks').glob('*.lock')):
            if marker.stem == 'maintenance':
                continue
            lease = SessionLock(marker.parent, marker.stem)
            lease.acquire()
            leases.append(lease)
        manifest = inspect_backup(archive_path, stage)
        _rewrite_paths(stage, manifest['dataRoot'], root)
        rollback.mkdir()
        for name in ROOTS:
            target = safe_path(root, name)
            if target.exists():
                target.rename(rollback / name)
                moved.append(name)
            source = stage / name
            if source.exists():
                source.rename(target)
                installed.append(name)
        # Portable settings are exported separately for review, never silently enable extensions or replace keys.
        atomic_json(root / 'restored-settings.json', {'connections': manifest['connectionSettings'], 'pricing': manifest['pricing']})
        atomic_json(rollback / 'RESTORE.json', {'restoredFrom': str(archive_path), 'dataRoot': str(root), 'version': __version__})
        return {'path': str(root), 'rollback': str(rollback), 'settings': str(root / 'restored-settings.json')}
    except BaseException:
        for name in reversed(installed):
            (root / name).rename(stage / name)
        for name in reversed(moved):
            (rollback / name).rename(root / name)
        raise
    finally:
        for lease in reversed(leases):
            lease.close()
        shutil.rmtree(stage)


def diagnostic(root, output):
    """Allowlisted metadata only: no prompts, paths, API URLs, keys, environment or tool arguments."""
    from collections import Counter
    counts = Counter()
    root = Path(root)
    for path in (root / 'history').glob('*/index.json'):
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            counts['workspaces'] += 1
            counts['sessions'] += len(data.get('sessions', {}))
        except (OSError, ValueError, TypeError):
            counts['unreadableIndexes'] += 1
    value = {'format': 'xuloom-diagnostics-1', 'version': __version__, 'platform': os.name,
             'python': '.'.join(map(str, __import__('sys').version_info[:3])), 'counts': dict(counts)}
    atomic_json(Path(output), value)
    return {'path': str(output), **value}


def import_settings(root):
    """Explicitly import metadata/prices after restore; existing secrets always stay with their original route."""
    from xuloom.core.config import load_config, save_config, CONFIG_FILE
    from xuloom.observability.cost import pricing_path, save_pricing_settings, load_pricing_settings, ModelPrice
    from xuloom.core.model_catalog import connections
    source = Path(root) / 'restored-settings.json'
    restored = json.loads(source.read_text(encoding='utf-8'))
    saved = portable_config(restored['connections'])
    restored_prices = restored.get('pricing', {}).get('models', {})
    for key, value in restored_prices.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError('Invalid restored price name')
        ModelPrice.coerce(value)
    # Keep original configuration for user-directed rollback, including OS-encrypted credentials.
    rollback = Path(root) / 'maintenance' / ('settings-' + uuid.uuid4().hex)
    rollback.mkdir(parents=True)
    mapping = {}
    def merge(cfg):
        existing = connections(cfg, defaults=False)
        ids = {c['id'] for c in existing}
        for item in saved['model_connections']:
            matched = next((c for c in existing if c['id'] == item['id']), None)
            old_id = item['id']
            if matched and (matched.get('provider'), matched.get('base_url')) == (item.get('provider'), item.get('base_url')):
                # Keep the existing key, merge only the user's restored model list.
                matched['models'] = list(dict.fromkeys(matched.get('models', []) + item.get('models', [])))
            else:
                if old_id in ids:
                    item['id'] = 'restored-' + uuid.uuid4().hex[:12]
                item['api_key'] = ''
                existing.append(item)
                ids.add(item['id'])
            mapping[old_id] = item['id']
        cfg['model_connections'] = existing
        return cfg
    from dataclasses import asdict
    from xuloom.core.session_lock import SessionLock
    lease = SessionLock(CONFIG_FILE.parent, 'config-writer')
    lease.acquire()
    previous = {}
    try:
        for path in (CONFIG_FILE, pricing_path()):
            previous[path] = path.read_bytes() if path.exists() else None
            if path.exists():
                shutil.copy2(path, rollback / path.name)
        cfg = merge(load_config())
        prices, cap = load_pricing_settings()
        values = {k: asdict(v) for k, v in prices.items()}
        for key, value in restored_prices.items():
            if key.startswith('@') and '/' in key:
                connection, model = key[1:].split('/', 1)
                key = '@' + mapping.get(connection, connection) + '/' + model
            values.setdefault(key, value)
        save_config(cfg)
        save_pricing_settings(values, cap)
        os.replace(source, source.with_suffix('.imported.json'))
    except BaseException:
        from xuloom.core.storage import atomic_bytes
        for path, data in previous.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic_bytes(path, data)
        raise
    finally:
        lease.close()
    return {'connections': len(saved['model_connections']), 'rollback': str(rollback)}
