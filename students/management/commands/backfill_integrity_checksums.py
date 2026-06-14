from django.core.management.base import BaseCommand

from students.models import Exam, Mark
from students.security.integrity import compute_exam_checksum, compute_mark_checksum


class Command(BaseCommand):
    help = "Backfill HMAC integrity checksums for existing marks and exams."

    def handle(self, *args, **options):
        mark_count = 0
        for mark in Mark.all_objects.iterator():
            mark.integrity_checksum = compute_mark_checksum(mark)
            mark.save(update_fields=["integrity_checksum"])
            mark_count += 1

        exam_count = 0
        for exam in Exam.all_objects.iterator():
            exam.integrity_checksum = compute_exam_checksum(exam)
            exam.save(update_fields=["integrity_checksum"])
            exam_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Backfilled integrity checksums for {mark_count} marks and {exam_count} exams."
            )
        )
