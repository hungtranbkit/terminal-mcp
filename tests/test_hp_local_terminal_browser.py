"""Actual Chromium, real HTML, synthetic create receipt; never opens a host shell."""
import os
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from terminal_mcp.dashboard import NODES_ADMIN_HTML, SESSIONS_ADMIN_HTML, WEBTERM_HTML, _nav_page


@pytest.mark.browser_smoke
@pytest.mark.parametrize('outcome', ['input', 'none', 'readonly', 'rejected', 'wrong-node', 'stale', 'offline', 'disabled'])
def test_local_terminal_explicit_create_and_pinned_receipt(outcome):
    # Optional dependency: importing here keeps ordinary pytest collection usable.
    # Explicit browser_smoke selection requires Playwright and fails if unavailable.
    from playwright.sync_api import sync_playwright, expect
    node = {'id': 'hp-linux', 'display_name': 'HP', 'endpoint': 'local', 'status': 'online'}
    creates, sockets, errors = [], [], []
    assets = Path(__file__).resolve().parents[1] / 'terminal_mcp/vendor/xterm'
    def serve(route):
        path = urlsplit(route.request.url).path
        pages = {'/dashboard/nodes': NODES_ADMIN_HTML, '/dashboard/sessions': SESSIONS_ADMIN_HTML,
                 '/dashboard/terminal': WEBTERM_HTML}
        if path in pages:
            route.fulfill(content_type='text/html', body=_nav_page(pages[path], path.rsplit('/', 1)[1]))
        elif path.startswith('/dashboard/assets/'):
            route.fulfill(path=assets / Path(path).name)
        elif path == '/dashboard/api/nodes':
            route.fulfill(json={'nodes': [node]})
        elif path == '/dashboard/api/sessions':
            route.fulfill(json={'sessions': [], 'session_lifecycle_enabled': outcome != 'disabled',
                                'web_terminal_enabled': True})
        elif path == '/dashboard/api/session/create':
            body = route.request.post_data_json
            creates.append(body)
            receipt = dict(session=body['name'], agent_type='shell', node_id='hp-linux',
                           session_id='$42', created_epoch=123, terminal_instance='$42@123',
                           terminal_access={'exists': True, 'input': outcome != 'readonly'})
            if outcome == 'none': receipt['terminal_access'] = {'error': 'ACCESS_DENIED'}
            if outcome == 'rejected': receipt = {'error': 'NODE_AT_SESSION_CAPACITY'}
            if outcome == 'wrong-node': receipt['node_id'] = 'worker'
            if outcome == 'stale': receipt.pop('terminal_instance')
            route.fulfill(json=receipt, status=409 if outcome == 'rejected' else 200)
        else:
            route.fulfill(json={})
    def socket(ws):
        sockets.append(ws.url)
        ws.send('{"type":"ready","readonly":false}')
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'))
        context = browser.new_context(reduced_motion='reduce')
        context.set_default_timeout(5000)
        context.route('**/*', serve)
        context.route_web_socket('**/dashboard/ws/terminal*', socket)
        page = context.new_page(); page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto('http://localhost/dashboard/nodes')
        link = page.get_by_role('link', name='Terminal cục bộ', exact=True)
        expect(link).to_have_count(1)
        for width, height in ((320, 844), (390, 844), (1366, 768), (1920, 1080)):
            page.set_viewport_size({'width': width, 'height': height})
            box = link.bounding_box()
            assert box and box['height'] >= 44 and box['x'] + box['width'] <= width
        link.click()
        expect(page.locator('#csModal')).to_be_visible()
        expect(page.locator('#csNode')).to_have_value('hp-linux')
        expect(page.locator('#csNode')).to_be_disabled()
        expect(page.locator('#csGrant')).to_have_value('none')
        assert not creates and not sockets
        measurements = []
        for width, height in ((320, 844), (390, 844), (1366, 768), (1920, 1080)):
            page.set_viewport_size({'width': width, 'height': height})
            geometry = page.locator('#csModal').evaluate('(e) => ({s:e.scrollWidth,c:e.clientWidth})')
            assert geometry['s'] <= geometry['c'] + 1
            measurements.append({'width': width, 'height': height, 'modal': geometry,
                                 'grant': page.locator('#csGrant').bounding_box()})
            if outcome == 'input' and os.environ.get('HP_LOCAL_EVIDENCE_DIR'):
                page.screenshot(path=str(Path(os.environ['HP_LOCAL_EVIDENCE_DIR']) / f'create-local-{width}.png'))
        if outcome == 'input' and os.environ.get('HP_LOCAL_EVIDENCE_DIR'):
            (Path(os.environ['HP_LOCAL_EVIDENCE_DIR']) / 'geometry.json').write_text(json.dumps(measurements, indent=2))
        page.locator('#csSubmitBtn').focus()
        page.keyboard.press('Tab')
        expect(page.locator('#csModalCloseBtn')).to_be_focused()
        if outcome == 'offline': node['status'] = 'offline'
        if outcome != 'none': page.locator('#csGrant').select_option('read' if outcome == 'readonly' else 'read_send')
        page.locator('#csSubmitBtn').click()
        if outcome in ('offline', 'disabled'):
            expect(page.locator('#csError')).not_to_be_empty()
            assert not creates
        else:
            expect(page.locator('#csError')).not_to_be_empty()
            assert len(creates) == 1
            assert creates[0]['node'] == creates[0]['expected_local_node'] == 'hp-linux'
            assert creates[0]['agent_type'] == 'shell'
            assert creates[0]['grant_mode'] == ('none' if outcome == 'none' else 'read' if outcome == 'readonly' else 'read_send')
        opened = page.locator('#csCreatedTerminal')
        if outcome in ('input', 'readonly'):
            expect(opened).to_be_visible()
            with page.expect_popup() as popup: opened.click()
            terminal = popup.value
            expect(terminal.locator('#connPill')).to_contain_text('LIVE')
            query = parse_qs(urlsplit(sockets[0]).query)
            assert query['session'] == [creates[0]['name']]
            assert query['expected_node'] == ['hp-linux']
            assert query['expected_instance'] == ['$42@123']
            terminal.close()
        else:
            expect(opened).to_have_count(0)
            assert sockets == []
        assert not errors
        browser.close()
