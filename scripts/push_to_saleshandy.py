import os
import sys
sys.path.insert(0, os.path.abspath('.'))

import time
import requests
from dotenv import load_dotenv
from app.pipeline.export_saleshandy import push_to_saleshandy_api, SEQUENCE_ID_MAP
from app.logging_config import setup_logging

load_dotenv()
setup_logging()

def main():
    print("=" * 80)
    print("DEPLOYING VERIFIED & CLASSIFIED LEADS TO LIVE SALESHANDY SEQUENCES")
    print("=" * 80)
    
    print("\nTarget Sequences:")
    for perm, seq_id in SEQUENCE_ID_MAP.items():
        print(f"  * {perm:<32} -> {seq_id} (https://app.saleshandy.com/sequences/{seq_id})")
        
    print("\nStarting live API deployment (min_score=80, exclude_unexported=True, only_classified=False)...")
    
    results = push_to_saleshandy_api(
        min_score=80,
        exclude_unexported=True,
        only_classified=False
    )
    
    print("\n" + "=" * 80)
    print("SALESHANDY IMPORT SUMMARY REPORT")
    print("=" * 80)
    total_imported = 0
    for perm, count in results.items():
        if count > 0:
            seq_id = SEQUENCE_ID_MAP.get(perm, "N/A")
            print(f"  [+] {perm:<32} : {count:>3} prospects -> https://app.saleshandy.com/sequences/{seq_id}")
            total_imported += count
            
    print("-" * 80)
    print(f"Total Prospects Successfully Enrolled into Saleshandy: {total_imported}")
    print("=" * 80)

if __name__ == "__main__":
    main()
