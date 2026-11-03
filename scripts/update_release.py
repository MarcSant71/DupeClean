import subprocess
import json
import urllib.request
from pathlib import Path

# 1. Get credentials from Git Credential Manager
res = subprocess.check_output(
    ['git', 'credential', 'fill'],
    input=b'protocol=https\nhost=github.com\n'
).decode('utf-8')

token = None
for line in res.splitlines():
    if line.startswith('password='):
        token = line.split('=', 1)[1]
        break

if not token:
    raise RuntimeError("GitHub token could not be retrieved from Git Credential Manager.")

# 2. Read RELEASE_NOTES.md directly as UTF-8
notes_file = Path(__file__).resolve().parent.parent / "RELEASE_NOTES.md"
body_text = notes_file.read_text(encoding="utf-8")

# 3. Patch release via GitHub API
payload = json.dumps({
    'name': 'DupeClean v1.0.0',
    'body': body_text
}).encode('utf-8')

req = urllib.request.Request(
    'https://api.github.com/repos/MarcSant71/DupeClean/releases/432197094',
    data=payload,
    headers={
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json; charset=utf-8',
        'User-Agent': 'DupeClean-Release-Updater'
    },
    method='PATCH'
)

with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode('utf-8'))
    print("SUCCESS: Release notes updated cleanly from RELEASE_NOTES.md!")
