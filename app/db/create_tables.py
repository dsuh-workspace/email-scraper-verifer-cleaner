# We import the building blocks from SQLAlchemy to define our table columns
from sqlalchemy import (
    create_engine,       # Manages the connection to the database
    Column,              # Tells SQLAlchemy that this variable is a table column.
    Integer,             # Data type: whole numbers
    Text,                # Data type: names, URLs, emails
    TIMESTAMP,           # Data type: exact date and time
    ForeignKey,          # Creates a relationship link to another table
    Boolean              # Data type: True or False
)

# This tool acts as the master register that tracks all of our database blueprints
from sqlalchemy.orm import declarative_base


# NOTE: If Python gives you an import error, change this to: from app.db.database import engine
from app.db.database import engine


# Base is a special class. Any Python class we create that uses (Base) will automatically be turned into a SQL table.
Base = declarative_base()

# ================================
# Defining the tables
# ================================

class ScrapeRun(Base):
    # tracks everytime webscraper runs to gather new HVAC leads
    __tablename__ = "scrape_runs" # The actual name of the table inside PostgreSQL

    id = Column(Integer, primary_key=True)
    query = Column(Text)
    location = Column(Text)
    category = Column(Text)
    status = Column(Text)
    started_at = Column(TIMESTAMP)
    completed_at = Column(TIMESTAMP)


class RawLead(Base):
    "The 'rough draft' table where scraped data is dumped directly from the web."
    __tablename__ = "raw_leads"

    id = Column(Integer, primary_key=True)

# This links this lead back to the specific ScrapeRun that found it.
    # If ScrapeRun #5 found this lead, this column will hold the number 5.
    scrape_run_id = Column(Integer, ForeignKey("scrape_runs.id"))
    
    business_name = Column(Text)  # Name scraped off Google/Yelp
    category = Column(Text)       # Category tags found on their page
    phone = Column(Text)          # Scraped phone number
    website = Column(Text)        # Scraped website URL
    email = Column(Text)          # Any email found on their site during the scrape


class Business(Base):
    """Your cleaned, master directory of unique HVAC companies. 
    Duplicates from raw_leads are filtered out before moving here."""
    __tablename__ = "businesses"

    id = Column(Integer, primary_key=True)  # Master business ID
    business_name = Column(Text)            # Verified business name
    category = Column(Text)                 # Main business category
    website = Column(Text)                  # Cleaned website URL
    domain = Column(Text)                   # Just the domain (e.g., "coolair.com") for quick checking
    phone = Column(Text)                    # Main office phone number


class Contact(Base):
    """Tracks individual people working at those companies (Owners, Managers, etc.)"""
    __tablename__ = "contacts"

    id = Column(Integer, primary_key=True)  # Unique ID for this human being
    
    # Links this person directly to a business profile in the 'businesses' table
    business_id = Column(Integer, ForeignKey("businesses.id"))
    
    name = Column(Text)         # Person's name (e.g., "John Doe")
    phone = Column(Text)        # Their direct or cell phone number
    title = Column(Text)        # Their job role (e.g., "Owner", "Marketing Director")
    email = Column(Text)        # Their direct business email address
    lead_status = Column(Text)  # Where they are in your sales funnel (e.g., "Not Contacted", "Emailed")


class EmailVerification(Base):
    """Stores data showing whether a contact's email address is real or fake."""
    __tablename__ = "email_verifications"

    id = Column(Integer, primary_key=True)
    
    # Links this verification result to a specific person in the 'contacts' table
    contact_id = Column(Integer, ForeignKey("contacts.id"))
    
    status = Column(Text)   # Result from your validation tool (e.g., "deliverable", "undeliverable", "catch-all")
    score = Column(Integer)  # Deliverability score from 1 to 100


class ExportHistory(Base):
    """Logs every time you export a lead to a spreadsheet or CRM 
    so you never accidentally spam the same person twice."""
    __tablename__ = "export_history"

    id = Column(Integer, primary_key=True)
    
    # Links this export log to the specific contact that was exported
    contact_id = Column(Integer, ForeignKey("contacts.id"))
    
    destination = Column(Text)  # Where you sent it (e.g., "Gsheet_Dallas_HVAC", "HubSpot_Import")

# ==========================================
# 4. EXECUTION (THE TRIGGER)
# ==========================================

# This single line tells SQLAlchemy to take all 6 blueprints defined above,
# cross-reference them against the database 'engine' pipeline, and completely 
# construct the actual physical tables inside your PostgreSQL database.
Base.metadata.create_all(engine)

# Success message to let you know it completed without crashing
print("Database tables created.")