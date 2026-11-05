import json
import threading
import urllib.request

from tracker.config import Product, Settings
from tracker.store import Store
from tracker.web import Renderer, start_web

from test_compute import PRODUCT, obs


def _settings(tmp_path):
    return Settings(db_path=tmp_path / "t.sqlite", web_port=0)


def test_page_renders_empty_and_populated(tmp_path):
    s = _settings(tmp_path)
    p = Product.model_validate(PRODUCT)
    store = Store(s.db_path)
    r = Renderer(s, [p])
    html = r.page(store)
    assert "Demo" in html and "No fresh in-stock offer" in html
    obs(store, "frame", "ch1", 1000)
    obs(store, "frame", "de1", 900)
    obs(store, "seat", "ch1", 400)
    store.add_observation(product_id="demo", group_id="frame", is_bundle=0, source_id="de1", seller="de1", url="u",
                          ok=0, error="HTTP 429")
    html = r.page(store)
    assert "1 300.00" in html  # cheapest set
    assert "HTTP 429" in html  # last failure shown
    assert "showing previous price" in html  # warn state
    api = r.api(store, "demo")
    assert api["total"]["value"] == 1300 and api["health"]["sources_total"] == 4
    assert r.api(store, "nope") is None


def test_http_server(tmp_path):
    s = _settings(tmp_path)
    p = Product.model_validate(PRODUCT)
    Store(s.db_path).close()
    srv = start_web(s, [p], port=0)  # port 0 => disabled
    assert srv is None
    srv = start_web(s, [p], port=18099)
    try:
        body = urllib.request.urlopen("http://127.0.0.1:18099/", timeout=5).read().decode()
        assert "<title>Demo</title>" in body
        data = json.loads(urllib.request.urlopen("http://127.0.0.1:18099/api/demo.json", timeout=5).read())
        assert data["product"] == "demo"
        code = urllib.request.urlopen("http://127.0.0.1:18099/p/demo", timeout=5).status
        assert code == 200
    finally:
        srv.shutdown()
