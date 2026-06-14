from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('students', '0027_marksubmission_admin_note_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='classteachermastercomment',
            name='closing_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='classteachermastercomment',
            name='opening_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='classteachermastercomment',
            name='headteacher_comment',
            field=models.TextField(blank=True, default='Good effort. I encourage the learner to maintain consistent progress into the next term.'),
        ),
    ]
