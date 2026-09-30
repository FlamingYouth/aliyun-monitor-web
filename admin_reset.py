"""Run through docker exec -it; no password in command arguments."""
from getpass import getpass
import json
import os
import sqlite3
from pathlib import Path
from werkzeug.security import generate_password_hash

path = Path(os.environ.get('DATA_DIR', '/data')) / 'monitor.db'
password = getpass('新管理员密码（至少 12 个字符）: ')
if len(password) < 12 or len(password) > 128 or password != getpass('再次输入: '):
    raise SystemExit('长度不符合要求或两次输入不同；未更改。')
with sqlite3.connect(path) as db:
    row = db.execute("SELECT value FROM meta WHERE key='admin'").fetchone()
    if not row:
        raise SystemExit('尚未初始化，请先完成网页向导。')
    admin = json.loads(row[0])
    admin['password_hash'] = generate_password_hash(password, method='pbkdf2:sha256:600000')
    db.execute("UPDATE meta SET value=? WHERE key='admin'", (json.dumps(admin),))
    db.execute('DELETE FROM sessions')
print('密码已重置，全部会话已退出。')
