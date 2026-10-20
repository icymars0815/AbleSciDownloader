"""Smoke-test, seal and ZIP a staged portable build; never overwrite releases."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_portable import digest, source_files, ROOT


def zip_files(root, paths, dest):
    with zipfile.ZipFile(dest, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for rel in sorted(paths):
            out.write(root / rel, Path(dest.stem) / rel)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--folder', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--source-ref', required=True)
    args = p.parse_args()
    folder = args.folder.resolve()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    if (folder / 'data').exists():
        raise ValueError('Runtime data found in package; refusing release')
    # Refresh only the explicit public source allowlist, including final build tools/docs.
    import shutil
    for rel in source_files(args.source_ref):
        dest = folder / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)
    (folder / 'START_HERE.txt').write_text(
        '科研通下载器 · Windows x64 免安装版\n\n'
        '完整解压后，双击“启动科研通下载器.cmd”。\n'
        '包内已有 Python 和依赖；首次自动从官方获取约 200 MB 浏览器，需要联网。\n'
        '准备完成自动打开 App；无需管理员权限、安装 Python/Chrome 或输入命令。\n'
        '数据保存在 data/；更新前先退出并备份，不覆盖运行中的旧版。\n'
        '首次准备失败时检查网络后重新双击；不要禁用系统防护。\n'
        '详细使用、更新、系统支持及签名说明见 docs/PORTABLE.md。\n',encoding='utf-8-sig')
    python = folder / 'runtime/python/python.exe'
    env = dict(os.environ)
    env.pop('PYTHONHOME', None)
    env.pop('PYTHONPATH', None)
    env['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
    subprocess.run([str(python), str(folder / 'portable_bootstrap.py'), '--prepare-only'], check=True, env=env, cwd=out)
    subprocess.run([str(python), str(folder / 'packaging/smoke_portable.py'), '--output', str(out / 'smoke')],
                   check=True, env=env, cwd=out)
    names = [f.relative_to(folder) for f in folder.rglob('*')
             if f.is_file() and '__pycache__' not in f.parts and f.suffix != '.pyc'
             and f.name != 'BUILD_MANIFEST.json' and not f.is_relative_to(folder / 'runtime/browsers')
             and f.name != 'Chromium-Credits.html']
    old = json.loads((folder / 'BUILD_MANIFEST.json').read_text(encoding='utf-8'))
    old.pop('chromium_url', None)
    old.pop('chromium_sha256', None)
    old['browser_provisioning'] = 'Official Playwright download at first launch; browser binaries excluded from ZIP'
    old.update(source_commit=args.source_ref, files=[{'path':str(r).replace('\\','/'), 'sha256':digest(folder/r)} for r in sorted(names)])
    (folder / 'BUILD_MANIFEST.json').write_text(json.dumps(old,indent=2),encoding='utf-8')
    zip_path = out / (folder.name + '.zip')
    zip_files(folder, names + [Path('BUILD_MANIFEST.json')], zip_path)
    source_zip = out / (folder.name.replace('-windows-x64', '-source') + '.zip')
    zip_files(folder, list(source_files(args.source_ref)), source_zip)
    sums = '\n'.join(digest(p) + '  ' + p.name for p in [zip_path, source_zip]) + '\n'
    (out / 'SHA256SUMS.txt').write_text(sums,encoding='ascii')
    # Check the actual ZIP structure, CRCs, absence of personal data and native runtime.
    with zipfile.ZipFile(zip_path) as z:
        assert z.testzip() is None
        members = [Path(p) for p in z.namelist()]
        for rel in members:
            assert not ({'data','.git','.venv','__pycache__'} & set(rel.parts)), rel
        assert any(str(p).endswith('python.exe') for p in members)
        assert not any(str(p).endswith('chrome.exe') for p in members)
        assert any('portable_bootstrap.py' in str(p) for p in members)
    print(json.dumps({'portable_zip':str(zip_path),'bytes':zip_path.stat().st_size,'sha256':digest(zip_path)}),flush=True)


if __name__ == '__main__':
    main()
