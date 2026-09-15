from django.core.management.base import BaseCommand

from students.models import Exam, ExamResultSnapshot, Mark, Student, Stream


class Command(BaseCommand):
    help = (
        "Backfill ExamResultSnapshots for all exams that have marks. "
        "This ensures the merit list can read from snapshots instead of "
        "hitting the DB with expensive live computation."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Show what would be built without actually saving.',
        )
        parser.add_argument(
            '--force', action='store_true',
            help='Rebuild snapshots even if one already exists.',
        )

    def handle(self, *args, **options):
        from students.views.helpers import build_exam_result_snapshot

        dry_run = options['dry_run']
        force = options['force']

        # Find all unique (exam, grade, stream) combinations that have marks
        mark_combos = (
            Mark.all_objects.filter(student__isnull=False)
            .values_list(
                'school', 'exam_type', 'term', 'year',
                'student__class_name', 'student__stream',
            )
            .distinct()
        )

        built = 0
        skipped = 0
        errors = 0

        for school_id, exam_name, term, year, grade, stream in mark_combos:
            if not grade or not stream:
                continue

            # Find the Exam object
            try:
                exam = Exam.all_objects.get(
                    school_id=school_id, name=exam_name,
                    term=term, year=year,
                )
            except Exam.DoesNotExist:
                skipped += 1
                continue
            except Exam.MultipleObjectsReturned:
                exam = Exam.all_objects.filter(
                    school_id=school_id, name=exam_name,
                    term=term, year=year,
                ).first()

            # Check if snapshot already exists
            existing = ExamResultSnapshot.get_latest(
                exam.school, term, year, exam_name, grade, stream,
            )
            if existing and not force:
                skipped += 1
                continue

            if existing and force:
                existing.delete()

            if dry_run:
                self.stdout.write(
                    f'  [DRY RUN] Would build: {exam_name} {term}Y{year} '
                    f'{grade}/{stream} (school={school_id})'
                )
                built += 1
                continue

            try:
                snap = build_exam_result_snapshot(
                    exam.school, exam, grade, stream,
                )
                if snap:
                    built += 1
                    self.stdout.write(
                        f'  Built: {exam_name} {term}Y{year} '
                        f'{grade}/{stream} ({snap.student_count} students)'
                    )
                else:
                    skipped += 1
            except Exception as e:
                errors += 1
                self.stdout.write(
                    self.style.ERROR(
                        f'  ERROR: {exam_name} {term}Y{year} '
                        f'{grade}/{stream}: {e}'
                    )
                )

        action = 'Would build' if dry_run else 'Built'
        self.stdout.write(
            self.style.SUCCESS(
                f'\nDone. {action} {built} snapshots. '
                f'Skipped {skipped}. Errors {errors}.'
            )
        )
