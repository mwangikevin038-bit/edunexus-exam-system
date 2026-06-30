"""
PostgreSQL Row-Level Security (RLS) for multi-tenant isolation.

Adds a second safety net: even if app-layer code forgets to filter by
school_id or school_section, PostgreSQL will strip unauthorized rows.

Session variables (set by TenantSecurityMiddleware on every request):
  app.current_school_id     — bigint (the tenant's PK)
  app.current_school_section — text ('PRIMARY', 'JSS', or 'BOTH')
  app.is_superuser           — boolean (true for platform admins)

Policy structure: single policy per table with OR logic:
  - superuser bypass, OR
  - tenant_has_access(school_id) AND tenant_has_section_access(school_section)
"""

from django.db import migrations

FUNCTIONS = """
CREATE OR REPLACE FUNCTION app.current_school_id()
RETURNS bigint LANGUAGE SQL STABLE SECURITY DEFINER
AS $$ SELECT NULLIF(current_setting('app.current_school_id', true), '')::bigint; $$;

CREATE OR REPLACE FUNCTION app.current_school_section()
RETURNS text LANGUAGE SQL STABLE SECURITY DEFINER
AS $$ SELECT NULLIF(current_setting('app.current_school_section', true), ''); $$;

CREATE OR REPLACE FUNCTION app.is_superuser()
RETURNS boolean LANGUAGE SQL STABLE SECURITY DEFINER
AS $$ SELECT COALESCE(NULLIF(current_setting('app.is_superuser', true), '')::boolean, false); $$;

CREATE OR REPLACE FUNCTION app.tenant_has_access(row_school_id bigint)
RETURNS boolean LANGUAGE SQL STABLE SECURITY DEFINER
AS $$
  SELECT app.is_superuser() OR row_school_id IS NULL OR row_school_id = app.current_school_id();
$$;

CREATE OR REPLACE FUNCTION app.tenant_has_section_access(row_section text)
RETURNS boolean LANGUAGE SQL STABLE SECURITY DEFINER
AS $$
  SELECT app.is_superuser()
    OR app.current_school_section() IS NULL
    OR app.current_school_section() = 'BOTH'
    OR row_section IS NULL
    OR row_section = app.current_school_section();
$$;
"""

TENANT_SECTION_TABLES = [
    "students_grade", "students_stream", "students_subject", "students_guardian",
    "students_student", "students_mark", "students_teacher",
    "students_subjectassignment", "students_marksubmission",
    "students_classteachermastercomment", "students_schoolheadteachercomment",
    "students_exam", "students_assessmentlock",
]

SCHOOL_ONLY_TABLES = ["students_classteacherassignment"]

OPEN_TABLES = [
    "students_school", "students_schooladmin", "students_subjectmergegroup",
    "students_securityauditlog", "students_systembroadcast",
]


def enable_rls(apps, schema_editor):
    schema_editor.execute("CREATE SCHEMA IF NOT EXISTS app")
    schema_editor.execute(FUNCTIONS)

    for table in TENANT_SECTION_TABLES:
        schema_editor.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        schema_editor.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        schema_editor.execute(f"DROP POLICY IF EXISTS p_{table}_access ON {table}")
        schema_editor.execute(f"""
            CREATE POLICY p_{table}_access ON {table} USING (
                app.is_superuser()
                OR (app.tenant_has_access(school_id)
                    AND app.tenant_has_section_access(school_section))
            )
        """)

    for table in SCHOOL_ONLY_TABLES:
        schema_editor.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        schema_editor.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        schema_editor.execute(f"DROP POLICY IF EXISTS p_{table}_access ON {table}")
        schema_editor.execute(f"""
            CREATE POLICY p_{table}_access ON {table} USING (
                app.is_superuser() OR app.tenant_has_access(school_id)
            )
        """)

    for table in OPEN_TABLES:
        schema_editor.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        schema_editor.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        schema_editor.execute(f"DROP POLICY IF EXISTS p_{table}_open ON {table}")
        schema_editor.execute(f"CREATE POLICY p_{table}_open ON {table} USING (true)")


class Migration(migrations.Migration):
    dependencies = [("students", "0056_add_exam_report_mode_and_merge_groups")]
    operations = [migrations.RunPython(enable_rls, migrations.RunPython.noop)]
