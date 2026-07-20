
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine

# 1. Look for the. env file and load its variables into memory
load_dotenv()

# 2. Grab the DATABASE_URL variable we set up in .env file
DATABASE_URL = os.getenv("DATABASE_URL")

# 3. Create the engine (the pipeline connection to Postgres)
engine = create_engine(DATABASE_URL)