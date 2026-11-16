from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pwa_manifest_and_app_shell_are_present():
    manifest = json.loads((ROOT / "static/manifest.webmanifest").read_text())
    assert manifest["name"] == "Quella Pro"
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"
    assert manifest["icons"]
    assert (ROOT / "static/icons/icon.svg").is_file()


def test_service_worker_never_caches_live_api_state():
    worker = (ROOT / "static/sw.js").read_text()
    assert "u.pathname.startsWith('/api/')" in worker
    assert "u.pathname==='/state'" in worker
    assert "quella-shell-v8" in worker


def test_mobile_layout_and_start_stop_controls_exist():
    html = (ROOT / "static/index.html").read_text()
    css = (ROOT / "static/app.css").read_text()
    assert 'id="start"' in html and 'id="stop"' in html
    assert "@media(max-width:760px)" in css
