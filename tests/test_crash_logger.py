import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dupeclean.core.crash_logger import sanitize_text, prune_old_logs, log_critical_crash, get_logs_dir

def run_tests():
    # 1. Test Sanitization
    user = os.environ.get("USERNAME", "testuser")
    raw = f"""Traceback (most recent call last):
  File "C:\\Users\\{user}\\Documents\\secret\\script.py", line 10, in run
    open('D:\\Arquivo\\Adulto\\photo1.jpg')
  File "\\\\NAS_STORAGE\\Private\\doc.pdf"
"""
    san = sanitize_text(raw)
    assert user not in san, f"Username leaked: {san}"
    assert "Adulto" not in san, f"Folder leaked: {san}"
    assert "photo1" not in san, f"Filename leaked: {san}"
    assert "NAS_STORAGE" not in san, f"UNC server leaked: {san}"
    print("[OK] Sanitization tests passed!")

    # 2. Test Logging & Pruning
    logs_dir = get_logs_dir()
    # Create a dummy old log (8 days ago)
    old_log = logs_dir / "crash_2020-01-01.log"
    old_log.write_text("old log", encoding="utf-8")
    old_mtime = time.time() - (8 * 86400)
    os.utime(old_log, (old_mtime, old_mtime))

    try:
        raise ValueError(f"Failed on D:\\SecretFolder\\file.png for user {user}")
    except Exception as e:
        log_file = log_critical_crash(e, context={"Scan Mode": "IMAGES", "Count": "141000"})

    assert not old_log.exists(), "Old log was not pruned!"
    assert log_file.exists(), "Crash log was not created!"
    content = log_file.read_text(encoding="utf-8")
    assert user not in content, "User leaked in crash report!"
    assert "SecretFolder" not in content, "Path leaked in crash report!"
    print("[OK] Prune & Log tests passed!")
    print(f"Logged file: {log_file}")
    
    # Clean up test crash log
    try:
        log_file.unlink()
    except Exception:
        pass

if __name__ == "__main__":
    run_tests()
