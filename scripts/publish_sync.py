"""Publish a sync from a fresh clone, retrying on concurrent remote updates."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def command(args, cwd, env, check=True):
    result = subprocess.run(args, cwd=cwd, env=env, text=True, encoding='utf-8', errors='replace', capture_output=True)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'Command failed')
    return result


def publish(repository, attempts=3):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid repository name')
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', GIT_TERMINAL_PROMPT='0')
    if env.get('GITHUB_TOKEN'):
        credential = base64.b64encode(('x-access-token:' + env['GITHUB_TOKEN']).encode()).decode()
        env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                   GIT_CONFIG_VALUE_0='AUTHORIZATION: basic ' + credential)
    remote = 'https://github.com/' + repository + '.git'
    for attempt in range(1, attempts + 1):
        with tempfile.TemporaryDirectory(prefix='visual-library-publish-') as directory:
            base_dir = Path(directory).resolve()
            checkout = base_dir / 'repo'
            command(['git', 'clone', '--depth', '1', '--branch', 'main', remote, str(checkout)], base_dir, env)
            base = command(['git', 'rev-parse', 'HEAD'], checkout, env).stdout.strip()
            outcome = command([sys.executable, '-B', 'scripts/sync_upstream.py'], checkout, env, check=False)
            if outcome.returncode:
                raise RuntimeError('Sync incomplete; remote left unchanged. ' + outcome.stdout[-1500:] + outcome.stderr[-1000:])
            source_result = json.loads(outcome.stdout or '{}')
            if source_result.get('status') == 'PARTIAL':
                print('Partial sync: saved available resources; missing resources remain pending.', flush=True)
            command([sys.executable, '-B', 'scripts/library.py', 'validate'], checkout, env)
            command([sys.executable, '-B', 'scripts/validate_archive.py'], checkout, env)
            changed = command(['git', 'status', '--porcelain'], checkout, env).stdout.strip()
            if not changed:
                print(json.dumps({'status': 'UNCHANGED', 'attempt': attempt}))
                return
            command(['git', 'config', 'user.name', 'github-actions[bot]'], checkout, env)
            command(['git', 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com'], checkout, env)
            command(['git', 'add', '--', 'cases', 'images', 'indexes', 'sources'], checkout, env)
            command(['git', 'commit', '-m', 'Sync visual reference archive'], checkout, env)
            pushed = command(['git', 'push', 'origin', 'HEAD:refs/heads/main'], checkout, env, check=False)
            if pushed.returncode == 0:
                print(json.dumps({'status': 'PARTIAL_PUSHED' if source_result.get('status') == 'PARTIAL' else 'PUSHED', 'attempt': attempt, 'base': base}))
                return
            # Never force-push or rebase generated case versions. Recompute from the new base.
            current = command(['git', 'ls-remote', 'origin', 'refs/heads/main'], checkout, env).stdout.split()[0]
            if current == base:
                raise RuntimeError('Push failed without a competing commit: ' + pushed.stderr)
            print('Concurrent update detected; recomputing from latest remote.', flush=True)
    raise RuntimeError('Remote changed repeatedly; retry later. No remote content was overwritten.')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY'))
    args = p.parse_args()
    if not args.repository:
        p.error('--repository is required')
    try:
        publish(args.repository)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
