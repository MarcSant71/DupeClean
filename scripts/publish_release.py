import os
import sys
import time
import json
import urllib.request
import urllib.error
import subprocess
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import dupeclean

REPO = "MarcSant71/DupeClean"
VERSION = f"v{dupeclean.__version__}"
ARCHIVE_NAME = f"DupeClean-{VERSION}-windows-cuda.7z"
ARCHIVE_PATH = ROOT_DIR / "dist" / ARCHIVE_NAME
NOTES_PATH = ROOT_DIR / "RELEASE_NOTES.md"

print(f"=== Publishing {VERSION} to GitHub ({REPO}) ===")

# 1. Get GitHub token from Git Credential Manager
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
    'User-Agent': 'DupeClean-Release-Publisher'
}

# 2. Check existing releases and delete older releases per retention policy
print("Listing existing releases...")
req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases", headers=headers)
with urllib.request.urlopen(req, timeout=30) as resp:
    releases = json.loads(resp.read().decode('utf-8'))

for r in releases:
    tag = r.get('tag_name')
    rel_id = r.get('id')
    print(f"Found existing release: {tag} (ID: {rel_id})")
    if tag != VERSION:
        print(f"Deleting older release {tag} (ID: {rel_id}) per user retention policy...")
        del_req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/releases/{rel_id}",
            headers=headers,
            method='DELETE'
        )
        try:
            with urllib.request.urlopen(del_req, timeout=30) as del_resp:
                print(f"Deleted older release {tag} successfully.")
        except Exception as e:
            print(f"Could not delete release {tag}: {e}")

        # Delete older tag ref on GitHub
        print(f"Deleting older git tag {tag} from GitHub...")
        del_tag_req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/git/refs/tags/{tag}",
            headers=headers,
            method='DELETE'
        )
        try:
            with urllib.request.urlopen(del_tag_req, timeout=30) as del_tag_resp:
                print(f"Deleted older tag {tag} successfully.")
        except Exception as e:
            print(f"Could not delete tag {tag}: {e}")

# 3. Read release notes
body_text = NOTES_PATH.read_text(encoding="utf-8")

# 4. Check if current release already exists
target_rel = next((r for r in releases if r.get('tag_name') == VERSION), None)

if target_rel:
    rel_id = target_rel['id']
    print(f"Release {VERSION} already exists (ID: {rel_id}). Updating notes...")
    patch_req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/{rel_id}",
        data=json.dumps({'name': f'DupeClean {VERSION}', 'body': body_text}).encode('utf-8'),
        headers={**headers, 'Content-Type': 'application/json'},
        method='PATCH'
    )
    with urllib.request.urlopen(patch_req, timeout=30) as resp:
        rel_data = json.loads(resp.read().decode('utf-8'))
else:
    print(f"Creating release {VERSION} on GitHub...")
    post_req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases",
        data=json.dumps({
            'tag_name': VERSION,
            'target_commitish': 'main',
            'name': f'DupeClean {VERSION}',
            'body': body_text,
            'draft': False,
            'prerelease': False
        }).encode('utf-8'),
        headers={**headers, 'Content-Type': 'application/json'},
        method='POST'
    )
    with urllib.request.urlopen(post_req, timeout=30) as resp:
        rel_data = json.loads(resp.read().decode('utf-8'))
    rel_id = rel_data['id']
    print(f"Created release {VERSION} (ID: {rel_id})!")

# 5. Check if asset already exists on this release
existing_assets = rel_data.get('assets', [])
for a in existing_assets:
    if a.get('name') == ARCHIVE_NAME:
        print(f"Deleting existing asset {ARCHIVE_NAME} (ID: {a['id']})...")
        del_asset_req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/releases/assets/{a['id']}",
            headers=headers,
            method='DELETE'
        )
        with urllib.request.urlopen(del_asset_req, timeout=30) as resp:
            print("Deleted old asset.")

# 6. Upload .7z asset via streaming with progress updates
# Clean up any older .7z archives in dist/
for old_archive in (ROOT_DIR / "dist").glob("*.7z"):
    if old_archive.name != ARCHIVE_NAME:
        try:
            print(f"Removing old local archive: {old_archive.name}")
            old_archive.unlink()
        except Exception as e:
            print(f"Could not remove {old_archive.name}: {e}")

if not ARCHIVE_PATH.exists():
    raise FileNotFoundError(f"Archive {ARCHIVE_PATH} does not exist!")

file_size_mb = ARCHIVE_PATH.stat().st_size / (1024 * 1024)
upload_url = f"https://uploads.github.com/repos/{REPO}/releases/{rel_id}/assets?name={ARCHIVE_NAME}"
print(f"Uploading asset via high-speed curl: {ARCHIVE_NAME} ({file_size_mb:.2f} MB)...")

curl_cmd = [
    "curl.exe",
    "-L",
    "-X", "POST",
    "-H", f"Authorization: Bearer {token}",
    "-H", "Accept: application/vnd.github+json",
    "-H", "X-GitHub-Api-Version: 2022-11-28",
    "-H", "Content-Type: application/x-7z-compressed",
    "--data-binary", f"@{str(ARCHIVE_PATH)}",
    upload_url
]

proc = subprocess.run(curl_cmd)
if proc.returncode != 0:
    raise RuntimeError(f"curl upload failed with exit code {proc.returncode}")

print(f"\nSUCCESS: Uploaded {ARCHIVE_NAME} successfully!")
print(f"=== Release {VERSION} published successfully! ===")
