"""
Seed the Subject dictionary table with Lower Primary learning areas
for Grade 1, Grade 2, and Grade 3.

Lower Primary has 6 merged subject groups:
  1. English Language Activities (ELA)
  2. Kiswahili Language Activities (KLA)
  3. Mathematical Activities (MA)
  4. Environmental Activities (ENV) + Hygiene and Nutrition (HYG)
  5. Religious Education Activities (CRE/IRE/HRE)
  6. Creative Activities (CRA)

Usage:
    python manage.py seed_lower_primary_subjects
    python manage.py seed_lower_primary_subjects --school CODE
"""

from django.core.management.base import BaseCommand

from students.models import School, Subject


LOWER_PRIMARY_SUBJECTS = [
    ("ELA", "English Language Activities"),
    ("KLA", "Kiswahili Language Activities"),
    ("MA", "Mathematical Activities"),
    ("ENV", "Environmental Activities"),
    ("HYG", "Hygiene and Nutrition Activities"),
    ("CRE", "Christian Religious Education Activities"),
    ("IRE", "Islamic Religious Education Activities"),
    ("HRE", "Hindu Religious Education Activities"),
    ("CRA", "Creative Activities"),
]

LOWER_PRIMARY_GRADES = ["Grade 1", "Grade 2", "Grade 3"]


class Command(BaseCommand):
    help = "Seed LOWER PRIMARY subjects into the Subject dictionary table"

    def add_arguments(self, parser):
        parser.add_argument(
            "--school",
            type=str,
            help="School code to seed. If omitted, seeds ALL active schools.",
        )

    def handle(self, *args, **options):
        school_code = options.get("school")

        if school_code:
            schools = School.objects.filter(code=school_code, status="active")
            if not schools.exists():
                self.stderr.write(self.style.ERROR(f"School '{school_code}' not found or inactive."))
                return
        else:
            schools = School.objects.filter(status="active")

        created_total = 0
        skipped_total = 0

        for school in schools:
            created, skipped = self._seed_school(school)
            created_total += created
            skipped_total += skipped
            self.stdout.write(
                f"  {school.name}: +{created} created, {skipped} already existed"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. {created_total} subjects created, {skipped_total} skipped (already exist)."
            )
        )

    def _seed_school(self, school):
        created = 0
        skipped = 0

        for grade in LOWER_PRIMARY_GRADES:
            for code, name in LOWER_PRIMARY_SUBJECTS:
                _, was_created = Subject.objects.get_or_create(
                    school=school,
                    code=code,
                    school_section="LOWER_PRIMARY",
                    grade=grade,
                    defaults={"name": name, "is_active": True},
                )
                if was_created:
                    created += 1
                else:
                    skipped += 1

        return created, skipped
