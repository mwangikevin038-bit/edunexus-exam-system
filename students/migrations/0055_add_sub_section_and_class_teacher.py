# Generated manually for sub_section + ClassTeacherAssignment

from django.db import migrations, models
import django.db.models.deletion
import students.models


class Migration(migrations.Migration):

    dependencies = [
        ('students', '0054_finalize_subject_fks'),
    ]

    operations = [
        # ── Grade: add sub_section ─────────────────────────────────
        migrations.AddField(
            model_name='grade',
            name='sub_section',
            field=models.CharField(
                blank=True,
                choices=[('LOWER', 'Lower Primary'), ('UPPER', 'Upper Primary')],
                help_text='Lower Primary (1-3) or Upper Primary (4-6). NULL for JSS.',
                max_length=10,
                null=True,
            ),
        ),

        # ── Student: add sub_section ───────────────────────────────
        migrations.AddField(
            model_name='student',
            name='sub_section',
            field=models.CharField(
                blank=True,
                choices=[('LOWER', 'Lower Primary'), ('UPPER', 'Upper Primary')],
                help_text='Lower Primary (1-3) or Upper Primary (4-6). NULL for JSS.',
                max_length=10,
                null=True,
            ),
        ),

        # ── Subject: add sub_section ───────────────────────────────
        migrations.AddField(
            model_name='subject',
            name='sub_section',
            field=models.CharField(
                blank=True,
                choices=[('LOWER', 'Lower Primary'), ('UPPER', 'Upper Primary')],
                help_text='Lower Primary (1-3) or Upper Primary (4-6). NULL for JSS.',
                max_length=10,
                null=True,
            ),
        ),

        # ── Teacher: add sub_section ───────────────────────────────
        migrations.AddField(
            model_name='teacher',
            name='sub_section',
            field=models.CharField(
                blank=True,
                choices=[('LOWER', 'Lower Primary'), ('UPPER', 'Upper Primary')],
                help_text='Lower Primary (1-3) or Upper Primary (4-6). NULL for JSS/Both.',
                max_length=10,
                null=True,
            ),
        ),

        # ── Exam: add sub_section ──────────────────────────────────
        migrations.AddField(
            model_name='exam',
            name='sub_section',
            field=models.CharField(
                blank=True,
                choices=[('LOWER', 'Lower Primary'), ('UPPER', 'Upper Primary')],
                help_text='Lower Primary (1-3) or Upper Primary (4-6). NULL for JSS.',
                max_length=10,
                null=True,
            ),
        ),

        # ── Mark: add sub_section ──────────────────────────────────
        migrations.AddField(
            model_name='mark',
            name='sub_section',
            field=models.CharField(
                blank=True,
                choices=[('LOWER', 'Lower Primary'), ('UPPER', 'Upper Primary')],
                help_text='Lower Primary (1-3) or Upper Primary (4-6). NULL for JSS.',
                max_length=10,
                null=True,
            ),
        ),

        # ── MarkSubmission: add sub_section ────────────────────────
        migrations.AddField(
            model_name='marksubmission',
            name='sub_section',
            field=models.CharField(
                blank=True,
                choices=[('LOWER', 'Lower Primary'), ('UPPER', 'Upper Primary')],
                help_text='Lower Primary (1-3) or Upper Primary (4-6). NULL for JSS.',
                max_length=10,
                null=True,
            ),
        ),

        # ── AssessmentLock: add sub_section ────────────────────────
        migrations.AddField(
            model_name='assessmentlock',
            name='sub_section',
            field=models.CharField(
                blank=True,
                choices=[('LOWER', 'Lower Primary'), ('UPPER', 'Upper Primary')],
                help_text='Lower Primary (1-3) or Upper Primary (4-6). NULL for JSS.',
                max_length=10,
                null=True,
            ),
        ),

        # ── ClassTeacherAssignment: new model ─────────────────────
        migrations.CreateModel(
            name='ClassTeacherAssignment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('year', models.IntegerField(default=students.models.current_year)),
                ('term', models.CharField(max_length=20)),
                ('school', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='%(class)s_records',
                    to='students.school',
                )),
                ('teacher', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='class_teacher_assignments',
                    to='students.teacher',
                )),
                ('stream', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='class_teacher_assignments',
                    to='students.stream',
                )),
            ],
            options={
                'ordering': ['-year', 'term'],
                'unique_together': {('school', 'stream', 'year', 'term')},
            },
        ),

        # ── Backfill Grade sub_section ─────────────────────────────
        migrations.RunSQL(
            sql=[
                "UPDATE students_grade SET sub_section = 'LOWER' WHERE CAST(SUBSTRING(name FROM 7) AS INTEGER) BETWEEN 1 AND 3 AND school_section = 'PRIMARY';",
                "UPDATE students_grade SET sub_section = 'UPPER' WHERE CAST(SUBSTRING(name FROM 7) AS INTEGER) BETWEEN 4 AND 6 AND school_section = 'PRIMARY';",
                "UPDATE students_grade SET sub_section = NULL WHERE school_section = 'JSS';",
            ],
            reverse_sql=[
                "UPDATE students_grade SET sub_section = NULL;",
            ],
        ),

        # ── Backfill Student sub_section (from Grade) ──────────────
        migrations.RunSQL(
            sql=[
                """
                UPDATE students_student s
                SET sub_section = g.sub_section
                FROM students_grade g
                WHERE s.class_name = g.name
                  AND s.school_id = g.school_id;
                """,
            ],
            reverse_sql=[
                "UPDATE students_student SET sub_section = NULL;",
            ],
        ),

        # ── Backfill Subject sub_section (from grade field) ────────
        migrations.RunSQL(
            sql=[
                """
                UPDATE students_subject sub
                SET sub_section = g.sub_section
                FROM students_grade g
                WHERE sub.grade = g.name
                  AND sub.school_id = g.school_id;
                """,
            ],
            reverse_sql=[
                "UPDATE students_subject SET sub_section = NULL;",
            ],
        ),

        # ── Backfill Mark sub_section (from Student) ───────────────
        migrations.RunSQL(
            sql=[
                """
                UPDATE students_mark m
                SET sub_section = s.sub_section
                FROM students_student s
                WHERE m.student_id = s.id;
                """,
            ],
            reverse_sql=[
                "UPDATE students_mark SET sub_section = NULL;",
            ],
        ),

        # ── Backfill MarkSubmission sub_section (from Student) ─────
        migrations.RunSQL(
            sql=[
                """
                UPDATE students_marksubmission ms
                SET sub_section = s.sub_section
                FROM students_student s
                WHERE ms.class_name = s.class_name
                  AND ms.stream = s.stream
                  AND ms.school_id = s.school_id;
                """,
            ],
            reverse_sql=[
                "UPDATE students_marksubmission SET sub_section = NULL;",
            ],
        ),

        # ── Backfill Exam sub_section (from school_section) ────────
        migrations.RunSQL(
            sql=[
                """
                UPDATE students_exam
                SET sub_section = 'UPPER'
                WHERE school_section = 'PRIMARY' AND sub_section IS NULL;
                """,
            ],
            reverse_sql=[
                "UPDATE students_exam SET sub_section = NULL WHERE school_section = 'PRIMARY';",
            ],
        ),

        # ── Add composite indexes for performance ──────────────────
        migrations.RunSQL(
            sql=[
                "CREATE INDEX IF NOT EXISTS idx_student_section_sub ON students_student (school_id, school_section, sub_section);",
                "CREATE INDEX IF NOT EXISTS idx_mark_section_sub ON students_mark (school_id, school_section, sub_section);",
                "CREATE INDEX IF NOT EXISTS idx_grade_section_sub ON students_grade (school_id, school_section, sub_section);",
            ],
            reverse_sql=[
                "DROP INDEX IF EXISTS idx_student_section_sub;",
                "DROP INDEX IF EXISTS idx_mark_section_sub;",
                "DROP INDEX IF EXISTS idx_grade_section_sub;",
            ],
        ),
    ]
