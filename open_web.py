#!/usr/bin/env python3
"""
Open the hosted news page in your browser.

The link lives in config_local.json ("web_url") and cloud_manager.py mirrors it
to web_url.txt. Hunting a single URL inside a long config file is annoying, so
double-click open_web.cmd instead (or run `python open_web.py`).

    python open_web.py           open the page
    python open_web.py --print   just print the link
"""
import json
import os
import sys
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
URL_FILE = os.path.join(BASE_DIR, "web_url.txt")
CONFIG_FILE = os.path.join(BASE_DIR, "config_local.json")


def web_url():
    """The site address: web_url.txt first (one line, easy to read), then the
    config's web_url key. Empty string when neither has one."""
    try:
        with open(URL_FILE, encoding="utf-8") as fh:
            url = fh.read().strip().lstrip("\ufeff").strip()
        if url:
            return url
    except OSError:
        pass
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            return str(json.load(fh).get("web_url") or "").strip()
    except Exception:
        return ""


def main():
    url = web_url()
    if not url:
        print("No news-page link found.")
        print('Add "web_url" to config_local.json (see SETUP-WEB.md), or paste the')
        print("address into the panel's Configuration tab once.")
        return 2
    print(url)
    if "--print" in sys.argv:
        return 0
    if not webbrowser.open(url):
        print("Could not start a browser - copy the link above.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
