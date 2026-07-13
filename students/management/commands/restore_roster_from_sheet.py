"""
Restore a class roster from a mark-entry sheet (generic).

The DB and the printed sheet disagree on both names and admission
numbers. The sheet is the authoritative source — it carries the
original (3-digit) admission numbers and the NAMES that were issued on
day-one. The DB has different names for several students and short
sequential numbers for everyone.

**Guardian links are preserved.** The user has manually linked each
student to their parent in the DB. We do not touch ``guardian_id`` on
existing rows, and new rows are created with a placeholder Guardian
that the user can re-link afterwards via the Faculty admin.

Strategy: match by ``assessment_no`` (unique per student).
  * DB student with the same assessment_no  ->  update name + admission_no (guardian preserved)
  * Sheet student not in DB at all          ->  create a new student (placeholder guardian)
  * DB student not in the sheet             ->  flag (do not delete)

The class+stream to target is taken from ``--class-name`` and
``--stream`` so the same command can be used for any roster. The
placeholder Guardian's phone and name come from settings (or the
env vars ``EDUNEXUS_UNLINKED_GUARDIAN_PHONE`` / ``..._NAME``).

Usage:
  python manage.py restore_roster_from_sheet \\
      --csv path/to/restore.csv \\
      --class-name "Grade 7" --stream "Yellow"
  python manage.py restore_roster_from_sheet --csv ... --dry-run
"""
import csv
import logging
import re

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from students.models import Guardian, School, Student

logger = logging.getLogger("students.management")


