"""
Seed SubjectMergeGroup configuration for INTEGRATED_KNEC exam mode.

Lower Primary (Grade 1-3): 8 timetable subjects -> 5 exam papers
Upper Primary (Grade 4-6): 9 timetable subjects -> 5 exam papers
"""

from django.db import migrations, models


LOWER_PRIMARY_MERGE_DATA = [
    # Merged: English Language Activities + Literacy Activities -> English Language and Literacy Activities
    ('LOWER', 'ENG_LIT', 'English Language and Literacy Activities', 'ELA', 'English Language Activities', 1),
    ('LOWER', 'ENG_LIT', 'English Language and Literacy Activities', 'LIT', 'Literacy Activities', 2),

    # Merged: Environmental + Hygiene/Nutrition + RE -> Environmental and Religious Activities
    ('LOWER', 'ENV_RE', 'Environmental and Religious Activities', 'ENV', 'Environmental Activities', 1),
    ('LOWER', 'ENV_RE', 'Environmental and Religious Activities', 'HYG', 'Hygiene and Nutrition Activities', 2),
    ('LOWER', 'ENV_RE', 'Environmental and Religious Activities', 'CRE', 'Christian Religious Education Activities', 3),
    ('LOWER', 'ENV_RE', 'Environmental and Religious Activities', 'IRE', 'Islamic Religious Education Activities', 4),
    ('LOWER', 'ENV_RE', 'Environmental and Religious Activities', 'HRE', 'Hindu Religious Education Activities', 5),

    # Standalone: Mathematical Activities
    ('LOWER', 'MATH', 'Mathematical Activities', 'MA', 'Mathematical Activities', 1),

    # Standalone: Kiswahili Language Activities
    ('LOWER', 'KIS', 'Kiswahili Language Activities', 'KLA', 'Kiswahili Language Activities', 1),

    # Standalone: Creative Activities
    ('LOWER', 'CRA', 'Creative Activities', 'CRA', 'Creative Activities', 1),
]

UPPER_PRIMARY_MERGE_DATA = [
    # Merged: Science and Technology + Agriculture and Nutrition -> Integrated Science
    ('UPPER', 'INT_SCI', 'Integrated Science', 'SCI', 'Science and Technology', 1),
    ('UPPER', 'INT_SCI', 'Integrated Science', 'AGR', 'Agriculture and Nutrition', 2),

    # Merged: Social Studies + Creative Arts and Sports + RE -> Creative Arts and Social Studies
    ('UPPER', 'CAS_RE', 'Creative Arts and Social Studies', 'SOC', 'Social Studies', 1),
    ('UPPER', 'CAS_RE', 'Creative Arts and Social Studies', 'CAS', 'Creative Arts and Sports', 2),
    ('UPPER', 'CAS_RE', 'Creative Arts and Social Studies', 'CRE', 'Christian Religious Education', 3),
    ('UPPER', 'CAS_RE', 'Creative Arts and Social Studies', 'IRE', 'Islamic Religious Education', 4),

    # Standalone: Mathematics
    ('UPPER', 'MAT', 'Mathematics', 'MAT', 'Mathematics', 1),

    # Standalone: English
    ('UPPER', 'ENG', 'English', 'ENG', 'English', 1),

    # Standalone: Kiswahili
    ('UPPER', 'KISW', 'Kiswahili', 'KIS', 'Kiswahili', 1),
]


def seed_merge_groups(apps, schema_editor):
    SubjectMergeGroup = apps.get_model('students', 'SubjectMergeGroup')

    for row in LOWER_PRIMARY_MERGE_DATA + UPPER_PRIMARY_MERGE_DATA:
        sub_section, group_code, group_name, comp_code, comp_name, order = row
        SubjectMergeGroup.objects.get_or_create(
            sub_section=sub_section,
            merge_group_code=group_code,
            component_code=comp_code,
            defaults={
                'merge_group_name': group_name,
                'component_name': comp_name,
                'display_order': order,
            },
        )


def reverse_seed(apps, schema_editor):
    SubjectMergeGroup = apps.get_model('students', 'SubjectMergeGroup')
    SubjectMergeGroup.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('students', '0055_add_sub_section_and_class_teacher'),
    ]

    operations = [
        migrations.CreateModel(
            name='SubjectMergeGroup',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sub_section', models.CharField(
                    choices=[('LOWER', 'Lower Primary'), ('UPPER', 'Upper Primary')],
                    help_text='Lower Primary (1-3) or Upper Primary (4-6)',
                    max_length=10,
                )),
                ('merge_group_code', models.CharField(
                    help_text="Identifier for the merge group, e.g. 'INT_SCI', 'ENG_LIT'",
                    max_length=20,
                )),
                ('merge_group_name', models.CharField(
                    help_text="Display name of the merged paper, e.g. 'Integrated Science'",
                    max_length=100,
                )),
                ('component_code', models.CharField(
                    help_text="Subject code of the individual component, e.g. 'SCI'",
                    max_length=10,
                )),
                ('component_name', models.CharField(
                    help_text="Display name of the component subject",
                    max_length=100,
                )),
                ('display_order', models.PositiveIntegerField(
                    default=1,
                    help_text="Order within the merge group for display",
                )),
            ],
            options={
                'ordering': ['sub_section', 'merge_group_code', 'display_order'],
                'unique_together': {('sub_section', 'merge_group_code', 'component_code')},
            },
        ),
        migrations.RunPython(seed_merge_groups, reverse_seed),

        # Add exam_report_mode to Exam (NOT NULL, no default)
        migrations.AddField(
            model_name='exam',
            name='exam_report_mode',
            field=models.CharField(
                choices=[
                    ('UNMERGED', 'Unmerged (Individual Subjects)'),
                    ('INTEGRATED_KNEC', 'Integrated KNEC (Merged Papers)'),
                ],
                default='UNMERGED',
                help_text='UNMERGED: show each subject individually. INTEGRATED_KNEC: merge subjects into combined exam papers per KNEC/CBC guidelines.',
                max_length=20,
            ),
            preserve_default=False,
        ),
    ]
