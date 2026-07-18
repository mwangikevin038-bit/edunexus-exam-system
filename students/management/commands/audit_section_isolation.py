"""
Audit section isolation across the database.

Reports:
  - Teachers whose school_section is missing/invalid
  - Students whose class_name doesn't match their school_section
  - Marks / MarkSubmissions whose school_section doesn't match the student's
  - SubjectAssignments whose class_name / school_section disagrees with the section
  - CSV-shaped violations: any row in any model with a class outside its
    declared school_section

Usage:
    python manage.py audit_section_isolation
    python manage.py audit_section_isolation --fix        # attempt to fix correctable cases
    python manage.py audit_section_isolation --json       # machine-readable output
"""
import json
import sys

from django.core.management.base import BaseCommand

from students.models import (
    Exam,
    Grade,
    Mark,
    MarkSubmission,
    School,
    Student,
    Subject,
    SubjectAssignment,
    Teacher,
)
from students.views.constants import (
    JSS_CLASSES,
    LOWER_PRIMARY_CLASSES,
    UPPER_PRIMARY_CLASSES,
    section_for_class,
)


def _expected_pair(class_name):
    """Return (school_section, sub_section) that class_name MUST belong to."""
    return section_for_class(class_name)


def _class_to_sub_sections():
    return {
        'LOWER_PRIMARY': LOWER_PRIMARY_CLASSES,
        'UPPER_PRIMARY': UPPER_PRIMARY_CLASSES,
        'JSS':           JSS_CLASSES,
    }


