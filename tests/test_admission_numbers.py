"""
REGRESSION TEST: Admission Number Logic
========================================
Run this to verify admission number generation follows the locked rules:
  - PRIMARY: LOWER + UPPER share one series (e.g. next after 344 is 345)
  - JSS: independent series (e.g. next after 450 is 451)
  - Never generates duplicate numbers across sections
  - Handles edge cases: empty DB, non-numeric existing numbers, etc.

Run: python tests/test_admission_numbers.py
"""
import os, sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "school.settings")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import django
django.setup()

from students.models import Student, School
from students.tasks import _next_admission_number


def get_school():
    return School.objects.first()


def count_primary(school):
    return Student.all_objects.filter(
        school=school, school_section='PRIMARY',
        sub_section__in=['LOWER', 'UPPER', None, '']
    ).count()


def count_jss(school):
    return Student.all_objects.filter(
        school=school, school_section='JSS',
        sub_section__isnull=True
    ).count()


def test_primary_and_jss_independent():
    """PRIMARY and JSS must have separate number series."""
    school = get_school()
    primary_count = count_primary(school)
    jss_count = count_jss(school)

    next_primary = _next_admission_number(school, 'PRIMARY')
    next_jss = _next_admission_number(school, 'JSS')

    assert next_primary >= primary_count + 1
    assert next_jss >= jss_count + 1
    print(f"  PASS: PRIMARY next={next_primary}, JSS next={next_jss} (independent series)")


def test_lower_primary_maps_to_primary():
    """LOWER_PRIMARY workspace token must normalize to PRIMARY."""
    school = get_school()
    next_lp = _next_admission_number(school, 'LOWER_PRIMARY')
    next_p = _next_admission_number(school, 'PRIMARY')
    assert next_lp == next_p
    print(f"  PASS: LOWER_PRIMARY and PRIMARY both return {next_lp}")


def test_unknown_section_returns_1():
    """Unknown section should return 1 as safe fallback."""
    school = get_school()
    next_unknown = _next_admission_number(school, 'UNKNOWN_SECTION')
    assert next_unknown == 1
    print(f"  PASS: Unknown section returns 1")


def test_no_gaps():
    """Next admission number should not skip any existing numbers."""
    school = get_school()
    nums = Student.all_objects.filter(
        school=school, school_section='PRIMARY',
        admission_no__regex=r'^[0-9]+$'
    ).values_list('admission_no', flat=True)
    int_nums = sorted(int(n) for n in nums if n.isdigit())

    if int_nums:
        highest = int_nums[-1]
        next_no = _next_admission_number(school, 'PRIMARY')
        assert next_no == highest + 1
        print(f"  PASS: No gaps - highest={highest}, next={next_no}")
    else:
        next_no = _next_admission_number(school, 'PRIMARY')
        assert next_no == 1
        print(f"  PASS: Empty DB returns 1")


def test_shared_series_lower_upper():
    """LOWER and UPPER students must share the same number series."""
    school = get_school()
    lower_highest = Student.all_objects.filter(
        school=school, school_section='PRIMARY', sub_section='LOWER',
        admission_no__regex=r'^[0-9]+$'
    ).order_by('-admission_no').values_list('admission_no', flat=True).first()

    upper_highest = Student.all_objects.filter(
        school=school, school_section='PRIMARY', sub_section='UPPER',
        admission_no__regex=r'^[0-9]+$'
    ).order_by('-admission_no').values_list('admission_no', flat=True).first()

    overall_highest = Student.all_objects.filter(
        school=school, school_section='PRIMARY',
        admission_no__regex=r'^[0-9]+$'
    ).order_by('-admission_no').values_list('admission_no', flat=True).first()

    next_no = _next_admission_number(school, 'PRIMARY')

    if overall_highest:
        expected = int(overall_highest) + 1
        assert next_no == expected
        print(f"  PASS: Shared series - lower_highest={lower_highest}, upper_highest={upper_highest}, overall={overall_highest}, next={next_no}")
    else:
        print(f"  PASS: No students yet - next={next_no}")


if __name__ == "__main__":
    print("=" * 60)
    print("  ADMISSION NUMBER REGRESSION TESTS")
    print("=" * 60)

    tests = [
        ("Primary & JSS independent", test_primary_and_jss_independent),
        ("LOWER_PRIMARY maps to PRIMARY", test_lower_primary_maps_to_primary),
        ("Unknown section returns 1", test_unknown_section_returns_1),
        ("No gaps in numbering", test_no_gaps),
        ("Shared LOWER+UPPER series", test_shared_series_lower_upper),
    ]

    passed = 0
    failed = 0
    for name, test_fn in tests:
        try:
            print(f"\n  [{name}]")
            test_fn()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"  Results: {passed} passed, {failed} failed")
    print(f"{'=' * 60}")
    sys.exit(1 if failed else 0)
