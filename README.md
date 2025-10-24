# XCancel Aggregated Feed (Local)

Fetch and display the **latest posts from multiple `xcancel.com` accounts** in a single browser feed — **no X/Twitter account required**. Runs locally, fast, and concurrently.

> ⚠️ This scrapes a public front-end. It’s for personal use; reliability may vary if the front-end changes.

## Features
- Add/remove accounts on the fly (no `@` needed).
- Concurrent fetching (very fast) with adjustable poll interval in the UI.
- Merged, newest-first feed shown in your browser.
- Zero auth; runs locally.

## Quick Start

### 1) Prereqs
- Python 3.11+ recommended (use `pyenv` or your system Python).

### 2) Create the environment (pyenv + pyenv-virtualenv)
```bash
brew install pyenv pyenv-virtualenv   # macOS; see README for Linux/Windows notes
pyenv install 3.11.9
cd xcancel-feed
pyenv local 3.11.9
pyenv virtualenv 3.11.9 xcancel-feed
pyenv local xcancel-feed

