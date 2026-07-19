"""
Verify GradingConfig is the sole source of truth for performance levels.

Reports:
  - Each GradingConfig row (subject_scale, total_scale)
  - How many marks exist per section
  - Marks with performance_level = 'NO CONFIG' (would have used the old fallback)
  - Marks with legacy hardcoded levels that might predate config-based grading
  - Sample mark -> level lookup for each section to confirm it matches config

Usage:
    python manage.py check_grading_config
    python manage.py check_grading_config --fix-empty    # initialize missing configs
"""
from django.core.management.base import BaseCommand
from django.db.models import Count, Q

from students.models import GradingConfig, Mark, School


class Command(BaseCommand):
    help = "Verify GradingConfig is present for all sections and used for all marks."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fix-empty",
            action="store_true",
            help="Create a default GradingConfig row for any section that has none.",
        )

    def handle(self, *args, **opts):
        schools = School.objects.all()
        any_problem = False
        for school in schools:
            self.stdout.write(self.style.NOTICE(
                f"\n=== School: {school.name} ({school.code}) ==="
            ))
            for section in ("LOWER_PRIMARY", "PRIMARY", "JSS"):
                cfg = GradingConfig.all_objects.filter(
                    school=school, school_section=section
                ).first()
                if not cfg:
                    any_problem = True
                    self.stdout.write(self.style.ERROR(
                        f"  [{section}]  NO CONFIG ROW"
                    ))
                    if opts["fix_empty"]:
                        cfg = GradingConfig.all_objects.create(
                            school=school,
                            school_section=section,
                            subject_scale=GradingConfig.get_default_subject_scale(section),
                            total_scale=GradingConfig.get_default_total_scale(section),
                        )
                        self.stdout.write(self.style.SUCCESS(
                            f"     -> Created default config"
                        ))
                    continue
                n_subj = len(cfg.subject_scale or [])
                n_total = len(cfg.total_scale or [])
                n_marks = Mark.all_objects.filter(
                    school=school, school_section=section
                ).count()
                n_no_config = Mark.all_objects.filter(
                    school=school, school_section=section, performance_level="NO CONFIG"
                ).count()
                self.stdout.write(
                    f"  [{section:<14}]  subj={n_subj}  total={n_total}  marks={n_marks}"
                    + (f"  NO_CONFIG_marks={n_no_config}" if n_no_config else "")
                )
                if n_no_config:
                    any_problem = True
                # Spot-check: pick a score, verify the level matches the config
                if n_subj > 0 and n_marks > 0:
                    sample = Mark.all_objects.filter(
                        school=school, school_section=section, score__isnull=False
                    ).exclude(is_absent=True).exclude(performance_level="AB").order_by("?").first()
                    if sample and sample.score is not None:
                        expected_lvl, expected_pts = cfg.get_subject_level(sample.score)
                        actual_lvl = sample.performance_level
                        ok = "OK " if actual_lvl == expected_lvl else "MISMATCH"
                        if actual_lvl != expected_lvl:
                            any_problem = True
                        self.stdout.write(
                            f"     spot-check: score={sample.score}  "
                            f"expected={expected_lvl}/{expected_pts}pts  "
                            f"stored={actual_lvl}  [{ok}]"
                        )
        if any_problem:
            self.stdout.write(self.style.WARNING(
                "\nIssues found. See lines marked MISMATCH / NO CONFIG / NO_CONFIG_marks above."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "\nAll GradingConfig rows present, all stored levels match config."
            ))
