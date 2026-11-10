import sys
import subprocess
import json
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
import dupeclean

REPO = "MarcSant71/DupeClean"
VERSION = f"v{dupeclean.__version__}"

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

headers = {
    'Authorization': f'Bearer {token}',
    'Accept': 'application/vnd.github+json',
    'Content-Type': 'application/json; charset=utf-8',
    'User-Agent': 'DupeClean-Release-Updater'
}

# 2. Find release by tag
req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/tags/{VERSION}", headers=headers)
with urllib.request.urlopen(req) as resp:
    rel_data = json.loads(resp.read().decode('utf-8'))
rel_id = rel_data['id']

# 3. Read RELEASE_NOTES.md directly as UTF-8
notes_file = ROOT_DIR / "RELEASE_NOTES.md"
body_text = notes_file.read_text(encoding="utf-8")

# 4. Patch release via GitHub API
payload = json.dumps({
    'name': f'DupeClean {VERSION}',
    'body': body_text
}).encode('utf-8')

patch_req = urllib.request.Request(
    f'https://api.github.com/repos/{REPO}/releases/{rel_id}',
    data=payload,
    headers=headers,
    method='PATCH'
)

with urllib.request.urlopen(patch_req) as resp:
    data = json.loads(resp.read().decode('utf-8'))
    print(f"SUCCESS: Release notes for {VERSION} updated cleanly on GitHub!")
