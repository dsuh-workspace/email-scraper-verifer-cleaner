import os
from dotenv import load_dotenv
from sqlalchemy import create_engine

# 1. Load .env into the process environment.
load_dotenv()

# 2. Read the connection string. Falls back to a local SQLite file so
#    fresh clones can boot without any config — override in .env for
#    Postgres/production.
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Create a .env file with a line like:\n"
        "  DATABASE_URL=sqlite:///database/hvac_leads.db\n"
        "or a full Postgres URI, e.g.:\n"
        "  DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/db"
    )

if DATABASE_URL.startswith("sqlite:///"):
    db_path = DATABASE_URL.replace("sqlite:///", "")
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

# 3. Create the SQLAlchemy engine used across the pipeline with SQLite lock protection
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"timeout": 30})
    from sqlalchemy import event
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()
else:
    engine = create_engine(DATABASE_URL)