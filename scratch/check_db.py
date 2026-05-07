import os
import sys
sys.path.append(os.getcwd())
from database import engine, Base, DailyCash
try:
    Base.metadata.create_all(bind=engine)
    print("Database tables synchronized.")
except Exception as e:
    print(f"Error: {e}")
