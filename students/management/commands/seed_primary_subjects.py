"""
Seed the Subject dictionary table with the 9 rationalized Upper Primary
learning areas for Grade 4, Grade 5, and Grade 6.

CRE and IRE are separate subjects — a student takes one or the other,
never both.

Usage:
    python manage.py seed_primary_subjects          # seed all schools
    python manage.py seed_primary_subjects --school CODE  # seed one school
"""

from django.core.management.base import BaseCommand

from students.models import School, Subject


# ── 9 Rationalized MOE Learning Areas (CRE / IRE split) ───────────────────
PRIMARY_SUBJECTS = [
    ("ENG", "English"),
    ("KIS", "Kiswahili"),
    ("MAT", "Mathematics"),
    ("SCI", "Science and Technology"),
    ("SOC", "Social Studies"),
    ("CRE", "Christian Religious Education"),
    ("IRE", "Islamic Religious Education"),
    ("AGR", "Agriculture and Nutrition"),
    ("CAS", "Creative Arts"),
]

PRIMARY_GRADES = ["Grade 4", "Grade 5", "Grade 6"]


class Command(BaseCommand):
    help = "Seed PRIMARY rationalized subjects into the Subject dictionary table"

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

        for grade in PRIMARY_GRADES:
            for code, name in PRIMARY_SUBJECTS:
                _, was_created = Subject.objects.get_or_create(
                    school=school,
                    code=code,
                    school_section="PRIMARY",
                    grade=grade,
                    defaults={"name": name, "is_active": True},
                )
                if was_created:
                    created += 1
                else:
                    skipped += 1

        return created, skipped
