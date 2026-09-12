from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('students', '0093_add_published_by_to_marksubmission'),
    ]

    operations = [
        migrations.AddField(
            model_name='exam',
            name='min_subjects',
            field=models.PositiveIntegerField(
                default=7,
                help_text='Minimum number of subjects a student must have published results for',
            ),
        ),
    ]
