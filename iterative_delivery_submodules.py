"""Preserve absent sibling gitlinks without initializing unrelated projects."""


def gitlinks(root):
    from iterative_delivery import git
    result = {}
    for entry in git(root, 'ls-files', '--stage', '-z').split('\0'):
        if not entry: continue
        fields, name = entry.split('\t', 1)
        mode, commit, stage = fields.split()
        if mode == '160000':
            if stage != '0': raise RuntimeError('DELIVERY_SUBMODULE_CONFLICT: ' + name)
            result[name] = commit
    return result


def preserve_absent_gitlink(source, root, name, expected):
    from iterative_delivery import contained, git
    path = source / name
    if not contained(source, path) or path.is_symlink():
        raise RuntimeError('DELIVERY_INVALID_PATH: ' + name)
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise RuntimeError('DELIVERY_SUBMODULE_REQUIRES_MATERIALIZATION: ' + name)
    # The superproject commit retains the exact gitlink. An absent sibling is
    # a valid source input; no remote fetch or unrelated submodule init occurs.
    git(root, 'update-index', '--add', '--cacheinfo', f'160000,{expected},{name}')


def validate_gitlinks(root, expected):
    if gitlinks(root) != expected:
        raise RuntimeError('DELIVERY_SUBMODULE_COMMIT_CHANGED: preserve the verified gitlinks')
