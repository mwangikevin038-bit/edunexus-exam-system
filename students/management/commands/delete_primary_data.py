"""
Management command to delete all Primary section data for a specific school.

Usage:
    python manage.py delete_primary_data --school <school_code>

This will permanently remove all data tagged with school_section='PRIMARY'
for the given school. JSS data is never touched.

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
    help = "Delete all Primary section data for a specific school (irreversible)."

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

        # Count records to be deleted (using all_objects to bypass school scoping)
        counts = {}

        counts["marks"] = Mark.all_objects.filter(
            school=school, school_section="PRIMARY"
        ).count()

        counts["mark_submissions"] = MarkSubmission.all_objects.filter(
            school=school, school_section="PRIMARY"
        ).count()

        counts["subject_assignments"] = SubjectAssignment.all_objects.filter(
            school=school, school_section="PRIMARY"
        ).count()

        counts["class_teacher_comments"] = ClassTeacherMasterComment.all_objects.filter(
            school=school, school_section="PRIMARY"
        ).count()

        counts["headteacher_comments"] = SchoolHeadteacherComment.all_objects.filter(
            school=school, school_section="PRIMARY"
        ).count()

        counts["assessment_locks"] = AssessmentLock.all_objects.filter(
            school=school, school_section="PRIMARY"
        ).count()

        counts["exams"] = Exam.all_objects.filter(
            school=school, school_section="PRIMARY"
        ).count()

        counts["students"] = Student.all_objects.filter(
            school=school, school_section="PRIMARY"
        ).count()

        counts["guardians"] = Guardian.all_objects.filter(
            school=school, school_section="PRIMARY"
        ).count()

        counts["teachers"] = Teacher.all_objects.filter(
            school=school, school_section="PRIMARY"
        ).count()

        counts["subjects"] = Subject.all_objects.filter(
            school=school, school_section="PRIMARY"
        ).count()

        counts["streams"] = Stream.all_objects.filter(
            school=school, school_section="PRIMARY"
        ).count()

        counts["grades"] = Grade.all_objects.filter(
            school=school, school_section="PRIMARY"
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
            self.stdout.write(self.style.SUCCESS("\nNo Primary data found. Nothing to delete."))
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
                school=school, school_section="PRIMARY"
            ).delete()
            self.stdout.write(f"  Deleted {n} Mark records")

            # 2. MarkSubmissions (depends on Teacher, Subject)
            n, _ = MarkSubmission.all_objects.filter(
                school=school, school_section="PRIMARY"
            ).delete()
            self.stdout.write(f"  Deleted {n} MarkSubmission records")

            # 3. SubjectAssignments (depends on Teacher, Subject)
            n, _ = SubjectAssignment.all_objects.filter(
                school=school, school_section="PRIMARY"
            ).delete()
            self.stdout.write(f"  Deleted {n} SubjectAssignment records")

            # 4. ClassTeacherMasterComment
            n, _ = ClassTeacherMasterComment.all_objects.filter(
                school=school, school_section="PRIMARY"
            ).delete()
            self.stdout.write(f"  Deleted {n} ClassTeacherMasterComment records")

            # 5. SchoolHeadteacherComment
            n, _ = SchoolHeadteacherComment.all_objects.filter(
                school=school, school_section="PRIMARY"
            ).delete()
            self.stdout.write(f"  Deleted {n} SchoolHeadteacherComment records")

            # 6. AssessmentLock
            n, _ = AssessmentLock.all_objects.filter(
                school=school, school_section="PRIMARY"
            ).delete()
            self.stdout.write(f"  Deleted {n} AssessmentLock records")

            # 7. Exams
            n, _ = Exam.all_objects.filter(
                school=school, school_section="PRIMARY"
            ).delete()
            self.stdout.write(f"  Deleted {n} Exam records")

            # 8. Students (depends on Guardian)
            n, _ = Student.all_objects.filter(
                school=school, school_section="PRIMARY"
            ).delete()
            self.stdout.write(f"  Deleted {n} Student records")

            # 9. Guardians
            n, _ = Guardian.all_objects.filter(
                school=school, school_section="PRIMARY"
            ).delete()
            self.stdout.write(f"  Deleted {n} Guardian records")

            # 10. Teachers (PRIMARY only - keep BOTH section teachers)
            n, _ = Teacher.all_objects.filter(
                school=school, school_section="PRIMARY"
            ).delete()
            self.stdout.write(f"  Deleted {n} Teacher records (PRIMARY only)")

            # 11. Subjects
            n, _ = Subject.all_objects.filter(
                school=school, school_section="PRIMARY"
            ).delete()
            self.stdout.write(f"  Deleted {n} Subject records")

            # 12. Streams (depends on Grade)
            n, _ = Stream.all_objects.filter(
                school=school, school_section="PRIMARY"
            ).delete()
            self.stdout.write(f"  Deleted {n} Stream records")

            # 13. Grades
            n, _ = Grade.all_objects.filter(
                school=school, school_section="PRIMARY"
            ).delete()
            self.stdout.write(f"  Deleted {n} Grade records")

        self.stdout.write(self.style.SUCCESS(
            f"\nDone! All Primary section data for '{school.name}' has been deleted."
        ))
        self.stdout.write(self.style.SUCCESS("JSS data was NOT affected."))