class Command(BaseCommand):
    help = "Audit section isolation: find rows whose class_name disagrees with school_section."

    def add_arguments(self, parser):
        parser.add_argument("--fix", action="store_true",
                            help="Attempt to correct correctable mismatches in place.")
        parser.add_argument("--json", action="store_true",
                            help="Emit JSON to stdout instead of human-readable.")

    def handle(self, *args, **opts):
        as_json = opts["json"]
        do_fix = opts["fix"]

        findings = {
            "students_bad_section": [],
            "students_missing_sub_section": [],
            "teachers_missing_section": [],
            "subjects_bad_section": [],
            "subject_assignments_bad_section": [],
            "exams_bad_section": [],
            "marks_bad_section": [],
            "mark_submissions_bad_section": [],
        }

        # ── Students ──────────────────────────────────────────────────────
        for s in Student.all_objects.all().only(
            "id", "name", "admission_no", "class_name",
            "school_section", "sub_section", "school_id",
        ):
            exp_section, exp_sub = _expected_pair(s.class_name)
            if exp_section is None:
                # class_name outside our canonical set; ignore
                continue
            if s.school_section != exp_section:
                findings["students_bad_section"].append({
                    "id": s.id, "admission_no": s.admission_no, "name": s.name,
                    "class_name": s.class_name,
                    "school_section": s.school_section,
                    "expected": exp_section,
                })
                if do_fix:
                    s.school_section = exp_section
                    s.sub_section = exp_sub or ""
                    s.save(update_fields=["school_section", "sub_section"])
            if exp_section == "PRIMARY" and (s.sub_section or "").upper() != (exp_sub or "").upper():
                findings["students_missing_sub_section"].append({
                    "id": s.id, "admission_no": s.admission_no, "name": s.name,
                    "class_name": s.class_name,
                    "sub_section": s.sub_section,
                    "expected": exp_sub,
                })
                if do_fix:
                    s.sub_section = exp_sub
                    s.save(update_fields=["sub_section"])

        # ── Teachers ──────────────────────────────────────────────────────
        for t in Teacher.all_objects.all().only(
            "id", "user__username", "school_section", "sub_section",
        ):
            if t.school_section not in ("PRIMARY", "JSS", "BOTH"):
                findings["teachers_missing_section"].append({
                    "id": t.id, "user": getattr(t.user, "username", None),
                    "school_section": t.school_section, "sub_section": t.sub_section,
                })
            elif t.school_section == "PRIMARY" and t.sub_section not in ("LOWER", "UPPER"):
                # PRIMARY teacher without a sub_section flag — we can recover
                # from their classes, but only via --fix if we want to infer.
                findings["teachers_missing_section"].append({
                    "id": t.id, "user": getattr(t.user, "username", None),
                    "school_section": t.school_section, "sub_section": t.sub_section,
                    "note": "PRIMARY teacher missing LOWER/UPPER sub_section",
                })

        # ── Subjects ──────────────────────────────────────────────────────
        for subj in Subject.all_objects.all().only(
            "id", "code", "name", "grade", "school_section", "sub_section",
        ):
            exp_section, exp_sub = _expected_pair(subj.grade)
            if exp_section is None:
                continue
            if subj.school_section != exp_section:
                findings["subjects_bad_section"].append({
                    "id": subj.id, "code": subj.code, "name": subj.name,
                    "grade": subj.grade, "school_section": subj.school_section,
                    "expected": exp_section,
                })
                if do_fix:
                    subj.school_section = exp_section
                    subj.sub_section = exp_sub or None
                    subj.save(update_fields=["school_section", "sub_section"])

        # ── SubjectAssignments ───────────────────────────────────────────
        for sa in SubjectAssignment.all_objects.all().only(
            "id", "class_name", "stream", "school_section", "sub_section", "subject_id",
        ):
            exp_section, exp_sub = _expected_pair(sa.class_name)
            if exp_section is None:
                continue
            if sa.school_section != exp_section:
                findings["subject_assignments_bad_section"].append({
                    "id": sa.id, "class_name": sa.class_name, "stream": sa.stream,
                    "school_section": sa.school_section, "expected": exp_section,
                })
                if do_fix:
                    sa.school_section = exp_section
                    sa.sub_section = exp_sub or None
                    sa.save(update_fields=["school_section", "sub_section"])

        # ── Exams ─────────────────────────────────────────────────────────
        for e in Exam.all_objects.all().only(
            "id", "name", "school_section", "sub_section",
        ):
            # Exams don't carry a class_name, so we can only check that
            # sub_section matches school_section's canonical form.
            if e.school_section == "JSS" and e.sub_section not in (None, "", "JSS"):
                findings["exams_bad_section"].append({
                    "id": e.id, "name": e.name, "school_section": e.school_section,
                    "sub_section": e.sub_section, "note": "JSS exam has a sub_section",
                })
            elif e.school_section == "PRIMARY" and (e.sub_section or "").upper() not in ("LOWER", "UPPER"):
                findings["exams_bad_section"].append({
                    "id": e.id, "name": e.name, "school_section": e.school_section,
                    "sub_section": e.sub_section, "note": "PRIMARY exam missing LOWER/UPPER",
                })

        # ── Marks / MarkSubmissions: cross-check vs student ───────────────
        for m in Mark.all_objects.select_related("student").only(
            "id", "school_section", "sub_section", "student__class_name",
            "student__school_section", "student__sub_section",
        ).iterator(chunk_size=2000):
            s = m.student
            if s is None:
                continue
            if m.school_section != s.school_section:
                findings["marks_bad_section"].append({
                    "id": m.id, "mark_section": m.school_section,
                    "mark_sub": m.sub_section,
                    "student_section": s.school_section,
                    "student_sub": s.sub_section,
                })
                if do_fix and s.school_section:
                    m.school_section = s.school_section
                    m.sub_section = s.sub_section
                    m.save(update_fields=["school_section", "sub_section"])

        for ms in MarkSubmission.all_objects.select_related("teacher").only(
            "id", "school_section", "sub_section", "teacher__school_section",
            "teacher__sub_section",
        ).iterator(chunk_size=2000):
            # Cross-check with teacher's section if teacher exists
            t = ms.teacher
            if t and t.school_section in ("PRIMARY", "JSS"):
                if ms.school_section != t.school_section:
                    findings["mark_submissions_bad_section"].append({
                        "id": ms.id, "ms_section": ms.school_section,
                        "ms_sub": ms.sub_section,
                        "teacher_section": t.school_section,
                        "teacher_sub": t.sub_section,
                    })

        if as_json:
            self.stdout.write(json.dumps(findings, indent=2, default=str))
        else:
            self._print_human(findings, do_fix)
        # Non-zero exit if anything was found
        total = sum(len(v) for v in findings.values())
        sys.exit(1 if total > 0 and not do_fix else 0)

    def _print_human(self, findings, do_fix):
        self.stdout.write(self.style.NOTICE(
            f"\n=== Section Isolation Audit (fix={'ON' if do_fix else 'OFF'}) ===\n"
        ))
        any_problems = False
        for label, items in findings.items():
            if not items:
                self.stdout.write(self.style.SUCCESS(f"  OK  {label}: 0"))
                continue
            any_problems = True
            self.stdout.write(self.style.ERROR(
                f"  FAIL {label}: {len(items)} (showing first 20)"
            ))
            for it in items[:20]:
                self.stdout.write(f"    {it}")
        if not any_problems:
            self.stdout.write(self.style.SUCCESS("\nAll section invariants hold."))
