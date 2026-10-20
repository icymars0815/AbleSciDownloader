"""AI module (custom provider, key handling, AI on/off split), logging/redaction/report, disclaimer flags. No network."""
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app_info
import applog
from ai_service import AIService, PRESETS, validate_base_url
from app import make_server
from credential_vault import AIKeyVault

KEY = "fictional-test-key-ABCD1234"


class UrlRuleTests(unittest.TestCase):
    def test_https_and_localhost_rule(self):
        self.assertEqual("https://api.example.com/v1", validate_base_url("https://api.example.com/v1/"))
        self.assertEqual("https://api.example.com/v1", validate_base_url("https://api.example.com/v1/chat/completions"))
        for ok in ("http://localhost:11434/v1", "http://127.0.0.1:8000", "http://[::1]:8000/v1"):
            validate_base_url(ok)
        for bad in ("http://api.example.com/v1", "ftp://x.test", "", "https://", "https://u:p@api.example.com", "https://a.test/v1?key=1", "javascript:alert(1)", "http://localhost.evil.test"):
            with self.assertRaises(ValueError, msg=bad):
                validate_base_url(bad)

    def test_presets_only_deepseek_flash(self):
        self.assertEqual(["deepseek-flash"], list(PRESETS))
        service = AIService()
        with self.assertRaises(ValueError):
            service.configure(True, KEY, model="deepseek-v4-pro", consent=True)
        self.assertEqual(["deepseek-flash"], [p["id"] for p in service.public_config()["presets"]])

    def test_custom_requires_model_and_valid_url(self):
        service = AIService()
        with self.assertRaises(ValueError):
            service.configure(True, KEY, consent=True, provider="custom", base_url="http://api.example.com", model="m")
        with self.assertRaises(ValueError):
            service.configure(True, KEY, consent=True, provider="custom", base_url="https://api.example.com", model="")
        service.configure(True, KEY, consent=True, provider="custom", base_url="https://api.example.com/v1", model="my-model")
        cfg = service.public_config()
        self.assertTrue(cfg["enabled"])
        self.assertEqual("AI 已开启 · api.example.com/my-model", cfg["mode_label"])

    def test_off_label_and_hint(self):
        cfg = AIService().public_config()
        self.assertFalse(cfg["enabled"])
        self.assertEqual("本地模式 · AI 关闭", cfg["mode_label"])
        self.assertEqual("请先在设置中开启 AI 辅助", cfg["disabled_hint"])