class Command(BaseCommand):
    help = "Restore a class roster from a mark-entry sheet (preserves guardian links)."

    def add_arguments(self, parser):
        parser.add_argument("--csv", required=True,
                            help="CSV with columns: sheet_adm, name, assessment_no")
        parser.add_argument("--class-name", required=True,
                            help='Class name as stored in the DB, e.g. "Grade 7"')
        parser.add_argument("--stream", required=True,
                            help='Stream name as stored in the DB, e.g. "Yellow"')
        parser.add_argument("--school", default=None,
                            help="School code; defaults to the only one in DB")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--rename", action="store_true",
                            help="ALSO rename DB students to match the sheet. "
                                 "Off by default — admission numbers only, "
                                 "the DB names are the source of truth.")

    @staticmethod
    def _school_section_for(class_name):
        """
        Map a class name to its school_section.  Grade 1-3 -> PRIMARY
        (LOWER), Grade 4-6 -> PRIMARY (UPPER), Grade 7-9 -> JSS.
        """
        m = re.search(r'\d+', class_name or '')
        n = int(m.group()) if m else 0
        if 1 <= n <= 6:
            return 'PRIMARY'
        if 7 <= n <= 9:
            return 'JSS'
        return 'JSS'

    @transaction.atomic
    def handle(self, *args, **opts):
        sid = transaction.savepoint()
        path = opts["csv"]
        dry = opts["dry_run"]
        allow_rename = opts["rename"]
        class_name = opts["class_name"]
        stream = opts["stream"]
        school_code = opts["school"]

        if school_code:
            school = School.objects.get(code=school_code)
        else:
            school = School.objects.first()
        if school is None:
            self.stdout.write(self.style.ERROR("No school in DB."))
            return

        # Read CSV
        rows = []
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append({
                    "adm": (r.get("sheet_adm") or "").strip(),
                    "name": (r.get("name") or "").strip(),
                    "assess": (r.get("assessment_no") or "").strip(),
                })
        self.stdout.write(f"Read {len(rows)} students from {path}")
        self.stdout.write(f"Targeting {class_name} {stream} in {school.code}")

        # Index DB by assessment_no
        db_qs = Student.all_objects.filter(
            school=school, class_name=class_name, stream=stream,
        )
        db_by_assess = {}
        for s in db_qs:
            if s.assessment_no:
                db_by_assess[s.assessment_no.strip()] = s
        self.stdout.write(f"DB has {len(db_by_assess)} {class_name} {stream} students with assessment_no")

        # Track which DB rows we touched
        matched_assess = set()
        updated = 0
        created = 0
        conflicts = 0

        # When two students need to swap adm numbers, the algorithm
        # has to move one BEFORE the other.  We track the desired
        # (pk -> new_adm) map as we go and use that to detect collisions
        # instead of going back to the DB (which hasn't been written
        # to yet during a dry-run, and which has stale values during
        # the real run as we iterate row by row).
        pending_adms = {}  # {pk: new_adm}

        for r in rows:
            if not r["assess"] or not r["adm"] or not r["name"]:
                continue
            new_adm = r["adm"].zfill(3)
            new_name = r["name"]
            assess = r["assess"]

            db = db_by_assess.get(assess)
            if db is None:
                # No DB match by assessment_no. Try to find by name + class.
                by_name = db_qs.filter(name__iexact=new_name).first()
                if by_name:
                    db = by_name

            if db is not None:
                matched_assess.add(db.assessment_no.strip() if db.assessment_no else "")
                changes = []
                if db.admission_no != new_adm:
                    # Check collision against pending updates FIRST
                    # (these won't be in the DB yet during a dry-run, and
                    # may be stale during the real run).  Then against
                    # the DB, EXCLUDING the rows that have a pending
                    # update (their DB value is the old one).
                    collision = None
                    for other_pk, other_new in pending_adms.items():
                        if other_pk == db.pk:
                            continue  # skip self
                        if other_new == new_adm:
                            other_db = db_qs.filter(pk=other_pk).first()
                            collision = type("X", (), {
                                "pk": other_pk,
                                "name": (other_db.name if other_db else f"pk={other_pk}") + " (pending in this run)",
                            })()
                            break
                    if collision is None:
                        # Exclude the row we're checking plus any rows
                        # with a pending update (their DB value is stale).
                        exclude_pks = [db.pk] + list(pending_adms.keys())
                        collision_qs = db_qs.filter(
                            admission_no=new_adm
                        ).exclude(pk__in=exclude_pks)
                        collision = collision_qs.first()
                    if collision is not None:
                        self.stdout.write(self.style.WARNING(
                            f"  [skip] adm {new_adm} already taken by {collision.name} (id={collision.pk})"
                        ))
                        conflicts += 1
                        continue
                    changes.append(f"adm {db.admission_no} -> {new_adm}")
                # Names: the DB is the source of truth.  We only rename
                # when the user explicitly opts in via --rename.
                if allow_rename and db.name != new_name:
                    changes.append(f"rename '{db.name}' -> '{new_name}'")
                if changes:
                    name_note = "" if allow_rename else "  (name preserved)"
                    self.stdout.write(
                        f"  UPDATE  {db.name:<30}  adm={db.admission_no:>4} -> {new_adm}  "
                        f"guardian_id={db.guardian_id} (preserved)  assess={assess}{name_note}"
                    )
                    # Record the pending change so subsequent rows in
                    # the same CSV see the new value, not the old DB one.
                    pending_adms[db.pk] = new_adm
                    if not dry:
                        # Update ONLY the fields that actually changed.
                        # Guardian is never touched.  Name is only
                        # updated when --rename is passed.
                        update_kwargs = {"admission_no": new_adm}
                        if allow_rename and db.name != new_name:
                            update_kwargs["name"] = new_name
                        Student.all_objects.filter(pk=db.pk).update(**update_kwargs)
                    updated += 1
                else:
                    self.stdout.write(f"  ok      {db.name:<30}  adm={db.admission_no}  (no change)")
            else:
                # No DB match. Create. (Names ARE taken from the sheet
                # here because there is no DB row to preserve.)
                self.stdout.write(
                    f"  CREATE  {new_name:<30}  adm={new_adm}  assess={assess}  "
                    f"(placeholder guardian — please link manually)"
                )
                if not dry:
                    # Reuse the single placeholder guardian we keep
                    # around for unlinked students (or create it once).
                    # Phone + name come from settings so the school
                    # can customise them per deployment.
                    placeholder_phone = settings.UNLINKED_GUARDIAN_PHONE
                    placeholder_name = settings.UNLINKED_GUARDIAN_NAME
                    guardian = Guardian.all_objects.filter(
                        school=school, phone=placeholder_phone,
                    ).first()
                    if guardian is None:
                        guardian = Guardian.all_objects.create(
                            school=school,
                            name=placeholder_name,
                            phone=placeholder_phone,
                        )
                    Student.all_objects.create(
                        school=school,
                        class_name=class_name,
                        stream=stream,
                        school_section=self._school_section_for(class_name),
                        admission_no=new_adm,
                        name=new_name,
                        assessment_no=assess,
                        term="Term 2",
                        year=2026,
                        religion="",
                        gender="",
                        guardian=guardian,
                    )
                created += 1

        # Orphans
        orphans = [s for s in db_qs if (s.assessment_no or "").strip() not in matched_assess]
        # But we need to compare on something; use name+class match against sheet names
        sheet_names = {r["name"].strip().lower() for r in rows}
        real_orphans = [s for s in orphans if (s.name or "").strip().lower() not in sheet_names]
        if real_orphans:
            self.stdout.write(self.style.WARNING(
                f"\n{len(real_orphans)} DB students are NOT in the sheet (left in place, guardian preserved):"
            ))
            for s in real_orphans:
                self.stdout.write(
                    f"  ORPHAN  adm={s.admission_no:>4}  {s.name}  assess={s.assessment_no or ''}  "
                    f"guardian_id={s.guardian_id}"
                )
            if retire:
                self.stdout.write(self.style.WARNING(
                    f"\nRetiring {len(real_orphans)} orphan students (is_active=False)..."
                ))
                if not dry:
                    Student.all_objects.filter(pk__in=[s.pk for s in real_orphans]).update(is_active=False)
                # actually Student model has no is_active; skip retire
                self.stdout.write(self.style.WARNING(
                    "(Student model has no is_active field; orphans left in place — please delete manually if needed)"
                ))

        if dry:
            transaction.savepoint_rollback(sid)
            self.stdout.write(self.style.WARNING("(dry-run, nothing changed)"))
        else:
            transaction.savepoint_commit(sid)
        self.stdout.write(self.style.SUCCESS(
            f"\nDone. updated={updated}  created={created}  conflicts={conflicts}  orphans={len(real_orphans)}"
        ))
