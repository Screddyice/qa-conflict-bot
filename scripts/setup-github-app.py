#!/usr/bin/env python3
"""One-shot GitHub App registration via the manifest flow.

GitHub Apps can't be created via plain REST — only via the manifest flow,
which still requires the user to click "Create" once in a browser. This
script automates everything around that single click:

  1. Opens a localhost HTTP listener (default port 8765).
  2. Serves an auto-submitting form that POSTs the manifest to GitHub.
  3. Captures the redirect callback containing a one-time code.
  4. POSTs to /app-manifests/{code}/conversions to obtain App ID, slug,
     webhook secret, and private-key PEM.
  5. Writes everything to the output directory (mode 0600).

Usage:
    python3 scripts/setup-github-app.py \\
        --webhook-url 'https://your-host.example.com/hooks/github' \\
        --name 'pr-conflict-bot' \\
        --homepage 'https://github.com/<you>/pr-conflict-bot' \\
        --output ~/.pr-conflict-bot/setup

After the script finishes, the output directory will contain:
    app-id           plain text App ID
    app-slug         plain text App slug (use <slug>[bot] as GITHUB_BOT_LOGIN)
    webhook-secret   plain text webhook secret
    private-key.pem  RSA private key for the App

Then visit `<html_url>/installations/new` (printed at the end) to install
the App on the orgs/repos you want covered.

Requires only the Python standard library.
"""

from __future__ import annotations

import argparse
import http.server
import json
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path


CALLBACK_HTML = b"""<!doctype html>
<html><body style="font-family: -apple-system, sans-serif; padding: 4em; text-align: center;">
<h2>Done.</h2>
<p>App registered. Returning to terminal - you can close this tab.</p>
</body></html>
"""


def index_html(manifest: dict) -> bytes:
    return f"""<!doctype html>
<html><body onload="document.forms[0].submit()" style="font-family: -apple-system, sans-serif; padding: 4em;">
<p>Submitting App manifest to GitHub… (if this doesn't auto-redirect, click Submit)</p>
<form action="https://github.com/settings/apps/new" method="post">
  <input type="hidden" name="manifest" value='{json.dumps(manifest).replace("'", "&apos;")}'>
  <input type="submit" value="Submit">
</form>
</body></html>
""".encode()


def make_handler(manifest: dict, result: dict, done: threading.Event) -> type:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlsplit(self.path)
            if path.path == "/":
                body = index_html(manifest)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path.path == "/callback":
                qs = urllib.parse.parse_qs(path.query)
                code = qs.get("code", [None])[0]
                if not code:
                    self.send_error(400, "missing ?code")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(CALLBACK_HTML)))
                self.end_headers()
                self.wfile.write(CALLBACK_HTML)
                result["code"] = code
                done.set()
            else:
                self.send_error(404)

        def log_message(self, *_args: object) -> None:
            pass

    return Handler


def convert_code(code: str) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/app-manifests/{code}/conversions",
        method="POST",
        headers={"Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--webhook-url", required=True,
                   help="Public HTTPS URL the App will POST webhooks to (e.g. https://x/hooks/github)")
    p.add_argument("--name", default="pr-conflict-bot",
                   help="App name (must be globally unique on GitHub)")
    p.add_argument("--homepage", default="https://github.com/Screddyice/pr-conflict-bot",
                   help="Homepage URL shown on the App's GitHub page")
    p.add_argument("--output", default="~/.pr-conflict-bot/setup",
                   help="Directory to write app-id, app-slug, webhook-secret, private-key.pem")
    p.add_argument("--port", type=int, default=8765,
                   help="Local port for the manifest-flow listener")
    args = p.parse_args()

    output = Path(args.output).expanduser()
    output.mkdir(parents=True, mode=0o700, exist_ok=True)

    webhook_secret = secrets.token_hex(32)
    manifest = {
        "name": args.name,
        "url": args.homepage,
        "hook_attributes": {"url": args.webhook_url, "secret": webhook_secret},
        "redirect_url": f"http://localhost:{args.port}/callback",
        "public": True,
        "default_events": ["pull_request"],
        "default_permissions": {
            "contents": "write",
            "pull_requests": "write",
            "metadata": "read",
            "checks": "read",
        },
    }

    result: dict = {}
    done = threading.Event()
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", args.port), make_handler(manifest, result, done)
    )
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    print(f"local listener: http://localhost:{args.port}")
    print("opening browser; click 'Create GitHub App' on the GitHub page…")
    webbrowser.open(f"http://localhost:{args.port}/")

    if not done.wait(timeout=300):
        print("timeout waiting for callback (5 min)", file=sys.stderr)
        server.shutdown()
        return 1
    server.shutdown()

    print(f"got manifest code; converting…")
    app = convert_code(result["code"])

    (output / "app-id").write_text(str(app["id"]))
    (output / "app-slug").write_text(app["slug"])
    (output / "webhook-secret").write_text(app["webhook_secret"])
    (output / "private-key.pem").write_text(app["pem"])
    for f in ("app-id", "app-slug", "webhook-secret", "private-key.pem"):
        (output / f).chmod(0o600)

    print()
    print("=== created ===")
    print(f"  App ID:      {app['id']}")
    print(f"  Slug:        {app['slug']}")
    print(f"  Bot login:   {app['slug']}[bot]")
    print(f"  Owner:       {app['owner']['login']}")
    print(f"  HTML URL:    {app['html_url']}")
    print()
    print(f"artifacts saved to {output}/ (mode 0600):")
    print("  app-id, app-slug, webhook-secret, private-key.pem")
    print()
    print("next: install the App on your orgs at:")
    print(f"  {app['html_url']}/installations/new")
    return 0


if __name__ == "__main__":
    sys.exit(main())
