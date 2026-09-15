"""
Pre-warm class list cache for all school/grade/stream combinations.

Usage:
    python manage.py backfill_class_list_cache
    python manage.py backfill_class_list_cache --school-id 5
    python manage.py backfill_class_list_cache --clear
"""

from django.core.cache import cache
from django.core.management.base import BaseCommand

from students.models import Grade, School, Stream, Student
from students.views.students_mgmt import _CLASS_LIST_CACHE_TTL, _class_list_cache_key


class Command(BaseCommand):
    help = 'Pre-warm class list Redis cache for all grade/stream combinations'

    def add_arguments(self, parser):
        parser.add_argument('--school-id', type=int, help='Only warm cache for this school')
        parser.add_argument('--clear', action='store_true', help='Clear all class list cache keys instead of warming')

    def handle(self, *args, **options):
        school_id = options.get('school_id')
        clear = options['clear']

        schools = School.objects.all()
        if school_id:
            schools = schools.filter(pk=school_id)

        if clear:
            cleared = 0
            for school in schools:
                for grade in Grade.all_objects.filter(school=school):
                    for stream in Stream.all_objects.filter(school=school, grade=grade):
                        key = _class_list_cache_key(school.pk, grade.name, stream.name)
                        cache.delete(key)
                        cleared += 1
                        combined_key = _class_list_cache_key(school.pk, grade.name, 'Combined')
                        cache.delete(combined_key)
            self.stdout.write(self.style.SUCCESS(f'Cleared {cleared} cache keys'))
            return

        warmed = 0
        students_total = 0

        for school in schools:
            for grade in Grade.all_objects.filter(school=school):
                streams = list(Stream.all_objects.filter(school=school, grade=grade).values_list('name', flat=True))
                for stream_name in streams:
                    key = _class_list_cache_key(school.pk, grade.name, stream_name)
                    students = list(
                        Student.all_objects.filter(
                            school=school, class_name=grade.name, stream=stream_name, is_active=True
                        ).values(
                            'id', 'admission_no', 'name', 'gender', 'stream',
                            'assessment_no', 'religion',
                        ).order_by('admission_no')
                    )
                    cache.set(key, students, _CLASS_LIST_CACHE_TTL)
                    warmed += 1
                    students_total += len(students)

        self.stdout.write(self.style.SUCCESS(
            f'Warmed {warmed} cache keys for {students_total} students across {schools.count()} schools'
        ))
