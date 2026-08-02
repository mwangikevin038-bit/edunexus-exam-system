"""
Migration 0070 — Create ExamSummary table for pre-calculated exam snapshots.
"""

from django.db import migrations, models
import django.db.models.deletion
import students.models


class Migration(migrations.Migration):

    dependencies = [
        ('students', '0069_mark_unique_constraint_upsert'),
    ]

    operations = [
        migrations.CreateModel(
            name='ExamSummary',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('term', models.CharField(max_length=20)),
                ('year', models.IntegerField(default=students.models.current_year)),
                ('exam_name', models.CharField(max_length=100, help_text="Exam name (e.g. 'Opener Assessment')")),
                ('school_section', models.CharField(choices=[('PRIMARY', 'Primary'), ('JSS', 'Junior Secondary')], default='JSS', max_length=10)),
                ('sub_section', models.CharField(blank=True, choices=[('LOWER', 'Lower Primary'), ('UPPER', 'Upper Primary')], max_length=10, null=True)),
                ('total_marks', models.PositiveIntegerField(default=0)),
                ('total_points', models.PositiveIntegerField(default=0)),
                ('mean_points', models.DecimalField(decimal_places=2, default=0, max_digits=6)),
                ('subject_count', models.PositiveIntegerField(default=0, help_text='Number of subjects with a captured mark')),
                ('overall_plv', models.CharField(default='-', help_text='Overall performance level (e.g. EXCELLENT, GOOD)', max_length=20)),
                ('stream_rank', models.PositiveIntegerField(default=0, help_text='Rank within the student\'s stream (1 = best)')),
                ('grade_rank', models.PositiveIntegerField(default=0, help_text='Rank across all streams in the grade (1 = best)')),
                ('frozen_class_teacher_comment', models.TextField(blank=True, default='')),
                ('frozen_headteacher_comment', models.TextField(blank=True, default='')),
                ('frozen_closing_date', models.DateField(blank=True, null=True)),
                ('frozen_opening_date', models.DateField(blank=True, null=True)),
                ('integrity_checksum', models.CharField(blank=True, default='', editable=False, max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('school', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='examsummary_records', to='students.school')),
                ('student', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='exam_summaries', to='students.student')),
            ],
            options={
                'verbose_name': 'Exam Summary',
                'verbose_name_plural': 'Exam Summaries',
                'ordering': ['student__admission_no'],
                'unique_together': {('school', 'student', 'term', 'year', 'exam_name', 'school_section', 'sub_section')},
            },
        ),
    ]
