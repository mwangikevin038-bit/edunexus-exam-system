import os, re, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'school.settings')
import django

django.setup()
from students.models import Student


# All students from the two screenshots (read carefully from the rotated images)
# Format: (sheet_adm, name, assessment_no)
sheet_students = [
    # First sheet (330-390)
    ('330', 'Kazibe Russa', 'B002157531'),
    ('334', 'Mwanaisha Kassim', 'B002297745'),
    ('337', 'Sharifa Rajabu', 'B002157502'),
    ('340', 'Khalsa Hamisi', 'B002691273'),
    ('343', 'Jenipher Juma', 'B002157523'),
    ('345', 'Mariamu Mboga', 'B002157522'),
    ('348', 'Mwanamisi Mbarak', 'A002791131'),
    ('353', 'Robinson Nuru', 'B002702049'),
    ('354', 'Yusufu Hamisi', 'A002791476'),
    ('357', 'Mwanahamisi', 'A002771547'),
    ('358', 'Philomena Omari', 'B002168263'),
    ('361', 'Asha Msuya', 'A002749302'),
    ('364', 'Esther Oluya', 'A002746935'),
    ('366', 'Fatuma Rihan', 'A002745691'),
    ('371', 'Khalid Muhidin', 'A002741122'),
    ('373', 'Zainab Wesa', 'A002746028'),
    ('375', 'Mishi Athuman', 'A002758225'),
    ('379', 'Aisha Athuman', 'A002746823'),
    ('380', 'Juma Juma', 'A002761298'),
    ('381', 'Rama Athuman', 'A002760695'),
    ('382', 'Juma Mwichahe', 'B002158415'),
    ('383', 'Fatuma Mshana', 'A002769115'),
    ('384', 'Mariamu Abdalla', 'A002612208'),
    ('386', 'Sauda Juma', 'B002161298'),
    ('387', 'Eunice Ega', 'B002161341'),
    ('389', 'Haniff Hussein', 'B002160454'),
    ('390', 'Rose Wanjiru', 'B002161321'),
    # Second sheet (392-444)
    ('392', 'Grace Chang', 'A002470467'),
    ('393', 'Malik Juma', 'B002678338'),
    ('394', 'Fatma Saidis', 'A002749235'),
    ('395', 'Fatma Suleiman', 'B002271122'),
    ('401', 'Mwanakombo Juma', 'B002157772'),
    ('402', 'Robert Suleiman', 'B002157707'),
    ('403', 'Mariamu Hamisi', 'A002568833'),
    ('404', 'Hadija Nyange', 'B002161335'),
    ('407', 'Haniff Hussein', 'A002757429'),
    ('409', 'Mose Nyinge', 'A002702568'),
    ('410', 'Haniff Arega', 'A002161244'),
    ('412', 'Janet Nzala', 'A002158960'),
    ('415', 'Mwanahamisi Hanif', 'A002161531'),
    ('416', 'Noble Arega', 'A002162826'),
    ('417', 'Kastim Kombo', 'A002161829'),
    ('418', 'Hadi Mohammed', 'A002161561'),
    ('420', 'Tatu Hamisi', 'A002491478'),
    ('421', 'Jamila Mohamed', 'A002161476'),
    ('425', 'Idi Ramadhan', 'B002161237'),
    ('426', 'Esther Mwinyihaji', 'A002746863'),
    ('431', 'Kasim Kiwia', 'A002491124'),
    ('433', 'Salma Msaki', 'A002769701'),
    ('434', 'Aisha Athuman', 'A002491124'),
    ('436', 'Asha Rashid', 'B002161978'),
    ('438', 'Hussein Mwinyihaji', 'B002160060'),
    ('439', 'Jamila Mohammed', 'A002760946'),
    ('442', 'Suleiman Said', 'A002161115'),
    ('443', 'Mchela Ndegwa', 'A002161298'),
    ('444', 'Mwinjuma Mtwana', 'B002161341'),
]

# Build DB index by assessment_no
db_yellow = list(Student.all_objects.filter(
    class_name='Grade 7', stream='Yellow'
).values('id', 'admission_no', 'name', 'assessment_no'))
print(f"DB Grade 7 Yellow: {len(db_yellow)} students")

db_by_assess = {}
for s in db_yellow:
    if s['assessment_no']:
        db_by_assess[s['assessment_no'].strip()] = s

# Match by assessment number
print()
print('=== Matching by assessment number ===')
matched = []
unmatched_sheet = []
for adm, name, assess in sheet_students:
    key = (assess or '').strip()
    db = db_by_assess.get(key)
    if db:
        matched.append((db, adm, name, assess))
        print(f'  {adm:>4}  {name:<30}  {assess:<14}  -> DB: {db["admission_no"]:>4}  {db["name"]:<30}  id={db["id"]}')
    else:
        unmatched_sheet.append((adm, name, assess))
        print(f'  {adm:>4}  {name:<30}  {assess:<14}  -- NOT IN DB --')

# Unmatched DB students
matched_db_ids = {m[0]['id'] for m in matched}
print()
print(f'Matched: {len(matched)} / {len(sheet_students)}')
print(f'Sheet not in DB: {len(unmatched_sheet)}')
print(f'DB not in sheet: {len(db_yellow) - len(matched_db_ids)}')
for s in db_yellow:
    if s['id'] not in matched_db_ids:
        print(f'  DB {s["admission_no"]:>4}  {s["name"]}  {s["assessment_no"] or ""}')
