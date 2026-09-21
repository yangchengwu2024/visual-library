"""Launch the installed read-only MCP against a verified personal-repo snapshot."""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import library
import validate_archive

DEFAULT_REPOSITORY = 'https://github.com/yangchengwu2024/visual-library.git'


def git(*arguments, timeout=120):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    result = subprocess.run(['git', '-c', 'core.longpaths=true', *map(str, arguments)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                            timeout=timeout, check=True)
    return result.stdout.decode('utf-8').strip()


def verify_snapshot(path, commit):
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Invalid snapshot commit')
    if git('-C', path, 'rev-parse', 'HEAD') != commit:
        raise ValueError('Snapshot HEAD changed')
    if git('-C', path, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('Snapshot has local modifications; refusing to serve it')
    result = library.validate(path)
    archived = validate_archive.validate(path)
    if not result['ok'] or archived['status'] != 'PASS':
        raise ValueError('Snapshot archive validation failed')


def attach_auto_refresh(snapshot, cache_root, repository_url):
    """Refresh data and reload the installed library when deployment markers change.

    The MCP process remains alive so the Codex connection does not need a manual
    restart after a normal deployment. A marker is changed only after the remote
    commit and verified cache are ready; a mismatch therefore fails visibly
    instead of silently serving an old snapshot.
    """
    install_root = Path(__file__).resolve().parents[1]
    data_marker = install_root / 'installed-commit.txt'
    code_marker = install_root / 'installed-code-revision.txt'

    def read_marker(path, fallback=''):
        try:
            return path.read_text(encoding='utf-8').strip()
        except OSError:
            return fallback

    snapshot.set_code_revision(read_marker(code_marker, read_marker(data_marker, 'unknown')))

    def refresh(current):
        desired_data = read_marker(data_marker, current.commit)
        desired_code = read_marker(code_marker, desired_data)
        data_stale = desired_data and desired_data.lower() != current.commit
        code_stale = desired_code and desired_code != current.code_revision
        if data_stale:
            refreshed, commit, status = prepare_snapshot(cache_root, repository_url, offline=False)
            if commit.lower() != desired_data.lower():
                raise RuntimeError('Deployment marker is not available on the configured remote main: ' + desired_data)
            current.replace_snapshot(refreshed, commit, status)
        if code_stale:
            importlib.reload(library)
        if data_stale or code_stale:
            current.set_code_revision(desired_code)

    snapshot.set_refresh_callback(refresh)


def prepare_snapshot(cache_root, repository_url=DEFAULT_REPOSITORY, *, offline=False, seed=None):
    """Only mutate our cache. Never pull/reset a user's working checkout."""
    key = hashlib.sha256(repository_url.encode()).hexdigest()[:16]
    cache = Path(cache_root).resolve() / key
    with library.library_lock(cache, timeout=150):
        mirror = cache / 'mirror.git'
        marker = cache / 'last-good.json'
        status = 'fresh'
        if offline:
            status = 'cached-offline'
        else:
            try:
                if not mirror.exists():
                    git('clone', '--bare', '--no-hardlinks', seed or repository_url, mirror)
                    if seed:
                        git('--git-dir', mirror, 'remote', 'set-url', 'origin', repository_url)
                actual = git('--git-dir', mirror, 'remote', 'get-url', 'origin')
                if actual != repository_url:
                    raise ValueError('Cache origin does not match configured personal repository')
                git('--git-dir', mirror, 'fetch', '--no-tags', 'origin',
                    '+refs/heads/main:refs/heads/main', timeout=45)
            except (subprocess.SubprocessError, OSError):
                status = 'cached-offline'
                print('Personal repository refresh failed; trying last verified snapshot.', file=sys.stderr)
        if status == 'cached-offline':
            if not marker.is_file():
                raise RuntimeError('No verified cached snapshot available. Connect to your GitHub repository first.')
            saved = json.loads(marker.read_text(encoding='utf-8'))
            if saved['repository_url'] != repository_url:
                raise ValueError('Cached repository identity mismatch')
            commit = saved['commit']
            if not re.fullmatch(r'[0-9a-f]{40}', commit):
                raise ValueError('Invalid cached commit')
        else:
            commit = git('--git-dir', mirror, 'rev-parse', 'refs/heads/main')
        snapshot = cache / 'snapshots' / commit
        if not snapshot.exists():
            if status == 'cached-offline':
                raise RuntimeError('Previously verified snapshot is missing')
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            git('--git-dir', mirror, 'worktree', 'add', '--detach', snapshot, commit)
        verify_snapshot(snapshot, commit)
        if status == 'fresh':
            value = {'repository_url': repository_url, 'commit': commit}
            temporary = marker.with_suffix('.tmp')
            temporary.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
            temporary.replace(marker)
        return snapshot, commit, status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository-url', default=DEFAULT_REPOSITORY)
    parser.add_argument('--cache-root', type=Path, default=Path.home() / '.cache' / 'visual-library-mcp')
    parser.add_argument('--offline', action='store_true', help='Use the last verified snapshot, explicitly marked cached-offline')
    parser.add_argument('--prepare-only', action='store_true', help='Prime/verify cache and print its status without starting MCP')
    parser.add_argument('--seed', type=Path, help='Optional existing clone for the initial object copy; always fetch the configured origin')
    args = parser.parse_args()
    snapshot, commit, status = prepare_snapshot(args.cache_root, args.repository_url, offline=args.offline, seed=args.seed)
    if args.prepare_only:
        print(json.dumps({'snapshot': str(snapshot), 'commit': commit, 'snapshot_status': status}))
        return
    # Always run the installed code, never code from the downloaded data snapshot.
    from mcp_library import create_server
    server = create_server(
        snapshot,
        commit,
        args.repository_url.removesuffix('.git'),
        snapshot_status=status,
        snapshot_setup=lambda current: attach_auto_refresh(current, args.cache_root, args.repository_url),
    )
    server.run(transport='stdio')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'Visual Library MCP startup failed: {exc}', file=sys.stderr)
        raise SystemExit(1)
