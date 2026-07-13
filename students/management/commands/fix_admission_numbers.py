"""
Restore original (3-digit) admission numbers for JSS students.

The DB has them stored as sequential short numbers (e.g. "001", "002",
"005"...) that came from a botched re-import. The original admission
numbers assigned on day-one (per the mark-entry sheet the school
printed) are 3-digit numbers in the 300-499 range, with intentional
gaps where students left or were re-registered.

This command:

  1. Accepts a CSV of (current_admission_no_or_name, correct_admission_no)
     rows and updates the matching students.
  2. Can also do bulk renumbering based on class+stream order (e.g.
     "renumber Grade 7 Blue starting at 331 in alphabetical order").

Usage examples
-------------

# Apply a CSV:
python manage.py fix_admission_numbers \\
    --csv path/to/fixes.csv \\
    --dry-run

# Show the current state of one class so you can build the CSV:
python manage.py fix_admission_numbers --show "Grade 7 Blue"

# Bulk renumber within a class (alphabetical) starting at a given
# number, e.g. renumber Grade 7 Blue alphabetically from 331:
python manage.py fix_admission_numbers \\
    --renumber "Grade 7 Blue" --start 331
"""
import csv
import sys

from django.core.management.base import BaseCommand
from django.db import transaction

from students.models import Student


class Command(BaseCommand):
    help = "Restore original admission numbers for JSS students."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv",
            help="Path to a CSV with columns: current_adm, new_adm, [class, stream]",
        )
        parser.add_argument(
            "--show",
            help='Show the current students for the given "Class Stream" so you can build a CSV.',
        )
        parser.add_argument(
            "--renumber",
            help='Renumber a class+stream alphabetically starting at --start, e.g. "Grade 7 Blue".',
        )
        parser.add_argument(
            "--start",
            type=int,
            default=301,
            help="Starting admission number for --renumber (default 301).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would change without writing.",
        )

    def handle(self, *args, **opts):
        if opts.get("show"):
            self._show(opts["show"])
            return
        if opts.get("renumber"):
            self._renumber(opts["renumber"], opts["start"], opts["dry_run"])
            return
        if opts.get("csv"):
            self._apply_csv(opts["csv"], opts["dry_run"])
            return
        self.stdout.write(self.style.ERROR(
            "Specify one of --show, --csv <path>, or --renumber <Class Stream>."
        ))

    # ── helpers ──────────────────────────────────────────────────────────
    def _parse_class_stream(self, class_stream):
        """'Grade 7 Blue' -> ('Grade 7', 'Blue').  The last token is
        the stream; everything before is the class name. This handles
        'Grade 7', 'Grade 8 Main', 'Grade 1', 'PP1 Red', etc."""
        tokens = class_stream.strip().split()
        if len(tokens) < 2:
            raise ValueError("Pass '<Class> <Stream>' e.g. 'Grade 7 Blue'.")
        stream = tokens[-1]
        grade = " ".join(tokens[:-1])
        return grade, stream

    def _show(self, class_stream):
        try:
            grade, stream = self._parse_class_stream(class_stream)
        except ValueError as e:
            self.stdout.write(self.style.ERROR(str(e)))
            return
        rows = list(Student.all_objects.filter(
            class_name=grade, stream=stream
        ).order_by("admission_no", "name").values(
            "id", "admission_no", "name", "assessment_no",
        ))
        self.stdout.write(
            f"Showing {grade} {stream} ({len(rows)} students):"
        )
        self.stdout.write(
            f"{'adm':>8} {'name':<40} {'id':>6}  assessment_no"
        )
        for r in rows:
            self.stdout.write(
                f'{r["admission_no"] or "":>8} {r["name"]:<40} {r["id"]:>6}  {r["assessment_no"] or ""}'
            )

    @transaction.atomic
    def _renumber(self, class_stream, start, dry_run):
        try:
            grade, stream = self._parse_class_stream(class_stream)
        except ValueError as e:
            self.stdout.write(self.style.ERROR(str(e)))
            return
        sid = transaction.savepoint()
        students = list(Student.all_objects.filter(
            class_name=grade, stream=stream
        ).order_by("name", "id"))
        if not students:
            self.stdout.write(self.style.WARNING("No students found."))
            return
        self.stdout.write(f"Will renumber {len(students)} students of {class_stream} starting at {start}.")
        for i, s in enumerate(students):
            new_no = f"{start + i:03d}"
            if s.admission_no == new_no:
                continue
            self.stdout.write(f'  {s.admission_no or "(empty)":>8} -> {new_no}   {s.name}')
            if not dry_run:
                Student.all_objects.filter(pk=s.pk).update(admission_no=new_no)
        if dry_run:
            transaction.savepoint_rollback(sid)
            self.stdout.write(self.style.WARNING("(dry-run, nothing changed)"))
        else:
            transaction.savepoint_commit(sid)
            self.stdout.write(self.style.SUCCESS("Done."))

    @transaction.atomic
    def _apply_csv(self, path, dry_run):
        sid = transaction.savepoint()
        n_changed = n_missing = n_dup = 0
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            required = {"current_adm", "new_adm"}
            if not required.issubset(reader.fieldnames or []):
                self.stdout.write(self.style.ERROR(
                    f"CSV must have columns: {', '.join(sorted(required))} (got: {reader.fieldnames})"
                ))
                return
            for row in reader:
                current = (row.get("current_adm") or "").strip()
                new = (row.get("new_adm") or "").strip().zfill(3)
                class_name = (row.get("class") or "").strip() or None
                stream = (row.get("stream") or "").strip() or None
                if not current or not new:
                    continue

                qs = Student.all_objects.filter(admission_no=current)
                if class_name:
                    qs = qs.filter(class_name=class_name)
                if stream:
                    qs = qs.filter(stream=stream)
                student = qs.first()
                if not student:
                    self.stdout.write(self.style.WARNING(
                        f"  [skip] no student with adm={current!r}"
                        + (f" in {class_name} {stream}" if class_name else "")
                    ))
                    n_missing += 1
                    continue

                # Collision check: another student may already have new adm
                qs2 = Student.all_objects.filter(
                    admission_no=new, class_name=student.class_name, stream=student.stream
                ).exclude(pk=student.pk)
                collision = qs2.first()
                if collision:
                    self.stdout.write(self.style.WARNING(
                        f"  [skip] {new} already taken by {collision.name} (id={collision.id})"
                    ))
                    n_dup += 1
                    continue

                if student.admission_no == new:
                    continue
                self.stdout.write(
                    f"  {student.admission_no:>8} -> {new}   {student.name}  ({student.class_name} {student.stream})"
                )
                if not dry_run:
                    Student.all_objects.filter(pk=student.pk).update(admission_no=new)
                n_changed += 1
        if dry_run:
            transaction.savepoint_rollback(sid)
            self.stdout.write(self.style.WARNING("(dry-run, nothing changed)"))
        else:
            transaction.savepoint_commit(sid)
        self.stdout.write(self.style.SUCCESS(
            f"Done. changed={n_changed}  missing={n_missing}  duplicate-targets={n_dup}"
        ))
