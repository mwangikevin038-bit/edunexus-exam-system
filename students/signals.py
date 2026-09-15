"""
Cache invalidation signals for the students app.

Deletes class list cache keys when students are added, modified, or removed.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .views.students_mgmt import _class_list_cache_key


def _invalidate_class_list_cache(instance):
    """Delete all class list cache keys that could include this student."""
    from django.core.cache import cache

    school_id = instance.school_id
    grade = instance.class_name
    stream = instance.stream

    if school_id and grade:
        cache.delete(_class_list_cache_key(school_id, grade, stream))
        cache.delete(_class_list_cache_key(school_id, grade, 'Combined'))


@receiver(post_save, sender='students.Student')
def student_post_save_cache_invalidation(sender, instance, **kwargs):
    _invalidate_class_list_cache(instance)


@receiver(post_delete, sender='students.Student')
def student_post_delete_cache_invalidation(sender, instance, **kwargs):
    _invalidate_class_list_cache(instance)
