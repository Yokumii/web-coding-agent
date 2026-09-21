from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from inspiration_library import production_browser as module


@pytest.mark.parametrize('results,expected', [
    ([200], ['direct://']),
    ([module.BrowserError('connection closed'), 200], ['direct://', 'http://proxy:7890']),
    ([503, 200], ['direct://', 'http://proxy:7890']),
    ([503, 502], ['direct://', 'http://proxy:7890']),
])
def test_entry_access_fallback(monkeypatch, tmp_path, results, expected):
    launches = []
    browsers = []
    monkeypatch.setenv('WEBCODING_BROWSER_PROXY', 'http://proxy:7890')
    monkeypatch.setattr(module, 'sync_playwright', lambda: nullcontext(object()))
    def launch(*args, proxy=None):
        launches.append(proxy)
        browser = MagicMock()
        browsers.append(browser)
        return browser
    def new_page(*args, **kwargs):
        value = results[len(launches) - 1]
        page = MagicMock()
        if isinstance(value, Exception):
            page.goto.side_effect = value
        else:
            page.goto.return_value = SimpleNamespace(status=value)
        return MagicMock(), page
    monkeypatch.setattr(module, '_launch_browser', launch)
    monkeypatch.setattr(module, '_new_page', new_page)
    class ReachedExtraction(Exception):
        pass
    def snapshot(page):
        raise ReachedExtraction()
    monkeypatch.setattr(module, '_snapshot', snapshot)
    failure = module.BrowserError if results[-1] == 502 else ReachedExtraction
    with pytest.raises(failure):
        module._observe_entry_url('https://example.org', tmp_path,
                                  network_mode='online_readonly', source_metadata={})
    assert launches == expected
    assert all(browser.close.called for browser in browsers)