class KeyHandlingTests(unittest.TestCase):
    def test_key_hint_only_last4_and_never_full_key(self):
        service = AIService()
        service.configure(True, KEY, consent=True)
        text = json.dumps(service.public_config(), ensure_ascii=False)
        self.assertNotIn(KEY, text)
        self.assertIn("已保存 ••••1234", text)

    @unittest.skipUnless(os.name == "nt", "DPAPI is Windows-only")
    def test_dpapi_roundtrip_and_separate_file(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = AIKeyVault(folder)
            service = AIService(vault)
            service.configure(True, KEY, consent=True, storage="dpapi")
            self.assertTrue((Path(folder) / "ai_key.dpapi").is_file())
            self.assertFalse((Path(folder) / "credentials.dpapi").exists())
            self.assertNotIn(KEY.encode(), (Path(folder) / "ai_key.dpapi").read_bytes())
            fresh = AIService(AIKeyVault(folder))
            fresh.restore(service.settings_dict())
            self.assertTrue(fresh.public_config()["enabled"])
            self.assertEqual(KEY, fresh.key)
            fresh.forget_key()
            self.assertFalse((Path(folder) / "ai_key.dpapi").exists())

    def test_memory_storage_removes_vault_file(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = AIKeyVault(folder)
            vault.file.write_bytes(b"x")
            service = AIService(vault)
            service.configure(True, KEY, consent=True, storage="memory")
            self.assertFalse(vault.file.exists())

    def test_connection_test_lists_models_only_and_sends_no_content(self):
        service = AIService()
        with patch("ai_service.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps({"data": [{"id": "m1"}, {"id": "m2"}]}).encode()
            result = service.test_connection(KEY, "custom", "https://api.example.com/v1", "m1")
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual("GET", request.get_method())
            self.assertIsNone(request.data)
            self.assertEqual("https://api.example.com/v1/models", request.full_url)
            self.assertEqual(["m1", "m2"], result["models"])
            self.assertNotIn(KEY, json.dumps(result))

    def test_key_only_goes_to_configured_host_and_redirect_refused(self):
        from ai_service import NoRedirect
        with self.assertRaises(ValueError):
            NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.test/")

    def test_no_auto_retry_on_error(self):
        from urllib.error import URLError
        service = AIService()
        service.configure(True, KEY, consent=True)
        with patch("ai_service.build_opener") as opener:
            opener.return_value.open.side_effect = URLError("down")
            with self.assertRaises(ValueError):
                service.analyze_report("report text", True)
            self.assertEqual(1, opener.return_value.open.call_count)

    def test_organize_text_needs_consent_and_drops_invented_doi(self):
        service = AIService()
        service.configure(True, KEY, consent=True)
        with self.assertRaises(ValueError):
            service.organize_text(["Some Title. Journal 2020"], False)
        reply = {"choices": [{"message": {"content": json.dumps({"records": [{"title": "Some Title", "authors": ["A B"], "doi": "10.9999/invented"}]})}}]}
        with patch("ai_service.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(reply).encode()
            result = service.organize_text(["Some Title. Journal 2020"], True)
            sent = opener.return_value.open.call_args.args[0].data.decode()
            self.assertNotIn(KEY, sent)
        self.assertEqual("", result["records"][0]["doi"])
        self.assertTrue(result["requires_manual_review"])


class RedactTests(unittest.TestCase):
    def test_cases(self):
        sample = ("mail me@example.com password=hunter22 \"password\": \"s3cret!\" key sk-abcdef1234567890 Authorization: Bearer abc.def-123456 "
                  "Cookie: sid=zzz; a=b\nSet-Cookie: x=y\n"
                  "https://x.test/a?token=TOK123&q=1&sig=SIG999&api_key=KK12345678 C:\\Users\\RealName\\Desktop\\a.pdf /Users/bob/x")
        out = applog.redact(sample)
        for leaked in ("me@example.com", "hunter22", "s3cret", "abcdef1234567890", "abc.def-123456", "sid=zzz", "x=y", "TOK123", "SIG999", "KK12345678", "RealName", "bob"):
            self.assertNotIn(leaked, out)
        self.assertIn("q=1", out)
        self.assertIn("C:\\Users\\<user>\\Desktop\\a.pdf", out)

    def test_keeps_request_ids_and_never_raises(self):
        self.assertIn("P7j5Qn", applog.redact("求助 id P7j5Qn 失败"))
        self.assertEqual("", applog.redact(None))


class LogTests(unittest.TestCase):
    def test_json_lines_with_redaction_and_stack(self):
        with tempfile.TemporaryDirectory() as folder:
            log = applog.AppLog(Path(folder) / "logs")
            try:
                raise RuntimeError("boom for person@example.com C:\\Users\\Zed\\f token=ABCDEFGH12345")
            except RuntimeError as exc:
                log.error("browser", "sync", exc, item_id="i1")
            files = list((Path(folder) / "logs").glob("kytdl-*.log"))
            self.assertEqual(1, len(files))
            row = json.loads(files[0].read_text("utf-8").splitlines()[0])
            self.assertEqual("error", row["result"])
            self.assertEqual("RuntimeError", row["error_type"])
            self.assertEqual(app_info.APP_VERSION, row["version"])
            text = json.dumps(row)
            for leaked in ("person@example.com", "Zed", "ABCDEFGH12345"):
                self.assertNotIn(leaked, text)
            self.assertTrue(any("test_ai_logging.py" in s for s in row["stack"]))

    def test_rotation_only_removes_own_old_files(self):
        with tempfile.TemporaryDirectory() as folder:
            logs = Path(folder) / "logs"
            logs.mkdir()
            today = datetime(2026, 10, 20, 12, 0)
            old = (today - timedelta(days=15)).strftime("kytdl-%Y%m%d.log")
            edge = (today - timedelta(days=14)).strftime("kytdl-%Y%m%d.log")
            for name in (old, edge, "notes.txt", "kytdl-old.log", "other-20200101.log", "kytdl-20200101.log.bak"):
                (logs / name).write_text("x")
            removed = applog.AppLog(logs, clock=lambda: today).rotate()
            self.assertEqual([old], removed)
            self.assertEqual({edge, "notes.txt", "kytdl-old.log", "other-20200101.log", "kytdl-20200101.log.bak"}, {p.name for p in logs.iterdir()})

    def test_write_triggers_rotation_once_per_day(self):
        with tempfile.TemporaryDirectory() as folder:
            logs = Path(folder) / "logs"
            logs.mkdir()
            (logs / "kytdl-20200101.log").write_text("x")
            applog.AppLog(logs).write("m", "a", "ok")
            self.assertFalse((logs / "kytdl-20200101.log").exists())


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(0, self.temp.name, start_monitor=False, auto_screen=False)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port
        html = urlopen(self.base).read().decode()
        self.token = html.split("const TOKEN=")[1].split(";let state=")[0].strip('"')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.agent.commands.put(("close", None, None))
        self.server.agent.thread.join(timeout=2)
        self.temp.cleanup()

    def post(self, route, payload=None):
        request = Request(self.base + "/api/" + route, json.dumps(payload or {}).encode(), method="POST",
                          headers={"Content-Type": "application/json", "Origin": self.base, "X-Demo-Token": self.token})
        return json.loads(urlopen(request).read())

    def state(self):
        return json.loads(urlopen(self.base + "/api/state").read())

    def test_ai_off_flags_and_key_not_in_state_events_logs(self):
        st = self.state()
        self.assertFalse(st["ai"]["enabled"])
        self.assertEqual("本地模式 · AI 关闭", st["ai"]["mode_label"])
        self.assertEqual("请先在设置中开启 AI 辅助", st["ai"]["disabled_hint"])
        self.post("ai/settings", {"enabled": True, "api_key": KEY, "provider": "custom", "base_url": "https://api.example.com/v1", "model": "m", "storage": "memory", "consent": True})
        st = self.state()
        self.assertTrue(st["ai"]["enabled"])
        self.assertTrue(st["ai"]["mode_label"].startswith("AI 已开启"))
        dump = json.dumps(st, ensure_ascii=False)
        self.assertNotIn(KEY, dump)
        self.assertIn("••••1234", dump)
        self.assertNotIn(KEY, json.dumps(self.server.store.snapshot()))
        self.server.store.log.write("ai", "x", "ok", message="Bearer " + KEY)
        logs = "".join(p.read_text("utf-8") for p in (Path(self.temp.name) / "logs").glob("*.log"))
        self.assertNotIn(KEY, logs)
        with patch("ai_service.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"data": []}'
            result = self.post("ai/test", {"api_key": "", "provider": "custom", "base_url": "https://api.example.com/v1"})
        self.assertTrue(result["ok"])
        self.assertNotIn(KEY, json.dumps(self.server.mcp_api.do_error_report({})))
        for ev in self.state()["events"]:
            self.assertNotIn(KEY, ev["message"])

    def test_ai_actions_refused_when_off(self):
        for route, body in (("ai/organize", {"segments": ["x"], "consent": True}), ("ai/recognize", {"id": "none", "pages": [0], "consent": True})):
            with self.assertRaises(HTTPError) as caught:
                self.post(route, body)
            self.assertEqual(400, caught.exception.code)

    def test_parse_returns_unparsed_and_import_ai_records_validated(self):
        parsed = self.post("parse", {"text": "10.1234/abc.def\n\nJust some words without anything useful here"})
        self.assertIn("unparsed", parsed)
        self.assertFalse(parsed["ai_used"])
        before = len(self.server.store.snapshot()["items"])
        self.post("import", {"records": [{"title": "AI Title", "authors": ["A B"], "doi": "10.1234/ai.1"}], "ai_assisted": True})
        items = self.server.store.snapshot()["items"]
        self.assertEqual(before + 1, len(items))
        self.assertEqual("AI整理", items[-1].get("import_format"))

    def test_report_is_redacted_and_ai_analysis_needs_preview_and_ai(self):
        self.server.store.log.write("browser", "sync", "error", "RuntimeError", "failed for me@example.com at C:\\Users\\Zed\\x token=ABCDEFGH12345")
        self.server.store.event("事件 password=hunter22 someone@example.org")
        errors = self.post("logs/recent")["errors"]
        self.assertEqual(1, len(errors))
        report = self.post("report/build", {"error_ids": [errors[0]["id"]]})
        for leaked in ("me@example.com", "Zed", "ABCDEFGH12345", "hunter22", "someone@example.org", self.temp.name):
            self.assertNotIn(leaked, report["text"])
        self.assertIn("## 环境", report["text"])
        with self.assertRaises(HTTPError):
            self.post("report/analyze", {"report_id": "wrong", "consent": True})
        with self.assertRaises(HTTPError):   # AI is off
            self.post("report/analyze", {"report_id": report["report_id"], "consent": True})

    def test_mcp_error_report_has_real_markdown(self):
        self.server.store.log.write("pipeline", "tick", "error", "ValueError", "bad me@example.com")
        out = self.server.mcp_api.do_error_report({})
        self.assertTrue(out["redacted"])
        self.assertIn("# 科研通下载器 错误报告", out["report_markdown"])
        self.assertNotIn("me@example.com", json.dumps(out, ensure_ascii=False))

    def test_first_run_flag_and_ack(self):
        about = self.state()["about"]
        self.assertTrue(about["disclaimer_pending"])
        self.post("preferences", {"disclaimer_ack": True})
        self.assertFalse(self.state()["about"]["disclaimer_pending"])
        with self.assertRaises(HTTPError):
            self.post("preferences", {"disclaimer_ack": False})

    def test_existing_install_not_forced(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "queue.json").write_text(json.dumps({"items": [], "events": []}))
            server = make_server(0, folder, start_monitor=False, auto_screen=False)
            try:
                self.assertEqual("upgrade", server.store.snapshot()["preferences"]["disclaimer_ack"])
            finally:
                server.server_close()
                server.agent.commands.put(("close", None, None))
                server.agent.thread.join(timeout=2)

    def test_github_button_disabled_without_repo_url(self):
        with patch.object(app_info, "REPO_URL", ""):
            self.assertFalse(self.state()["about"]["issue_enabled"])
            with self.assertRaises(HTTPError):
                self.post("report/issue")
        self.assertIsNone(applog.issue_url("", "t", "b"))
        self.assertIsNone(applog.issue_url("https://evil.test/x/y", "t", "b"))
        with patch.object(app_info, "REPO_URL", "https://github.com/u/r"):
            self.assertTrue(self.state()["about"]["issue_enabled"])
            url = self.post("report/issue")["url"]
            self.assertTrue(url.startswith("https://github.com/u/r/issues/new?title="))

    def test_front_end_declares_ai_off_hint_and_texts(self):
        js = (Path(__file__).resolve().parent.parent / "app.js").read_text("utf-8")
        for needle in ("请先在设置中开启 AI 辅助", "通过 Claude Code、Codex 等外部工具使用时，无需在此填写 AI 密钥", "密钥保存方式",
                       "每个账户须为本人注册，不得替他人求助", "我已了解"):
            self.assertIn(needle, js)
        html = (Path(__file__).resolve().parent.parent / "index.html").read_text("utf-8")
        self.assertIn('id="aiOrganizeBtn" disabled', html)


if __name__ == "__main__":
    unittest.main()
