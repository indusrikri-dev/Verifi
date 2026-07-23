import csv
import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

load_dotenv()

OUTPUT_DIR = Path(__file__).parent / "db_export"


def main():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set. Add it to .env first.")

    OUTPUT_DIR.mkdir(exist_ok=True)

    conn = psycopg2.connect(database_url)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
    )
    tables = [row[0] for row in cur.fetchall()]

    if not tables:
        print("No tables found in the 'public' schema.")
        return

    print(f"Found {len(tables)} table(s): {', '.join(tables)}\n")

    for table in tables:
        cur.execute(f'SELECT * FROM "{table}"')
        rows = cur.fetchall()
        columns = [desc[0] for desc in cur.description]

        out_path = OUTPUT_DIR / f"{table}.csv"
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            writer.writerows(rows)

        print(f"  {table}: {len(rows)} row(s) -> {out_path}")

    cur.close()
    conn.close()
    print(f"\nDone. CSV files are in {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
