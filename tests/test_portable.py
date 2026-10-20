"""Portable runtime selection, tested without real sessions or downloads."""
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path
from runtime_paths import python_executable, bundled_browser, browser_options


class PortableRuntimeTests(unittest.TestCase):
    def test_prepared_browser_skips_network(self):
        from portable_bootstrap import prepare
        with patch('portable_bootstrap.bundled_browser', return_value=Path('existing.exe')):
            prepare(runner=lambda *a, **k: self.fail('No download should occur'))

    def test_first_launch_prepares_only_a_local_browser(self):
        from portable_bootstrap import prepare
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch('portable_bootstrap.bundled_browser', side_effect=[None, Path('ready.exe')]):
                def run(args, env, cwd):
                    self.assertEqual(str(root / 'runtime/python/python.exe'), args[0])
                    self.assertEqual(str(root / 'runtime/browsers'), env['PLAYWRIGHT_BROWSERS_PATH'])
                    self.assertIn('--no-shell', args)
                    self.assertEqual(root, cwd)
                    return SimpleNamespace(returncode=0)
                prepare(root, runner=run)

    def test_failed_prepare_does_not_open_app_or_clear_data(self):
        from portable_bootstrap import prepare
        with patch('portable_bootstrap.bundled_browser', return_value=None):
            with self.assertRaises(RuntimeError):
                prepare(runner=lambda *a, **k: SimpleNamespace(returncode=1))

    def test_development_paths_stay_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(root / '.venv/Scripts/python.exe', python_executable(root))
            self.assertEqual({'channel': 'chrome'}, browser_options(root))
            self.assertIsNone(bundled_browser(root))

    def test_portable_paths_support_spaces_and_unicode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / '便携目录 with spaces'
            for name in ('python.exe', 'pythonw.exe'):
                p = root / 'runtime/python' / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.touch()
            browser = root / 'runtime/browsers/chromium-1234/chrome-win64/chrome.exe'
            browser.parent.mkdir(parents=True)
            browser.touch()
            self.assertEqual(root / 'runtime/python/pythonw.exe', python_executable(root, True))
            self.assertEqual(root / 'runtime/python/python.exe', python_executable(root))
            self.assertEqual(browser, bundled_browser(root))
            self.assertEqual({'executable_path': str(browser)}, browser_options(root))
