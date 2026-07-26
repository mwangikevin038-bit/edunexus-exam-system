import sqlite3  # Change to psycopg2 if using Postgres, or mysql.connector for MySQL

def restore_admission_numbers():
    try:
        # 1. Connect to your database (Replace with your actual database details)
        # Example for SQLite: conn = sqlite3.connect("database.db")
        # Example for Postgres: conn = psycopg2.connect(host="localhost", database="exam_db", user="postgres", password="yourpassword")
        conn = sqlite3.connect("database.db") 
        cursor = conn.cursor()
        
        print("Connected to database successfully. Fetching students...")

        # 2. Fetch all students sorted by their ORIGINAL database insertion order
        cursor.execute("SELECT id FROM students ORDER BY id ASC")
        rows = cursor.fetchall()
        
        print(f"Found {len(rows)} students. Starting re-indexing...")

        # 3. Loop through and update them with the correct sequential number
        for index, row in enumerate(rows, start=1):
            student_id = row[0] # Gets the ID from the tuple
            
            # Formats number to 3 digits (1 -> 001, 25 -> 025)
            correct_admission = f"{index:03d}" 
            
            # If using MySQL/Postgres, use %s instead of ?
            cursor.execute(
                "UPDATE students SET admission_number = ? WHERE id = ?",
                (correct_admission, student_id)
            )
            
        # 4. Save changes to database
        conn.commit()
        cursor.close()
        conn.close()
        print("Database successfully restored to original chronological order!")
        
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    restore_admission_numbers()