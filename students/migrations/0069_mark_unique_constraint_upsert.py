from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('students', '0068_exam_unique_sub_section'),
    ]

    operations = [
        # Intentionally empty — the upsert uses SELECT FOR UPDATE instead
        # of ON CONFLICT with an expression index.  This migration exists
        # as a placeholder for the 0069 slot.
    ]
