"""
Safe management command to append section suffixes to student admission numbers.

  PRIMARY (Grades 1-6): "247"  → "247P"
  JSS     (Grades 7-9): "247"  → "247J"

Students whose admission_no already ends with 'P' or 'J' are skipped.

Usage:
    python manage.py suffix_admission_numbers              # dry-run (default)
    python manage.py suffix_admission_numbers --execute    # apply changes
"""

import sys

from django.core.management.base import BaseCommand
from django.db import transaction

from students.models import Student


PRIMARY_GRADES = {"Grade 1", "Grade 2", "Grade 3", "Grade 4", "Grade 5", "Grade 6"}
JSS_GRADES = {"Grade 7", "Grade 8", "Grade 9"}


class Command(BaseCommand):
    help = "Append 'P' or 'J' suffix to admission numbers based on student grade/section."

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute",
            action="store_true",
            default=False,
            help="Actually apply changes. Without this flag, runs as a dry-run.",
        )

    def handle(self, *args, **options):
        execute = options["execute"]

        if not execute:
            self.stdout.write(self.style.WARNING(
                "=== DRY RUN MODE ===  No data will be modified.\n"
                "Re-run with --execute to apply changes.\n"
            ))

        students = Student.all_objects.order_by("id").only("id", "name", "admission_no", "class_name")

        total = students.count()
        to_update = 0
        skipped_already_suffixed = 0
        skipped_no_grade = 0
        updated = 0
        errors = 0

        self.stdout.write(f"Processing {total} students...\n")

        # Collect all changes first so we can validate before writing
        changes = []

        for student in students:
            adm = student.admission_no or ""
            grade = student.class_name

            # Determine expected suffix
            if grade in PRIMARY_GRADES:
                suffix = "P"
            elif grade in JSS_GRADES:
                suffix = "J"
            else:
                skipped_no_grade += 1
                self.stdout.write(self.style.NOTICE(
                    "  SKIP  id=%-5d  adm=%-8s  class=%-10s  name=%s  (unrecognised class)" % (
                        student.id, repr(adm), grade, student.name
                    )
                ))
                continue

            # Already has the correct suffix?
            if adm.endswith(suffix):
                skipped_already_suffixed += 1
                continue

            new_adm = adm + suffix
            to_update += 1

            self.stdout.write(
                "  %s  id=%-5d  adm=%-8s -> %-8s  class=%-10s  name=%s" % (
                    "WOULD UPDATE" if not execute else "UPDATE     ",
                    student.id,
                    repr(adm),
                    repr(new_adm),
                    grade,
                    student.name,
                )
            )

            changes.append((student, adm, new_adm))

        # Summary before applying
        self.stdout.write("\n" + "-" * 70)
        self.stdout.write("  Total students:            %d" % total)
        self.stdout.write("  Already suffixed (skip):   %d" % skipped_already_suffixed)
        self.stdout.write("  Unrecognised class (skip): %d" % skipped_no_grade)
        self.stdout.write("  To update:                 %d" % to_update)
        self.stdout.write("-" * 70 + "\n")

        if not changes:
            self.stdout.write(self.style.SUCCESS("Nothing to update. All admission numbers are already suffixed."))
            return

        if not execute:
            self.stdout.write(self.style.WARNING(
                "DRY RUN complete. Re-run with --execute to apply the above changes."
            ))
            return

        # ── Apply changes inside a single transaction ──────────────────────
        try:
            with transaction.atomic():
                for student, old_adm, new_adm in changes:
                    # Re-fetch inside the transaction for safety
                    fresh = Student.all_objects.select_for_update().get(pk=student.pk)

                    # Double-check: only modify admission_no
                    fresh.admission_no = new_adm
                    fresh.save(update_fields=["admission_no"])

                    updated += 1
                    self.stdout.write(self.style.SUCCESS(
                        "  SAVED  id=%-5d  %s -> %s  %s" % (
                            fresh.id, repr(old_adm), repr(new_adm), fresh.name
                        )
                    ))

            self.stdout.write(self.style.SUCCESS(
                f"\nDone. {updated}/{to_update} admission numbers updated successfully."
            ))

        except Exception as exc:
            self.stdout.write(self.style.ERROR(
                f"\nERROR: {exc}\nAll changes have been rolled back. No data was modified."
            ))
            sys.exit(1)
