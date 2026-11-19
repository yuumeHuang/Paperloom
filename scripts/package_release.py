"""Export a standalone project, or package a clean reader deployment."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def distribution_files():
    manifest = ROOT / 'scripts/github-files.txt'
    paths = []
    for line in manifest.read_text(encoding='utf-8').splitlines():
        name = line.strip()
        if not name or name.startswith('#'):
            continue
        relative = Path(name)
        source = ROOT / relative
        if (relative.anchor or '..' in relative.parts or '.git' in relative.parts
                or source.resolve() != source or not source.is_file()):
            raise SystemExit(f'Invalid source distribution entry: {name}')
        paths.append(relative)
    if not paths or len(paths) != len(set(paths)):
        raise SystemExit('The source distribution manifest is empty or contains duplicate paths')
    return paths


def zip_sources(target, root, paths, mode):
    with zipfile.ZipFile(target, mode, zipfile.ZIP_DEFLATED) as archive:
        for relative in paths:
            archive.write(root / relative, 'Paperloom/' + relative.as_posix())


def export_github(destination, paths):
    destination = destination.resolve()
    archive = destination.parent / (destination.name + '.zip')
    if destination.exists() or archive.exists():
        raise SystemExit('Destination or its ZIP already exists; choose a new directory name')
    destination.mkdir(parents=True)
    for relative in paths:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    zip_sources(archive, destination, paths, 'x')
    return {'directory': str(destination), 'zip': str(archive), 'files': len(paths)}


def package_site(out):
    web = ROOT / 'web'
    dirty = subprocess.check_output(['git', '-C', str(web), 'status', '--porcelain'], text=True).strip()
    if dirty:
        raise SystemExit('Reader source differs from the pushed commit; synchronize before packaging')
    sha = subprocess.check_output(['git', '-C', str(web), 'rev-parse', 'HEAD'], text=True).strip()
    manifest = json.loads((web / '.openai/hosting.json').read_text(encoding='utf-8'))
    if manifest.get('static', {}).get('directory') != 'dist':
        raise SystemExit('Static directory must be dist')
    archive_path = out / 'paperloom-reader.tar.gz'
    with tarfile.open(archive_path, 'w:gz') as archive:
        for file in sorted((web / 'dist').rglob('*')):
            if file.is_symlink(): raise SystemExit('Symlink in reader build')
            if file.is_file(): archive.add(file, arcname='dist/' + file.relative_to(web / 'dist').as_posix())
        archive.add(web / '.openai/hosting.json', arcname='dist/.openai/hosting.json')
    with tarfile.open(archive_path) as archive:
        names = archive.getnames()
        assert 'dist/index.html' in names and 'dist/.openai/hosting.json' in names
    return {'project_id': manifest['project_id'], 'commit_sha': sha, 'archive': str(archive_path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--github-dir', type=Path, help='write a new standalone directory and ZIP; does not contact GitHub')
    args = parser.parse_args()
    paths = distribution_files()
    if args.github_dir:
        result = export_github(args.github_dir, paths)
    else:
        out = ROOT / 'output'
        out.mkdir(exist_ok=True)
        result = package_site(out) if (ROOT / 'web/.openai/hosting.json').is_file() else {}
        version = json.loads((ROOT / 'mobile/package.json').read_text(encoding='utf-8'))['version']
        distribution = out / f'Paperloom-{version}.zip'
        zip_sources(distribution, ROOT, paths, 'w')
        result['source_zip'] = str(distribution)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__': main()
