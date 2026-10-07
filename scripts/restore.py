"""Linux/Docker, application stopped: python scripts/restore.py backups/NAME.sqlite3.
Prefer documented Railway RESTORE_BACKUP_FILE workflow when not using a terminal.
"""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from autoucbot.config import Config
from autoucbot.maintenance import restore_backup
if len(sys.argv)!=2:raise SystemExit('Usage: python scripts/restore.py backups/NAME.sqlite3')
print('Restored; reconciliation is mandatory.' if restore_backup(Config.from_env(),sys.argv[1]) else 'Already applied; no changes.')
