"""Smoke tests: the app boots and core pages render."""


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_dashboard_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Track every sat" in r.text


def test_brand_arcasats(client):
    r = client.get("/")
    assert "ArcaSats" in r.text
    assert "Track the Chain. Own the Future." in r.text
    assert "Support" in r.text
    assert "DirigoBTC" in r.text  # supporter attribution in footer


def test_about_page_with_faq(client):
    r = client.get("/about")
    assert r.status_code == 200
    assert "ArcaSats" in r.text
    assert "Frequently asked questions" in r.text
    assert "<details" in r.text  # expand/collapse accordion
    assert "What is ArcaSats?" in r.text


def test_dark_mode_present(client):
    # Dark-mode wiring is mode-agnostic (works with local-default OR cdn assets): the toggle
    # button plus the pre-paint script that adds the `dark` class to <html>.
    r = client.get("/")
    assert "theme-toggle" in r.text
    assert "classList.add('dark')" in r.text


def test_primary_buttons_use_btn_tokens(client):
    # Guard the dark-mode contrast bug: primary buttons must use the btn tokens,
    # never `bg-heading text-darkink` (cream-on-cream in dark mode).
    r = client.get("/")
    assert "bg-btnbg" in r.text
    assert "bg-heading text-darkink" not in r.text


def test_accounts_page(client):
    r = client.get("/accounts")
    assert r.status_code == 200
    assert "Accounts" in r.text


def test_local_assets_mode_no_cdn(client):
    # Packaged builds set ASSETS=local -> vendored assets, no CDN requests.
    from app.templating import templates
    templates.env.globals["ASSETS"] = "local"
    try:
        r = client.get("/")
        assert "/static/tailwind.css" in r.text
        assert "/static/vendor/htmx.min.js" in r.text
        assert "cdn.tailwindcss.com" not in r.text
        assert "unpkg.com" not in r.text
    finally:
        templates.env.globals["ASSETS"] = "cdn"


def test_add_account_partial(client):
    r = client.get("/partials/add-account-form")
    assert r.status_code == 200
    assert "New account" in r.text
