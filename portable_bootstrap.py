"""Prepare the official Playwright browser locally once, then open the app.
The portable ZIP already contains Python and all Python dependencies.
No admin rights, system installation, account access or credential handling.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys

from runtime_paths import ROOT, bundled_browser


def prepare(root=ROOT, runner=subprocess.run):
    root = Path(root)
    if bundled_browser(root):
        return
    print('首次准备：正在从 Playwright 官方下载浏览器到本目录。无需管理员权限。', flush=True)
    print('这一步约需下载 200 MB，请保持网络连接；完成后自动打开 App。', flush=True)
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(root / 'runtime/browsers'))
    result = runner([str(root / 'runtime/python/python.exe'), '-m', 'playwright', 'install',
                     '--no-shell', '--no-remove', 'chromium'], env=env, cwd=root)
    if result.returncode or not bundled_browser(root):
        raise RuntimeError('浏览器准备未完成。请检查网络和文件夹写入权限，然后重新双击启动；已保存数据不会被清理。')


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--prepare-only', action='store_true')
    args = p.parse_args(argv)
    try:
        prepare()
        if not args.prepare_only:
            import launcher
            launcher.main([])
        return 0
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
