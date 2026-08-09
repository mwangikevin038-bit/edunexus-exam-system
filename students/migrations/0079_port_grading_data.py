"""
Data migration: Port existing GradingConfig rows into the new
GradingScale + GradingAssignment architecture.

For each GradingConfig row:
  1. Create a GradingScale with the same subject_scale/total_scale JSON
  2. Create a GradingAssignment linking the scale to the section+subject

This preserves all historical grading data while migrating to the new schema.
"""
from django.db import migrations


def port_grading_configs(apps, schema_editor):
    GradingConfig = apps.get_model('students', 'GradingConfig')
    GradingScale = apps.get_model('students', 'GradingScale')
    GradingAssignment = apps.get_model('students', 'GradingAssignment')

    created_scales = 0
    created_assignments = 0

    for cfg in GradingConfig.objects.all().order_by('school_id', 'school_section', 'sub_section'):
        # Step 1: Create a GradingScale from the config's JSON data
        scale_name = cfg.name or f"{cfg.get_school_section_display()} Default"
        scale = GradingScale.objects.create(
            school_id=cfg.school_id,
            name=scale_name,
            subject_scale=cfg.subject_scale or [],
            total_scale=cfg.total_scale or [],
        )
        created_scales += 1

        # Step 2: Create a GradingAssignment (general fallback — subject=NULL)
        GradingAssignment.objects.create(
            school_id=cfg.school_id,
            school_section=cfg.school_section,
            sub_section=cfg.sub_section,
            subject=None,  # general fallback
            grading_scale=scale,
        )
        created_assignments += 1

    print(f"  Created {created_scales} GradingScale rows")
    print(f"  Created {created_assignments} GradingAssignment rows")


def reverse_port(apps, schema_editor):
    """Reverse migration — clear the new tables (old GradingConfig untouched)."""
    GradingScale = apps.get_model('students', 'GradingScale')
    GradingAssignment = apps.get_model('students', 'GradingAssignment')
    GradingAssignment.objects.all().delete()
    GradingScale.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('students', '0078_add_grading_scale_assignment'),
    ]

    operations = [
        migrations.RunPython(port_grading_configs, reverse_port),
    ]
