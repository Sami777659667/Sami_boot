import sqlite3

DB_NAME = "hosting_bot_advanced.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS bots
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  bot_name TEXT,
                  token TEXT,
                  folder_path TEXT,
                  main_file TEXT,
                  status TEXT DEFAULT 'stopped',
                  pid INTEGER,
                  archive_file_id TEXT,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

# ... (بقية الدوال الأصلية) ...

def add_bot(user_id, bot_name, folder_path, main_file, archive_file_id=None):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO bots (user_id, bot_name, folder_path, main_file, archive_file_id) VALUES (?, ?, ?, ?, ?)",
              (user_id, bot_name, folder_path, main_file, archive_file_id))
    bot_id = c.lastrowid
    conn.commit()
    conn.close()
    return bot_id

def update_bot_token(bot_id, token):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE bots SET token = ? WHERE id = ?", (token, bot_id))
    conn.commit()
    conn.close()

def update_bot_status(bot_id, status, pid=None):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE bots SET status = ?, pid = ? WHERE id = ?", (status, pid, bot_id))
    conn.commit()
    conn.close()

def get_user_bots(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT id, bot_name, status, pid FROM bots WHERE user_id = ?", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_bot_info(bot_id):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM bots WHERE id = ?", (bot_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def delete_bot_from_db(bot_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
    conn.commit()
    conn.close()
    
# 🆕 دالة لاستخدامها في التنظيف والمراقبة
def get_all_running_bots():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT id, pid FROM bots WHERE status = 'running' AND pid IS NOT NULL")
    rows = c.fetchall()
    conn.close()
    return rows
