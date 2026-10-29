"""Build a clean Windows x64 portable ZIP from an explicit public source tree.

Run with the development Python. No user data/profiles are read or copied.
Each invocation requires a new output directory; it never cleans previous builds.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PYTHON_URL = 'https://www.python.org/ftp/python/3.13.16/python-3.13.16-embeddable-amd64.zip'
PYTHON_SHA256 = '589dc1e9d02549ca5f680710307ff9b77f6e3ffc4aa3efccd7fa54eeeafac94b'
OMIT = {'data', '.venv', '.git', 'runtime', 'build', 'dist', '__pycache__'}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def source_files(ref):
    raw = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', '-z', ref], cwd=ROOT)
    names = set(raw.decode('utf-8').strip('\0').split('\0'))
    for name in sorted(names):
        path = Path(name)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('Invalid source path')
        if set(path.parts) & OMIT or path.suffix in {'.log', '.pyc'}:
            continue
        if not (ROOT / path).is_file():
            raise FileNotFoundError(path)
        yield path


def copy_sources(ref, folder):
    """Read committed bytes, so local edits cannot slip into a tagged release."""
    raw = subprocess.check_output(['git', 'archive', '--format=zip', ref], cwd=ROOT)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for rel in source_files(ref):
            dest = folder / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(archive.read(rel.as_posix()))


def run(*args, env=None):
    subprocess.run(list(map(str, args)), cwd=ROOT, env=env, check=True)


def write_zip(folder, target):
    with zipfile.ZipFile(target, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for file in sorted(folder.rglob('*')):
            if file.is_file():
                out.write(file, Path(folder.name) / file.relative_to(folder))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--source-ref', required=True, help='Public commit used as source allowlist')
    p.add_argument('--output', required=True, type=Path, help='New directory, never overwritten')
    p.add_argument('--cache', type=Path, default=ROOT / 'build/cache')
    p.add_argument('--version', default='0.10.0')
    args = p.parse_args()
    if sys.platform != 'win32':
        raise SystemExit('Build this artifact on Windows x64')
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    cache = args.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    folder = out / f'AbleSciDownloader-{args.version}-windows-x64'
    folder.mkdir()
    copy_sources(args.source_ref, folder)
    source_zip = out / f'AbleSciDownloader-{args.version}-source.zip'
    write_zip(folder, source_zip)
    runtime = folder / 'runtime/python'
    runtime.mkdir(parents=True)
    archive = cache / 'python-3.13.16-embed-amd64.zip'
    if not archive.exists():
        with urlopen(PYTHON_URL, timeout=180) as r:
            archive.write_bytes(r.read())
    if digest(archive) != PYTHON_SHA256:
        raise ValueError('Python SHA256 mismatch; refusing build')
    with zipfile.ZipFile(archive) as z:
        z.extractall(runtime)
    packages = runtime / 'Lib/site-packages'
    packages.mkdir(parents=True)
    wheels = cache / 'wheels'
    wheels.mkdir(exist_ok=True)
    run(sys.executable, '-m', 'pip', 'download', '--only-binary=:all:', '--platform', 'win_amd64',
        '--python-version', '3.13', '--implementation', 'cp', '--dest', wheels,
        '-r', ROOT / 'requirements-lock.txt')
    run(sys.executable, '-m', 'pip', 'install', '--no-index', '--find-links', wheels, '--only-binary=:all:',
        '--platform', 'win_amd64', '--python-version', '3.13', '--implementation', 'cp', '--no-compile',
        '--target', packages, '-r', ROOT / 'requirements-lock.txt')
    (runtime / 'python313._pth').write_text('python313.zip\n.\nLib\\site-packages\nimport site\n', encoding='utf-8')
    (runtime / 'sitecustomize.py').write_text(
        'import site, sys\nfrom pathlib import Path\n'
        'sys.path.insert(0, str(Path(__file__).resolve().parents[2]))\n'
        'site.addsitedir(str(Path(__file__).resolve().parent / "Lib" / "site-packages"))\n', encoding='utf-8')
    for module in ['app', 'mcp_server', 'pymupdf', 'pystray', 'win32crypt', 'pythoncom', 'win32com.client']:
        run(runtime / 'python.exe', '-c', f'import {module}; print("import OK: {module}")')
    licenses = folder / 'THIRD_PARTY_LICENSES'
    licenses.mkdir()
    shutil.copy2(runtime / 'LICENSE.txt', licenses / 'Python-LICENSE.txt')
    (folder / 'START_HERE.txt').write_text(
        '科研通下载器 · Windows 10/11 x64 免安装版\n\n'
        '1. 先完整解压 ZIP 到可写文件夹，不要直接在压缩包内运行。\n'
        '2. 双击“启动科研通下载器.cmd”。首次自动从官方获取约 200 MB 浏览器；无需安装 Python、Chrome 或管理员权限。\n'
        '3. 首次运行阅读声明，再登录你本人的科研通账户。验证码/微信验证由你完成。\n'
        '4. 首次准备及登录、下载需联网；之后不再重复下载浏览器。\n'
        '5. 文件、账户会话和设置均在 data/。退出后台后可移动整个目录。\n'
        '6. 更新请解压到新目录，退出旧版后自行备份/迁移 data/，不要覆盖运行中的旧版。\n'
        '7. 本软件没有代码签名，Windows 可能显示来源提示；请核对官方 Release 和 SHA256，勿禁用系统保护。\n'
        '8. Windows ARM64 / 32 位 / 干净虚拟机尚未验收。本包仅发布 x64。\n\n'
        '详细说明：docs/PORTABLE.md；许可：LICENSE；声明：DISCLAIMER.md。\n', encoding='utf-8-sig')
    manifest = {'version':args.version, 'source_allowlist_ref':args.source_ref,
                'python_url':PYTHON_URL, 'python_sha256':PYTHON_SHA256,
                'browser_provisioning':'Official Playwright download at first launch; not redistributed in ZIP',
                'files':[{ 'path':str(x.relative_to(folder)).replace('\\','/'), 'sha256':digest(x)}
                         for x in sorted(folder.rglob('*')) if x.is_file()]}
    (folder / 'BUILD_MANIFEST.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    (out / 'BUILD_READY.json').write_text(json.dumps({'folder':str(folder),'source_zip':str(source_zip)}),encoding='utf-8')
    print('Build staged. Run packaging/smoke_portable.py before archiving and publishing: ' + str(folder), flush=True)


if __name__ == '__main__':
    main()
