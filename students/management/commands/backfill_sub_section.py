"""
Backfill sub_section for all PRIMARY section records.

Derives sub_section from grade:
  Grade 1-3 -> 'LOWER'
  Grade 4-6 -> 'UPPER'

Updates: Teachers, SubjectAssignments, MarkSubmissions.

Usage:
    python manage.py backfill_sub_section
    python manage.py backfill_sub_section --school CODE
    python manage.py backfill_sub_section --dry-run
"""

from django.core.management.base import BaseCommand
from students.models import (
    Teacher, SubjectAssignment, MarkSubmission, Student, Mark, Exam
)


LOWER_GRADES = {'Grade 1', 'Grade 2', 'Grade 3'}
UPPER_GRADES = {'Grade 4', 'Grade 5', 'Grade 6'}


def derive_sub_section(class_name):
    if class_name in LOWER_GRADES:
        return 'LOWER'
    elif class_name in UPPER_GRADES:
        return 'UPPER'
    return None


class Command(BaseCommand):
    help = "Backfill sub_section for PRIMARY section records"

    def add_arguments(self, parser):
        parser.add_argument("--school", type=str, help="School code (optional)")
        parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")

    def handle(self, *args, **options):
        school_code = options.get("school")
        dry_run = options.get("dry_run", False)

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be written"))

        # 1. SubjectAssignments
        sa_qs = SubjectAssignment.all_objects.filter(school_section='PRIMARY', sub_section__isnull=True)
        if school_code:
            sa_qs = sa_qs.filter(school__code=school_code)
        sa_count = 0
        for sa in sa_qs:
            new_sub = derive_sub_section(sa.class_name)
            if new_sub:
                if not dry_run:
                    sa.sub_section = new_sub
                    sa.save(update_fields=['sub_section'])
                sa_count += 1
                self.stdout.write(f"  SubjectAssignment pk={sa.pk} class={sa.class_name} -> sub_section={new_sub}")
        self.stdout.write(f"SubjectAssignments: {sa_count} updated")

        # 2. Teachers — derive from their SubjectAssignments
        t_qs = Teacher.all_objects.filter(school_section='PRIMARY', sub_section__isnull=True)
        if school_code:
            t_qs = t_qs.filter(school__code=school_code)
        t_count = 0
        for teacher in t_qs:
            assignments = SubjectAssignment.all_objects.filter(
                teacher_profile=teacher, school_section='PRIMARY', sub_section__isnull=False
            )
            sub_sections = set(a.sub_section for a in assignments if a.sub_section)
            if len(sub_sections) == 1:
                new_sub = sub_sections.pop()
            elif len(sub_sections) > 1:
                # Teacher spans both — default to UPPER
                new_sub = 'UPPER'
            else:
                # No assignments with sub_section — check grade names from all assignments
                all_assignments = SubjectAssignment.all_objects.filter(
                    teacher_profile=teacher, school_section='PRIMARY'
                )
                grades = set(a.class_name for a in all_assignments)
                if grades & UPPER_GRADES:
                    new_sub = 'UPPER'
                elif grades & LOWER_GRADES:
                    new_sub = 'LOWER'
                else:
                    new_sub = 'UPPER'  # default
            if not dry_run:
                teacher.sub_section = new_sub
                teacher.save(update_fields=['sub_section'])
            t_count += 1
            self.stdout.write(f"  Teacher pk={teacher.pk} name={teacher} -> sub_section={new_sub}")
        self.stdout.write(f"Teachers: {t_count} updated")

        # 3. MarkSubmissions with NULL sub_section
        ms_qs = MarkSubmission.all_objects.filter(school_section='PRIMARY', sub_section__isnull=True)
        if school_code:
            ms_qs = ms_qs.filter(school__code=school_code)
        ms_count = 0
        for ms in ms_qs:
            # Try to derive from the linked assignment
            if ms.subject and ms.class_name:
                new_sub = derive_sub_section(ms.class_name)
                if new_sub:
                    if not dry_run:
                        ms.sub_section = new_sub
                        ms.save(update_fields=['sub_section'])
                    ms_count += 1
                    self.stdout.write(f"  MarkSubmission pk={ms.pk} class={ms.class_name} -> sub_section={new_sub}")
        self.stdout.write(f"MarkSubmissions: {ms_count} updated")

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Teachers: {t_count}, SubjectAssignments: {sa_count}, MarkSubmissions: {ms_count}"
        ))
