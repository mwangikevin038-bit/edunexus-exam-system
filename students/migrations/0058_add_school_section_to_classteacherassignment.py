from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('students', '0057_add_rls_policies'),
    ]

    operations = [
        migrations.AddField(
            model_name='classteacherassignment',
            name='school_section',
            field=models.CharField(
                choices=[('PRIMARY', 'Primary'), ('JSS', 'Junior Secondary')],
                default='PRIMARY',
                help_text='Which section this assignment belongs to',
                max_length=10,
            ),
        ),
    ]
