# Sync Django state for unique constraints and subject.school_section max_length.
# DB is already correct from 0060 RunSQL; this only updates Django's internal state.

from django.db import migrations, models


def forwards(apps, schema_editor):
    pass  # DB already correct


def reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('students', '0061_subjectassignment_sub_section'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterUniqueTogether(
                    name='marksubmission',
                    unique_together={('school', 'teacher', 'subject', 'class_name', 'stream', 'exam_name', 'term', 'year', 'school_section')},
                ),
                migrations.AlterUniqueTogether(
                    name='subjectassignment',
                    unique_together={('school', 'subject', 'class_name', 'stream')},
                ),
                migrations.AlterField(
                    model_name='subject',
                    name='school_section',
                    field=models.CharField(
                        choices=[('LOWER_PRIMARY', 'Lower Primary'), ('PRIMARY', 'Primary'), ('JSS', 'Junior Secondary')],
                        help_text='Which section this subject belongs to',
                        max_length=20,
                    ),
                ),
            ],
            database_operations=[],
        ),
    ]
