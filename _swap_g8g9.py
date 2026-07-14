"""
Atomic swap: update Grade 8 (name-matched) and Grade 9 (assessment-matched)
admission numbers from their PDFs.
"""
import csv
import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "school.settings")
django.setup()

from django.db import transaction

from students.models import Student


G8_CSV = r"C:\Exam System\g8_main_full.csv"
G9_CSV = r"C:\Exam System\g9_main_full.csv"
G8_STAGE_START = 700
G9_STAGE_START = 850
DRY_RUN = "--dry-run" in sys.argv


def read_g8():
    rows = []
    with open(G8_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({
                "adm": r["sheet_adm"].strip().zfill(3),
                "name": r["name"].strip(),
            })
    return rows


def read_g9():
    rows = []
    with open(G9_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({
                "adm": r["sheet_adm"].strip().zfill(3),
                "name": r["name"].strip(),
                "assess": r["assessment_no"].strip(),
            })
    return rows


@transaction.atomic
def run():
    sid = transaction.savepoint()
    g8 = read_g8()
    g9 = read_g9()
    print(f"DRY_RUN={DRY_RUN}")
    print(f"Loaded {len(g8)} Grade 8 rows and {len(g9)} Grade 9 rows")

    # ===== PHASE 1: stage Grade 8 =====
    g8_students = list(Student.all_objects.filter(
        class_name="Grade 8", stream="Main",
    ).order_by("id"))
    print(f"\n[Phase 1] Staging {len(g8_students)} Grade 8 students to {G8_STAGE_START}-{G8_STAGE_START+len(g8_students)-1}")
    for i, s in enumerate(g8_students):
        new = f"{G8_STAGE_START + i:03d}"
        if s.admission_no == new:
            continue
        print(f"  STAGE  {s.admission_no} -> {new}   {s.name}")
        if not DRY_RUN:
            Student.all_objects.filter(pk=s.pk).update(admission_no=new)

    # ===== PHASE 2: stage Grade 9 =====
    g9_students = list(Student.all_objects.filter(
        class_name="Grade 9", stream="Main",
    ).order_by("id"))
    print(f"\n[Phase 2] Staging {len(g9_students)} Grade 9 students to {G9_STAGE_START}-{G9_STAGE_START+len(g9_students)-1}")
    for i, s in enumerate(g9_students):
        new = f"{G9_STAGE_START + i:03d}"
        if s.admission_no == new:
            continue
        print(f"  STAGE  {s.admission_no} -> {new}   {s.name}")
        if not DRY_RUN:
            Student.all_objects.filter(pk=s.pk).update(admission_no=new)

    # ===== PHASE 3: write Grade 8 final numbers (name-matched) =====
    print(f"\n[Phase 3] Writing Grade 8 final numbers (name-matched)")
    # Build a list of (name_lower, student) for ALL Grade 8 students, so
    # duplicate names are handled by consuming the first unused match.
    g8_pool = [
        (s.name.strip().lower(), s)
        for s in Student.all_objects.filter(class_name="Grade 8", stream="Main")
        .order_by("admission_no", "id")
    ]
    used_pks = set()
    g8_changes = 0
    g8_skipped = []
    g8_missing = []
    for r in g8:
        target = r["adm"]
        # Skip Mohamed Hassan — the two of them are indistinguishable by
        # name alone. The user will fix them manually afterwards.
        if r["name"].strip().lower() == "mohamed hassan":
            g8_skipped.append(r)
            print(f"  [SKIP]  Mohamed Hassan -> target {target}  (left at staged value, manual fix needed)")
            continue
        candidates = [s for (n, s) in g8_pool
                      if n == r["name"].lower() and s.pk not in used_pks]
        if not candidates:
            g8_missing.append(r)
            print(f"  [MISS]  no DB match for {r['name']!r}")
            continue
        s = candidates[0]
        used_pks.add(s.pk)
        if s.admission_no == target:
            continue
        print(f"  UPDATE  {s.admission_no} -> {target}   {s.name}")
        if not DRY_RUN:
            Student.all_objects.filter(pk=s.pk).update(admission_no=target)
        g8_changes += 1

    if g8_missing:
        print(f"\n  Grade 8 MISSING in DB ({len(g8_missing)}):")
        for r in g8_missing:
            print(f"    {r['adm']}  {r['name']}")

    if g8_skipped:
        print(f"\n  Grade 8 SKIPPED (manual fix needed): {len(g8_skipped)}")
        for r in g8_skipped:
            print(f"    {r['adm']}  {r['name']}")

    # Orphans: DB students whose name never appeared in the PDF
    g8_pdf_names = {r["name"].lower() for r in g8}
    orphans = [s for (n, s) in g8_pool if n not in g8_pdf_names]
    if orphans:
        print(f"\n  Grade 8 ORPHANS in DB (not in PDF, left untouched): {len(orphans)}")
        for s in orphans:
            print(f"    adm={s.admission_no}  {s.name}")

    # ===== PHASE 4: write Grade 9 final numbers (assessment-matched) =====
    print(f"\n[Phase 4] Writing Grade 9 final numbers (assessment-matched)")
    g9_now = {s.assessment_no.strip(): s for s in
              Student.all_objects.filter(class_name="Grade 9", stream="Main")
              if s.assessment_no}
    g9_changes = 0
    g9_missing = []
    used_adm_g9 = set()
    for r in g9:
        target = r["adm"]
        s = g9_now.get(r["assess"])
        if s is None:
            g9_missing.append(r)
            print(f"  [MISS]  no DB match for {r['name']!r} (assess={r['assess']})")
            continue
        if s.admission_no == target:
            continue
        if target in used_adm_g9:
            print(f"  [DUP]   {r['name']!r} target {target} already used in this run")
            continue
        print(f"  UPDATE  {s.admission_no} -> {target}   {s.name}  (assess={r['assess']})")
        if not DRY_RUN:
            Student.all_objects.filter(pk=s.pk).update(admission_no=target)
        used_adm_g9.add(target)
        g9_changes += 1

    if g9_missing:
        print(f"\n  Grade 9 MISSING in DB ({len(g9_missing)}):")
        for r in g9_missing:
            print(f"    {r['adm']}  {r['name']}  assess={r['assess']}")

    print(f"\n=== Summary: g8 updated={g8_changes} skipped={len(g8_skipped)} missing={len(g8_missing)} "
          f"g9 updated={g9_changes} missing={len(g9_missing)} ===")

    if DRY_RUN:
        transaction.savepoint_rollback(sid)
        print("\n(dry-run, nothing changed)")
    else:
        transaction.savepoint_commit(sid)
        print("\n(committed)")


run()
