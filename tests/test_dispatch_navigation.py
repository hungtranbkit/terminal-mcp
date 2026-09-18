from terminal_mcp.dashboard import DASHBOARD_HTML


def test_dispatch_navigation_links_are_in_canonical_menu():
    assert 'id="dispatchMonitorLink"' in DASHBOARD_HTML
    assert 'href="/dashboard/ops/novaretail-dispatch"' in DASHBOARD_HTML
    assert 'id="dispatchSettingsLink"' in DASHBOARD_HTML
    assert 'href="/dashboard/ops/dispatch-settings?project=novaretail"' in DASHBOARD_HTML
