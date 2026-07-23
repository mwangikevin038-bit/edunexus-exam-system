"""
Ensure Grade 1-3 records exist with sub_section='LOWER' and have streams.

Usage:
    python manage.py fix_lower_primary_grades
    python manage.py fix_lower_primary_grades --dry-run
"""

from django.core.management.base import BaseCommand
from students.models import School, Grade, Stream


LOWER_GRADES = ['Grade 1', 'Grade 2', 'Grade 3']
DEFAULT_STREAM = 'Main'


class Command(BaseCommand):
    help = "Create Grade 1-3 records with sub_section='LOWER' and default streams"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN"))

        schools = School.objects.all()
        for school in schools:
            self.stdout.write(f"\nSchool: {school.name} ({school.code})")
            for idx, grade_name in enumerate(LOWER_GRADES, start=1):
                grade, created = Grade.all_objects.get_or_create(
                    school=school,
                    name=grade_name,
                    defaults={
                        'school_section': 'PRIMARY',
                        'sub_section': 'LOWER',
                        'order': idx,
                    },
                )
                if created:
                    self.stdout.write(f"  CREATED Grade {grade_name} (pk={grade.pk})")
                elif grade.sub_section != 'LOWER':
                    if not dry_run:
                        grade.sub_section = 'LOWER'
                        grade.school_section = 'PRIMARY'
                        grade.save(update_fields=['sub_section', 'school_section'])
                    self.stdout.write(f"  FIXED Grade {grade_name}: sub_section -> LOWER (pk={grade.pk})")
                else:
                    self.stdout.write(f"  OK    Grade {grade_name} (pk={grade.pk})")

                stream, s_created = Stream.all_objects.get_or_create(
                    school=school,
                    grade=grade,
                    name=DEFAULT_STREAM,
                    defaults={'school_section': 'PRIMARY'},
                )
                if s_created:
                    self.stdout.write(f"  CREATED stream '{DEFAULT_STREAM}' for {grade_name}")

        self.stdout.write(self.style.SUCCESS("\nDone."))
