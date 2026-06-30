"""
Management command to delete all Grade 5 data for a specific school.

Usage:
    python manage.py delete_grade5_data --school <school_code>

This will permanently remove all Grade 5 (PRIMARY section) data for the given school.

WARNING: This is irreversible. Always backup your database first.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from students.models import (
    School,
    Grade,
    Stream,
    Subject,
    Guardian,
    Student,
    Teacher,
    SubjectAssignment,
    Exam,
    Mark,
    MarkSubmission,
    ClassTeacherMasterComment,
    SchoolHeadteacherComment,
    AssessmentLock,
)


class Command(BaseCommand):
    help = "Delete all Grade 5 data for a specific school (irreversible)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--school",
            required=True,
            type=str,
            help="School code (e.g. lungalunga)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without actually deleting anything",
        )

    def handle(self, *args, **options):
        school_code = options["school"]
        dry_run = options["dry_run"]

        try:
            school = School.objects.get(code=school_code)
        except School.DoesNotExist:
            raise CommandError(f"School with code '{school_code}' not found.")

        self.stdout.write(self.style.WARNING(
            f"\n{'[DRY RUN] ' if dry_run else ''}School: {school.name} (code={school.code})"
        ))

        # Get Grade 5 IDs
        grade5_ids = Grade.all_objects.filter(
            school=school, school_section="PRIMARY", name="Grade 5"
        ).values_list('id', flat=True)

        # Count records to be deleted
        counts = {}

        counts["marks"] = Mark.all_objects.filter(
            school=school, school_section="PRIMARY", student__class_name="Grade 5"
        ).count()

        counts["mark_submissions"] = MarkSubmission.all_objects.filter(
            school=school, school_section="PRIMARY", subject__grade="Grade 5"
        ).count()

        counts["subject_assignments"] = SubjectAssignment.all_objects.filter(
            school=school, school_section="PRIMARY", class_name="Grade 5"
        ).count()

        counts["class_teacher_comments"] = ClassTeacherMasterComment.all_objects.filter(
            school=school, school_section="PRIMARY", grade="Grade 5"
        ).count()

        counts["assessment_locks"] = AssessmentLock.all_objects.filter(
            school=school, school_section="PRIMARY", grade="Grade 5"
        ).count()

        counts["students"] = Student.all_objects.filter(
            school=school, school_section="PRIMARY", class_name="Grade 5"
        ).count()

        # Guardians may be shared - count unique guardians for Grade 5 students
        grade5_student_ids = Student.all_objects.filter(
            school=school, school_section="PRIMARY", class_name="Grade 5"
        ).values_list('id', flat=True)
        counts["guardians"] = Guardian.objects.filter(
            students__id__in=grade5_student_ids
        ).distinct().count()

        counts["streams"] = Stream.all_objects.filter(
            school=school, school_section="PRIMARY", grade__name="Grade 5"
        ).count()

        counts["grades"] = Grade.all_objects.filter(
            school=school, school_section="PRIMARY", name="Grade 5"
        ).count()

        # Print summary
        self.stdout.write(self.style.WARNING("\n--- Records to be deleted ---"))
        total = 0
        for model_name, count in counts.items():
            label = model_name.replace("_", " ").title()
            self.stdout.write(f"  {label}: {count}")
            total += count
        self.stdout.write(self.style.WARNING(f"\n  TOTAL: {total} records"))

        if total == 0:
            self.stdout.write(self.style.SUCCESS("\nNo Grade 5 data found. Nothing to delete."))
            return

        if dry_run:
            self.stdout.write(self.style.WARNING(
                "\n[DRY RUN] No changes made. Remove --dry-run to execute deletion."
            ))
            return

        # Confirm
        self.stdout.write(self.style.ERROR(
            "\nThis will PERMANENTLY delete all the above records."
        ))
        confirm = input("Type 'YES' to confirm: ").strip()
        if confirm != "YES":
            self.stdout.write(self.style.WARNING("Aborted."))
            return

        # Execute deletion in a transaction
        with transaction.atomic():
            # 1. Marks (depends on Student, Subject)
            n, _ = Mark.all_objects.filter(
                school=school, school_section="PRIMARY", student__class_name="Grade 5"
            ).delete()
            self.stdout.write(f"  Deleted {n} Mark records")

            # 2. MarkSubmissions (depends on Teacher, Subject)
            n, _ = MarkSubmission.all_objects.filter(
                school=school, school_section="PRIMARY", subject__grade="Grade 5"
            ).delete()
            self.stdout.write(f"  Deleted {n} MarkSubmission records")

            # 3. SubjectAssignments (depends on Teacher, Subject)
            n, _ = SubjectAssignment.all_objects.filter(
                school=school, school_section="PRIMARY", class_name="Grade 5"
            ).delete()
            self.stdout.write(f"  Deleted {n} SubjectAssignment records")

            # 4. ClassTeacherMasterComment
            n, _ = ClassTeacherMasterComment.all_objects.filter(
                school=school, school_section="PRIMARY", grade="Grade 5"
            ).delete()
            self.stdout.write(f"  Deleted {n} ClassTeacherMasterComment records")

            # 5. AssessmentLock
            n, _ = AssessmentLock.all_objects.filter(
                school=school, school_section="PRIMARY", grade="Grade 5"
            ).delete()
            self.stdout.write(f"  Deleted {n} AssessmentLock records")

            # 6. Students (depends on Guardian)
            n, _ = Student.all_objects.filter(
                school=school, school_section="PRIMARY", class_name="Grade 5"
            ).delete()
            self.stdout.write(f"  Deleted {n} Student records")

            # 7. Guardians (only those with no remaining students)
            n, _ = Guardian.objects.filter(students__isnull=True).delete()
            self.stdout.write(f"  Deleted {n} orphaned Guardian records")

            # 8. Streams for Grade 5
            n, _ = Stream.all_objects.filter(
                school=school, school_section="PRIMARY", grade__name="Grade 5"
            ).delete()
            self.stdout.write(f"  Deleted {n} Stream records")

            # 9. Grade 5
            n, _ = Grade.all_objects.filter(
                school=school, school_section="PRIMARY", name="Grade 5"
            ).delete()
            self.stdout.write(f"  Deleted {n} Grade records")

        self.stdout.write(self.style.SUCCESS(
            f"\nDone! All Grade 5 data for '{school.name}' has been deleted."
        ))
        self.stdout.write(self.style.SUCCESS("Other grades were NOT affected."))
