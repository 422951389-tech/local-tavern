from __future__ import annotations

import sys
from pathlib import Path

import pytest

from desktop.resources import (
    ResourceError,
    content_type_for,
    read_asset,
    resolve_resource_root,
    resolve_web_asset,
    resolve_web_root,
)


ROOT = Path(__file__).resolve().parents[1]


def _packaged_root(tmp_path: Path) -> Path:
    web = tmp_path / "web"
    styles = web / "styles"
    styles.mkdir(parents=True)
    (web / "index.html").write_text(
        '<!doctype html><html><head><meta charset="UTF-8"></head><body></body></html>',
        encoding="utf-8",
    )
    (web / "app.mjs").write_text("export const packaged = true;", encoding="utf-8")
    (styles / "workspace.css").write_text("body { color: white; }", encoding="utf-8")
    return tmp_path


def test_source_resource_root_resolves_repository_web(monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert resolve_resource_root() == ROOT
    web = resolve_web_root()
    assert web == ROOT / "web"
    assert resolve_web_asset("/") == web / "index.html"
    assert resolve_web_asset("/index.html") == web / "index.html"
    assert resolve_web_asset("/static/app.mjs") == web / "app.mjs"


def test_packaged_resource_root_uses_meipass_and_never_current_directory(
    monkeypatch,
    tmp_path,
):
    package = _packaged_root(tmp_path / "bundle")
    unrelated = tmp_path / "working-directory"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    monkeypatch.setattr(sys, "_MEIPASS", str(package), raising=False)

    assert resolve_resource_root() == package.resolve()
    assert resolve_web_root() == (package / "web").resolve()
    assert resolve_web_asset("/static/styles/workspace.css") == (
        package / "web" / "styles" / "workspace.css"
    ).resolve()


@pytest.mark.parametrize(
    "path",
    [
        "/static/../index.html",
        "/static/%2e%2e/index.html",
        "/static/missing.js",
        "/private/secret.txt",
        "https://example.com/static/app.mjs",
        "//example.com/static/app.mjs",
        "/static/app.mjs?remote=1",
        "/static/app.mjs#fragment",
        r"/static\app.mjs",
    ],
)
def test_packaged_asset_resolver_rejects_escape_external_and_missing_paths(
    path,
    tmp_path,
):
    package = _packaged_root(tmp_path / "bundle")
    with pytest.raises(ResourceError):
        resolve_web_asset(path, package / "web")


def test_packaged_index_injects_offline_csp_and_correct_content_types(tmp_path):
    package = _packaged_root(tmp_path / "bundle")
    web = package / "web"
    html = read_asset(web / "index.html").decode("utf-8")
    assert "Content-Security-Policy" in html
    assert "connect-src 'none'" in html
    assert "object-src 'none'" in html
    assert "frame-ancestors 'none'" in html
    assert content_type_for(web / "index.html") == b"text/html; charset=utf-8"
    assert content_type_for(web / "app.mjs") == b"text/javascript; charset=utf-8"
    assert content_type_for(web / "styles" / "workspace.css") == b"text/css; charset=utf-8"
