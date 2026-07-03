from django.core.management.base import BaseCommand

from students.models import GradingConfig, Mark
from students.security.integrity import compute_mark_checksum


DEFAULT_PRIMARY_SCALE = [
    (75, 'EE', 4),
    (50, 'ME', 3),
    (25, 'AE', 2),
    (0,  'BE', 1),
]


def get_primary_level(score, school=None):
    """Return (level, points) using the school's saved GradingConfig subject_scale."""
    if school:
        config = GradingConfig.all_objects.filter(
            school=school, school_section='PRIMARY'
        ).first()
        if config and config.subject_scale:
            return config.get_subject_level(score)

    for threshold, level, points in DEFAULT_PRIMARY_SCALE:
        if score >= threshold:
            return level, points
    return 'BE', 1


class Command(BaseCommand):
    help = (
        "Recalculate performance_level and points for all Primary section marks "
        "using the school's saved GradingConfig (subject_scale). "
        "Falls back to default CBC scale if no config exists."
    )

    def handle(self, *args, **options):
        marks = Mark.all_objects.filter(school_section='PRIMARY')
        total = marks.count()
        updated = 0
        config_used = {}

        for mark in marks.iterator():
            old_pl = mark.performance_level
            old_pts = mark.points

            level, points = get_primary_level(mark.score, mark.school)

            school_id = mark.school_id
            if school_id not in config_used:
                config = GradingConfig.all_objects.filter(
                    school=mark.school, school_section='PRIMARY'
                ).first()
                config_used[school_id] = bool(config and config.subject_scale)

            if level != old_pl or points != old_pts:
                mark.performance_level = level
                mark.points = points
                mark.integrity_checksum = compute_mark_checksum(mark)
                mark.save(update_fields=[
                    'performance_level', 'points', 'integrity_checksum'
                ])
                updated += 1

        for sid, used in config_used.items():
            source = "GradingConfig" if used else "default CBC scale"
            self.stdout.write(f"  School {sid}: used {source}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Recalculated grading for {total} Primary marks. "
                f"{updated} updated, {total - updated} unchanged."
            )
        )
