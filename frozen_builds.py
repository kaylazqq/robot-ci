"""Freeze branch tips at submission and never fall back to a different ref."""
import re


def resolve(url, branch, run_cmd, git_args, env):
    if not isinstance(branch, str) or not branch or branch.startswith('-') or any(c.isspace() for c in branch):
        raise ValueError('Invalid branch')
    ref = 'refs/heads/' + branch
    code, output = run_cmd(git_args('ls-remote', '--exit-code', url, ref), env=env, timeout=60)
    if code:
        raise ValueError('Cannot freeze branch: remote lookup failed')
    matches = [line.split() for line in output.splitlines() if line.split() and len(line.split()) == 2]
    if len(matches) != 1 or matches[0][1] != ref or not re.fullmatch(r'[0-9a-f]{40}', matches[0][0]):
        raise ValueError('Cannot freeze branch: missing or ambiguous full SHA')
    return matches[0][0]


def checkout(job_id, dest, url, sha, run_stream, run_cmd, git_args, env):
    if not re.fullmatch(r'[0-9a-f]{40}', sha or ''):
        return False, 'Frozen source SHA missing'
    commands = [git_args('init', str(dest)), git_args('-C', str(dest), 'remote', 'add', 'origin', url),
                git_args('-C', str(dest), 'fetch', '--depth', '1', 'origin', sha),
                git_args('-C', str(dest), 'checkout', '--detach', 'FETCH_HEAD')]
    for args in commands:
        if run_stream(job_id, args, env=env, timeout=600):
            return False, 'Frozen source fetch/checkout failed; no branch fallback'
    code, actual = run_cmd(git_args('-C', str(dest), 'rev-parse', 'HEAD'), env=env, timeout=30)
    if code or actual.strip() != sha:
        return False, 'Frozen source SHA mismatch'
    return True, str(dest)
