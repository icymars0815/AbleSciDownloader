"""Offline smoke test using only the packaged Python and Chromium, on a random port.
No real accounts, downloads, AI calls, Startup shortcuts or public services.
Writes evidence outside the package so no test data enter the portable ZIP.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import threading
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def mcp_check(base):
    import os
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = dict(os.environ, ABLESCI_LOCAL_URL=base)
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / 'mcp_server.py')], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            assert any(t.name == 'workbench_status' for t in result.tools)
            state = await session.call_tool('workbench_status', {})
            assert not state.isError
            return len(result.tools)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    evidence = args.output.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    import app
    import core
    import runtime_paths
    from credential_vault import protect
    from playwright.sync_api import sync_playwright
    from kytdl_client import Client
    assert Path(sys.executable).resolve() == ROOT / 'runtime/python/python.exe'
    browser_exe = runtime_paths.bundled_browser()
    assert browser_exe and browser_exe.is_relative_to(ROOT / 'runtime')
    assert protect(protect(b'fictional-local-smoke-value'), decrypt=True) == b'fictional-local-smoke-value'
    server = app.make_server(0, data_root=evidence / 'test-data', start_monitor=False, auto_screen=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        core.seed_demo(server.store)
        c = Client(base)
        state = c.get('/api/state')
        assert not state['ai']['enabled']
        assert len(state['items']) == 3
        item = next(i for i in state['items'] if i.get('demo_name') == 'matching')
        c.post('/api/verify', {'id':item['id'], 'allow_website_actions':False})
        config = c.get('/api/mcp-config')
        assert config['claude']['mcpServers']['kytdl']['command'] == str(python_executable_path := ROOT / 'runtime/python/python.exe')
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=str(browser_exe), headless=True)
            browser_version = browser.version
            context = browser.new_context(viewport={'width':1440, 'height':1000})
            context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base + '/') else route.abort())
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(base, wait_until='networkidle')
            assert page.title() == '科研通下载器'
            assert page.get_by_role('heading', name='科研通下载器', exact=True).count() == 1
            assert not errors, errors
            page.screenshot(path=str(evidence / 'portable-ui.png'), full_page=False)
            # Exercise the new settings with a fictional key, entirely on loopback.
            page.get_by_role('button', name='我已了解', exact=True).click()
            page.locator('#settingsBtn').click()
            page.locator('#tab-download').click()
            key = page.locator('#openalexKey')
            key.wait_for(state='visible')
            assert key.get_attribute('type') == 'password'
            key.fill('fictional-openalex-smoke-key')
            with page.expect_response(lambda r: r.url == base + '/api/openalex-key') as saved:
                page.locator('[data-action="openalex-key-save"]').click()
            assert saved.value.status == 200
            page.wait_for_function("document.querySelector('#openalexKey').value === '' && !document.querySelector('[data-action=\"openalex-key-clear\"]').disabled")
            public_state = c.get('/api/state')
            assert public_state['openalex_key_saved']
            assert 'fictional-openalex-smoke-key' not in json.dumps(public_state)
            page.locator('#openalexKey').scroll_into_view_if_needed()
            page.screenshot(path=str(evidence / 'portable-openalex-settings.png'), full_page=False)
            with page.expect_response(lambda r: r.url == base + '/api/openalex-key-clear') as cleared:
                page.locator('[data-action="openalex-key-clear"]').click()
            assert cleared.value.status == 200
            assert not c.get('/api/state')['openalex_key_saved']
            assert not errors, errors
            browser.close()
        tools = asyncio.run(mcp_check(base))
        result = {'result':'PASS', 'python':sys.version, 'python_executable':str(sys.executable),
                  'browser':str(browser_exe.relative_to(ROOT)), 'browser_version':browser_version,
                  'checks':['native imports','DPAPI roundtrip','isolated API','fictional PDF verification',
                            'offline UI','OpenAlex key settings save/clear and privacy','JavaScript errors absent','bundled Chromium','stdio MCP handshake'],
                  'mcp_tools':tools, 'real_site_actions':False, 'fresh_windows_vm_tested':False}
        (evidence / 'SMOKE_RESULT.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps({'result':'PASS','mcp_tools':tools,'browser_version':browser_version}),flush=True)
    finally:
        server.pipeline.close()
        server.monitor.close()
        server.agent.commands.put(('close', None, None))
        server.agent.thread.join(timeout=10)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == '__main__':
    main()
