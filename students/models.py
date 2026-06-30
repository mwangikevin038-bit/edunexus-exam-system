import datetime
import logging

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.conf import settings
from django.db import models

from .school_scope import SchoolScopedManager, get_current_school
from .security.integrity import (
    compute_exam_checksum,
    compute_mark_checksum,
    verify_exam_checksum,
    verify_mark_checksum,
)

logger = logging.getLogger("students.models")


class School(models.Model):
    name = models.CharField(max_length=100, verbose_name="School Name")
    code = models.SlugField(
        max_length=50,
        unique=True,
        db_index=True,
        help_text="Used in logins, e.g. 0712345678@baringohigh",
    )
    logo = models.ImageField(upload_to='school_logos/', blank=True, null=True, verbose_name="Official School Logo")
    
    # Contact Information
    address = models.CharField(max_length=255, blank=True, null=True, verbose_name="Postal Address", help_text="e.g., P.O. BOX 31-80402")
    phone_number = models.CharField(max_length=20, blank=True, null=True, verbose_name="Official Phone")
    email = models.EmailField(blank=True, null=True, verbose_name="Billing/Contact Email")
    
    # Subscription Management (Zeraki Style)
    SUBSCRIPTION_TIERS = [
        ('Basic', 'Basic Exam Entry'),
        ('Premium', 'Premium + SMS Reports'),
        ('Enterprise', 'Full System Suite'),
    ]
    tier = models.CharField(max_length=20, choices=SUBSCRIPTION_TIERS, default='Basic', verbose_name="Subscription Tier")
    
    STATUS_CHOICES = [
        ('active', 'Active Subscribed'),
        ('trial', 'Free Trial Period'),
        ('suspended', 'Account Suspended (Unpaid)'),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='trial', verbose_name="Account Status")
    
    paid_until = models.DateField(blank=True, null=True, verbose_name="Subscription Valid Until")
    on_trial = models.BooleanField(default=True, verbose_name="Is on Trial")
    created_on = models.DateField(auto_now_add=True, verbose_name="Registration Date")

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Subscribed School"
        verbose_name_plural = "Subscribed Schools"


class SchoolScopedModel(models.Model):
    school = models.ForeignKey(
        School,
        on_delete=models.PROTECT,
        related_name="%(class)s_records",
        null=True,
        blank=True,
    )

    objects = SchoolScopedManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.school_id is None:
            school = get_current_school()
            if school is not None:
                self.school = school
        super().save(*args, **kwargs)

class SchoolAdmin(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='school_admin_profile',
    )
    school = models.OneToOneField(
        School,
        on_delete=models.CASCADE,
        related_name='admin_profile',
    )
    is_active = models.BooleanField(default=True)
    must_change_password = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Admin for {self.school.name}"

    class Meta:
        verbose_name = "School Admin"
        verbose_name_plural = "School Admins"

# -------------------- Grade Model --------------------
class Grade(SchoolScopedModel):
    SECTION_CHOICES = [
        ('PRIMARY', 'Primary'),
        ('JSS', 'Junior Secondary'),
    ]
    GRADE_CHOICES = [(f'Grade {i}', f'Grade {i}') for i in range(1, 13)]
    
    name = models.CharField(max_length=20, choices=GRADE_CHOICES)
    school_section = models.CharField(
        max_length=10,
        choices=SECTION_CHOICES,
        default='JSS',
        help_text="Which section this grade belongs to"
    )
    sub_section = models.CharField(
        max_length=10,
        choices=[('LOWER', 'Lower Primary'), ('UPPER', 'Upper Primary')],
        help_text='Lower Primary (1-3) or Upper Primary (4-6). NULL for JSS.',
        null=True,
        blank=True,
    )
    order = models.PositiveIntegerField()

    class Meta:
        unique_together = ('school', 'name')
        ordering = ['order']

    def __str__(self):
        return self.name


# -------------------- Stream Model --------------------
class Stream(SchoolScopedModel):
    SECTION_CHOICES = [
        ('PRIMARY', 'Primary'),
        ('JSS', 'Junior Secondary'),
    ]
    grade = models.ForeignKey(
        Grade,
        on_delete=models.CASCADE,
        related_name='streams',
    )
    name = models.CharField(max_length=50)
    school_section = models.CharField(
        max_length=10,
        choices=SECTION_CHOICES,
        default='JSS',
        help_text="Which section this stream belongs to"
    )

    class Meta:
        unique_together = ('school', 'grade', 'name')
        ordering = ['name']

    def __str__(self):
        return f"{self.grade.name} - {self.name}"


# -------------------- Subject Model (Dictionary Table) --------------------
class Subject(SchoolScopedModel):
    SECTION_CHOICES = [
        ('LOWER_PRIMARY', 'Lower Primary'),
        ('PRIMARY', 'Primary'),
        ('JSS', 'Junior Secondary'),
    ]
    GRADE_CHOICES = [(f'Grade {i}', f'Grade {i}') for i in range(1, 10)]

    code = models.CharField(max_length=10, help_text="Unique subject code, e.g. ENG, MAT")
    name = models.CharField(max_length=100, help_text="Official subject / learning area name")
    school_section = models.CharField(
        max_length=15,
        choices=SECTION_CHOICES,
        help_text="Which section this subject belongs to"
    )
    sub_section = models.CharField(
        max_length=10,
        choices=[('LOWER', 'Lower Primary'), ('UPPER', 'Upper Primary')],
        help_text='Lower Primary (1-3) or Upper Primary (4-6). NULL for JSS.',
        null=True,
        blank=True,
    )
    grade = models.CharField(
        max_length=20,
        choices=GRADE_CHOICES,
        help_text="Grade level this subject is offered at"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ('school', 'code', 'school_section', 'grade')
        ordering = ['school_section', 'grade', 'code']
        verbose_name = "Subject"
        verbose_name_plural = "Subjects"

    def __str__(self):
        return f"{self.code} - {self.name} ({self.grade})"


# Helper function to get the current year dynamically
def current_year():
    return datetime.date.today().year


# -------------------- Guardian Model --------------------
class Guardian(SchoolScopedModel):
    SECTION_CHOICES = [
        ('PRIMARY', 'Primary'),
        ('JSS', 'Junior Secondary'),
    ]
    user = models.OneToOneField(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="guardian_profile",
        help_text="Optional parent portal account (read-only access to linked learners).",
    )
    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=15, help_text="Unique phone number for SMS alerts")
    school_section = models.CharField(
        max_length=10,
        choices=SECTION_CHOICES,
        default='JSS',
        help_text="Which section this guardian belongs to"
    )

    def __str__(self):
        return f"{self.name} ({self.phone})"

    class Meta:
        unique_together = ('school', 'phone')


# -------------------- Student Model --------------------
class Student(SchoolScopedModel):
    SECTION_CHOICES = [
        ('PRIMARY', 'Primary'),
        ('JSS', 'Junior Secondary'),
    ]

    user = models.OneToOneField(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="student_profile",
        help_text="Optional student portal account (read-only access to own records).",
    )
    CLASS_CHOICES = [
        ('Grade 1', 'Grade 1'),
        ('Grade 2', 'Grade 2'),
        ('Grade 3', 'Grade 3'),
        ('Grade 4', 'Grade 4'),
        ('Grade 5', 'Grade 5'),
        ('Grade 6', 'Grade 6'),
        ('Grade 7', 'Grade 7'),
        ('Grade 8', 'Grade 8'),
        ('Grade 9', 'Grade 9'),
    ]
    STREAM_CHOICES = [
        ('Yellow', 'Yellow'),
        ('Blue', 'Blue'),
        ('Main', 'Main'),
    ]
    TERM_CHOICES = [
        ('Term 1', 'Term 1'),
        ('Term 2', 'Term 2'),
        ('Term 3', 'Term 3'),
    ]

    admission_no = models.CharField(max_length=20)
    name = models.CharField(max_length=100)
    school_section = models.CharField(
        max_length=10,
        choices=SECTION_CHOICES,
        default='JSS',
        help_text="Which section this student belongs to"
    )
    sub_section = models.CharField(
        max_length=10,
        choices=[('LOWER', 'Lower Primary'), ('UPPER', 'Upper Primary')],
        help_text='Lower Primary (1-3) or Upper Primary (4-6). NULL for JSS.',
        null=True,
        blank=True,
    )
    class_name = models.CharField(max_length=20, choices=CLASS_CHOICES)
    stream = models.CharField(max_length=20)
    term = models.CharField(max_length=20, choices=TERM_CHOICES, default='Term 1')
    assessment_no = models.CharField(max_length=50, blank=True, null=True)
    guardian = models.ForeignKey(Guardian, on_delete=models.PROTECT, related_name='students')
    year = models.IntegerField(default=current_year)

    RELIGION_CHOICES = [
        ('CRE', 'CRE'),
        ('IRE', 'IRE'),
        ('None', 'None'),
    ]
    religion = models.CharField(
        max_length=10,
        choices=RELIGION_CHOICES,
        default='None',
        blank=True,
        null=True,
        verbose_name="Religious Education"
    )

    GENDER_CHOICES = [
        ('Male', 'Male'),
        ('Female', 'Female'),
        ('Not Specified', 'Not Specified'),
    ]
    gender = models.CharField(
        max_length=15,
        choices=GENDER_CHOICES,
        default='Not Specified',
        blank=True,
        null=True,
        verbose_name="Gender"
    )

    def __str__(self):
        return f"{self.name} ({self.admission_no})"

    class Meta:
        unique_together = ('school', 'admission_no')

# -------------------- Mark Model --------------------
class Mark(SchoolScopedModel):

    SECTION_CHOICES = [
        ('PRIMARY', 'Primary'),
        ('JSS', 'Junior Secondary'),
    ]

    KJSEA_SUBJECTS = [
        ('901', 'English Language'),
        ('902', 'Kiswahili'),
        ('903', 'Mathematics'),
        ('904', 'Kenyan Sign Language'),
        ('905', 'Integrated Science'),
        ('906', 'Agriculture and Nutrition'),
        ('907', 'Social Studies'),
        ('908', 'CRE'),
        ('909', 'IRE'),
        ('910', 'HRE'),
        ('911', 'Creative Arts and Sports'),
        ('912', 'Pre-Technical Studies'),
    ]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='marks')
    subject = models.ForeignKey(
        'Subject',
        on_delete=models.PROTECT,
        related_name='marks',
        null=True,
        blank=True,
        help_text="Subject for this mark"
    )
    school_section = models.CharField(
        max_length=10,
        choices=SECTION_CHOICES,
        default='JSS',
        help_text="Which section this mark belongs to"
    )
    sub_section = models.CharField(
        max_length=10,
        choices=[('LOWER', 'Lower Primary'), ('UPPER', 'Upper Primary')],
        help_text='Lower Primary (1-3) or Upper Primary (4-6). NULL for JSS.',
        null=True,
        blank=True,
    )

    score = models.PositiveIntegerField()
    raw_score = models.PositiveIntegerField(null=True, blank=True)
    maximum_marks = models.PositiveIntegerField(default=100)
    is_absent = models.BooleanField(default=False)

    # ── Primary Assessment Fields ──────────────────────────────────────
    PRIMARY_DESCRIPTOR_CHOICES = [
        ('EE', 'Exceeding Expectations'),
        ('ME', 'Meeting Expectations'),
        ('AE', 'Approaching Expectations'),
        ('BE', 'Below Expectations'),
        ('AB', 'Absent'),
    ]

    primary_raw_score = models.CharField(
        max_length=10,
        blank=True,
        default='',
        help_text="Primary raw score: whole number (e.g. 85) or 'AB' if absent",
    )
    primary_performance_point = models.CharField(
        max_length=10,
        blank=True,
        default='',
        help_text="Primary rank scale: 1, 2, 3, 4 or 'AB' if absent",
    )
    primary_descriptor = models.CharField(
        max_length=2,
        choices=PRIMARY_DESCRIPTOR_CHOICES,
        blank=True,
        default='',
        help_text="CBC competency descriptor: EE, ME, AE, BE, or AB",
    )

    term = models.CharField(max_length=20, choices=Student.TERM_CHOICES)
    year = models.IntegerField(default=current_year)
    exam_type = models.CharField(max_length=50, null=True, blank=True)

    performance_level = models.CharField(max_length=50, editable=False)
    points = models.IntegerField(editable=False)

    date_recorded = models.DateTimeField(auto_now_add=True)
    integrity_checksum = models.CharField(max_length=64, editable=False, blank=True, default="")

    def clean(self):
        """Validate primary assessment fields and subject-grade consistency."""
        super().clean()
        if self.school_section == 'PRIMARY':
            raw = (self.primary_raw_score or '').strip()
            if raw and raw.upper() != 'AB':
                if not raw.isdigit():
                    raise ValidationError({
                        'primary_raw_score': 'Must be a whole number or "AB".'
                    })
            point = (self.primary_performance_point or '').strip()
            if point and point.upper() != 'AB':
                if point not in ('1', '2', '3', '4'):
                    raise ValidationError({
                        'primary_performance_point': 'Must be 1, 2, 3, 4, or "AB".'
                    })
            desc = (self.primary_descriptor or '').strip().upper()
            if desc and desc not in ('EE', 'ME', 'AE', 'BE', 'AB'):
                raise ValidationError({
                    'primary_descriptor': 'Must be EE, ME, AE, BE, or AB.'
                })

        if self.subject_id and self.student_id:
            if self.subject.grade != self.student.class_name:
                raise ValidationError({
                    'subject': f"Subject {self.subject.code} is for {self.subject.grade}, but student is in {self.student.class_name}."
                })
            if self.subject.school_section != self.school_section:
                raise ValidationError({
                    'subject': f"Subject {self.subject.code} belongs to {self.subject.get_school_section_display()}, but mark is for {self.get_school_section_display()}."
                })

    def save(self, *args, **kwargs):
        if self.pk and self.integrity_checksum and not verify_mark_checksum(self):
            logger.critical(
                "Mark integrity violation detected: mark_id=%s student_id=%s school_id=%s",
                self.pk,
                self.student_id,
                self.school_id,
            )
            raise ValidationError("Mark record failed integrity verification.")

        if self.is_absent:
            self.score = 0
            self.raw_score = None
            self.performance_level = "AB"
            self.points = 0
            super().save(*args, **kwargs)
            return

        if self.raw_score is not None:
            max_marks = self.maximum_marks or 100
            if max_marks > 0:
                self.score = round((self.raw_score / max_marks) * 100)

        if self.score >= 90:
            self.performance_level, self.points = "EE1", 8
        elif self.score >= 75:
            self.performance_level, self.points = "EE2", 7
        elif self.score >= 58:
            self.performance_level, self.points = "ME1", 6
        elif self.score >= 41:
            self.performance_level, self.points = "ME2", 5
        elif self.score >= 31:
            self.performance_level, self.points = "AE1", 4
        elif self.score >= 21:
            self.performance_level, self.points = "AE2", 3
        elif self.score >= 11:
            self.performance_level, self.points = "BE1", 2
        else:
            self.performance_level, self.points = "BE2", 1

        self.integrity_checksum = compute_mark_checksum(self)
        super().save(*args, **kwargs)

    # ── Report Card Snapshot (frozen comments after 30 days) ────────────
    frozen_class_teacher_comment = models.TextField(blank=True, default="")
    frozen_headteacher_comment = models.TextField(blank=True, default="")
    frozen_closing_date = models.DateField(null=True, blank=True)
    frozen_opening_date = models.DateField(null=True, blank=True)

    def __str__(self):
        if self.is_absent:
            return f"{self.student.name} - {self.subject}: AB"
        return f"{self.student.name} - {self.subject}: {self.score}"
# ==================================================================================
# 🔄 UNIFIED TEACHER MODEL - Consolidates TeacherProfile + Teacher
# ==================================================================================

class Teacher(SchoolScopedModel):
    """
    🌟 UNIFIED TEACHER MODEL
    Combines both TeacherProfile (auth/login) and Teacher (report cards) into one source of truth.
    
    This eliminates the duplicate data issue and ensures all teacher info flows from a single place.
    """
    
    SECTION_CHOICES = [
        ('PRIMARY', 'Primary'),
        ('JSS', 'Junior Secondary'),
        ('BOTH', 'Both Sections'),
    ]

    # ============ AUTHENTICATION & LINKING ============
    user = models.OneToOneField(
        User, 
        on_delete=models.CASCADE, 
        related_name='teacher_profile',
        help_text="Link this teacher to their user account for login"
    )
    
    school_section = models.CharField(
        max_length=10,
        choices=SECTION_CHOICES,
        default='BOTH',
        help_text="Which section this teacher belongs to"
    )
    sub_section = models.CharField(
        max_length=10,
        choices=[('LOWER', 'Lower Primary'), ('UPPER', 'Upper Primary')],
        help_text='Lower Primary (1-3) or Upper Primary (4-6). NULL for JSS/Both.',
        null=True,
        blank=True,
    )

    # ============ PERSONAL INFORMATION ============
    TITLE_CHOICES = [
        ('Mr.', 'Mr.'),
        ('Ms.', 'Ms.'),
        ('Mrs.', 'Mrs.'),
        ('Dr.', 'Dr.'),
        ('Prof.', 'Prof.'),
    ]
    
    title = models.CharField(
        max_length=10,
        choices=TITLE_CHOICES,
        default='Mr.',
        help_text="Title (Mr., Ms., Mrs., Dr., Prof.)"
    )

    # TSC Number (Teacher Service Commission)
    tsc_number = models.CharField(
        max_length=20,
        verbose_name="TSC Number",
        help_text="Teacher Service Commission registration number"
    )
    
    phone_number = models.CharField(
        max_length=20, 
        verbose_name="Phone Number",
        help_text="Contact phone number"
    )
    
    email = models.EmailField(
        help_text="Teacher's email address"
    )
    
    # ============ TEACHING ASSIGNMENT ============
    TASK_CHOICES = [
        ('Class Teacher Grade 7 Yellow', 'Class Teacher Grade 7 Yellow'),
        ('Class Teacher Grade 7 Blue', 'Class Teacher Grade 7 Blue'),
        ('Class Teacher Grade 8 Main', 'Class Teacher Grade 8 Main'),
        ('Class Teacher Grade 9 Main', 'Class Teacher Grade 9 Main'),
        ('Teacher', 'Teacher'),
    ]
    
    assigned_task = models.CharField(
        max_length=100,
        choices=TASK_CHOICES,
        default='Teacher',
        help_text="Administrative role or class assignment"
    )
    
    subjects_taught = models.CharField(
        max_length=200, 
        verbose_name="Subjects Taught",
        help_text="Subjects taught (e.g., Mathematics, Physics). Separate with commas for multiple subjects."
    )
    
    classes = models.CharField(
        max_length=255,
        help_text="Classes taught (e.g., Grade 7 Yellow, Grade 8 Blue, Grade 9 Main)"
    )
    
    # ============ STATUS & METADATA ============
    is_active = models.BooleanField(
        default=True, 
        help_text="Whether the teacher is currently active"
    )
    
    must_change_password = models.BooleanField(
        default=True,
        help_text="Force teacher to change password on first login"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = "Teacher"
        verbose_name_plural = "Teachers"
        ordering = ['user__first_name', 'user__last_name']
        indexes = [
            models.Index(fields=['school', 'tsc_number']),
            models.Index(fields=['school', 'email']),
            models.Index(fields=['school', 'subjects_taught']),
            models.Index(fields=['school', 'is_active']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['user'],
                name='unique_user_teacher',
                condition=models.Q(user__isnull=False)
            ),
            models.UniqueConstraint(fields=['school', 'tsc_number'], name='unique_school_teacher_tsc'),
            models.UniqueConstraint(fields=['school', 'phone_number'], name='unique_school_teacher_phone'),
        ]
    
    # ============ HELPER METHODS ============
    def __str__(self):
        return f"{self.get_full_title()} - {self.subjects_taught}"

    def get_full_title(self):
        """Returns title + full name properly formatted and title-cased using single field"""
        full_name = self.user.first_name.strip()
        if not full_name:
            full_name = self.user.username # Fallback to system username if empty
        return f"{self.title} {full_name}".title()

    def get_display_name(self):
        """Returns full name with subject"""
        full_name = self.user.first_name.strip()
        if not full_name:
            full_name = self.tsc_number
        return f"{self.title} {full_name} ({self.subjects_taught})"

    @property
    def full_name(self):
        """Returns user's full unified name string"""
        return self.user.first_name.strip()


# -------------------- Subject Assignment Model --------------------
class SubjectAssignment(SchoolScopedModel):
    SECTION_CHOICES = [
        ('PRIMARY', 'Primary'),
        ('JSS', 'Junior Secondary'),
    ]
    SUB_SECTION_CHOICES = [
        ('LOWER', 'Lower Primary'),
        ('UPPER', 'Upper Primary'),
    ]
    teacher_profile = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='assignments')
    subject = models.ForeignKey(
        'Subject',
        on_delete=models.PROTECT,
        related_name='assignments',
        null=True,
        blank=True,
        help_text="Subject assigned to this teacher"
    )
    class_name = models.CharField(max_length=20, choices=Student.CLASS_CHOICES)
    stream = models.CharField(max_length=20)
    school_section = models.CharField(
        max_length=10,
        choices=SECTION_CHOICES,
        default='JSS',
        help_text="Which section this assignment belongs to"
    )
    sub_section = models.CharField(
        max_length=10,
        choices=SUB_SECTION_CHOICES,
        null=True,
        blank=True,
        help_text="Lower Primary (1-3) or Upper Primary (4-6). NULL for JSS."
    )

    class Meta:
        unique_together = ('school', 'subject', 'class_name', 'stream')

    def __str__(self):
        return f"{self.teacher_profile.user.get_full_name() or self.teacher_profile.user.username} -> {self.subject.name} ({self.class_name} {self.stream})"

    def clean(self):
        super().clean()
        if self.subject_id:
            if self.subject.grade != self.class_name:
                raise ValidationError({
                    'subject': f"Subject {self.subject.code} is for {self.subject.grade}, but assignment is for {self.class_name}."
                })
            if self.subject.school_section != self.school_section:
                raise ValidationError({
                    'subject': f"Subject {self.subject.code} belongs to {self.subject.get_school_section_display()}, but assignment is for {self.get_school_section_display()}."
                })

#------------------------------MARK SUBMISSION-------------------------------------#
class MarkSubmission(SchoolScopedModel):
    SECTION_CHOICES = [
        ('PRIMARY', 'Primary'),
        ('JSS', 'Junior Secondary'),
    ]
    STATUS_CHOICES = [
        ('submitted', 'Submitted'),
        ('returned', 'Returned to Teacher'),
        ('approved', 'Approved'),
        ('published', 'Published'),
    ]

    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE)
    subject = models.ForeignKey(
        'Subject',
        on_delete=models.PROTECT,
        related_name='submissions',
        null=True,
        blank=True,
        help_text="Subject for this submission"
    )
    school_section = models.CharField(
        max_length=10,
        choices=SECTION_CHOICES,
        default='JSS',
        help_text="Which section this submission belongs to"
    )
    sub_section = models.CharField(
        max_length=10,
        choices=[('LOWER', 'Lower Primary'), ('UPPER', 'Upper Primary')],
        help_text='Lower Primary (1-3) or Upper Primary (4-6). NULL for JSS.',
        null=True,
        blank=True,
    )
    class_name = models.CharField(max_length=50)
    stream = models.CharField(max_length=50)
    exam_name = models.CharField(max_length=100)
    term = models.CharField(max_length=20)
    year = models.PositiveIntegerField()

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='submitted')
    admin_note = models.TextField(blank=True, default='')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)

    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = (
            'school',
            'teacher',
            'subject',
            'class_name',
            'stream',
            'exam_name',
            'term',
            'year',
            'school_section',
        )

    def __str__(self):
        return (
            f"{self.teacher} - {self.subject.code} - "
            f"{self.class_name} {self.stream} - {self.exam_name} {self.term} {self.year}"
        )
# -------------------- Class Teacher Master Comments Model --------------------
class ClassTeacherMasterComment(SchoolScopedModel):
    SECTION_CHOICES = [
        ('PRIMARY', 'Primary'),
        ('JSS', 'Junior Secondary'),
    ]
    year = models.CharField(max_length=4)
    term = models.CharField(max_length=20)
    grade = models.CharField(max_length=20)
    stream = models.CharField(max_length=20)
    exam_type = models.CharField(max_length=50)
    school_section = models.CharField(
        max_length=10,
        choices=SECTION_CHOICES,
        default='JSS',
        help_text="Which section this comment applies to"
    )

    # 8 comment boxes for the 8 JSS performance bands
    comment_ee1 = models.TextField(blank=True, default="")
    comment_ee2 = models.TextField(blank=True, default="")
    comment_me1 = models.TextField(blank=True, default="")
    comment_me2 = models.TextField(blank=True, default="")
    comment_ae1 = models.TextField(blank=True, default="")
    comment_ae2 = models.TextField(blank=True, default="")
    comment_be1 = models.TextField(blank=True, default="")
    comment_be2 = models.TextField(blank=True, default="")
    closing_date = models.DateField(null=True, blank=True)
    opening_date = models.DateField(null=True, blank=True)

    # Primary-specific class teacher comment boxes (4 levels)
    comment_ee = models.TextField(blank=True, default="")
    comment_me = models.TextField(blank=True, default="")
    comment_ae = models.TextField(blank=True, default="")
    comment_be = models.TextField(blank=True, default="")

    last_modified = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('school', 'year', 'term', 'grade', 'stream', 'exam_type')

    def __str__(self):
        return f"{self.grade} {self.stream} - Comments ({self.exam_type})"


class SchoolHeadteacherComment(SchoolScopedModel):
    SECTION_CHOICES = [
        ('PRIMARY', 'Primary'),
        ('JSS', 'Junior Secondary'),
    ]
    year = models.CharField(max_length=4)
    term = models.CharField(max_length=20)
    exam_type = models.CharField(max_length=50)
    school_section = models.CharField(
        max_length=10,
        choices=SECTION_CHOICES,
        default='JSS',
        help_text="Which section this comment applies to"
    )

    ht_comment_ee1 = models.TextField(blank=True, default="")
    ht_comment_ee2 = models.TextField(blank=True, default="")
    ht_comment_me1 = models.TextField(blank=True, default="")
    ht_comment_me2 = models.TextField(blank=True, default="")
    ht_comment_ae1 = models.TextField(blank=True, default="")
    ht_comment_ae2 = models.TextField(blank=True, default="")
    ht_comment_be1 = models.TextField(blank=True, default="")
    ht_comment_be2 = models.TextField(blank=True, default="")

    # Primary-specific (4 levels: EE, ME, AE, BE)
    ht_comment_ee = models.TextField(blank=True, default="")
    ht_comment_me = models.TextField(blank=True, default="")
    ht_comment_ae = models.TextField(blank=True, default="")
    ht_comment_be = models.TextField(blank=True, default="")

    last_modified = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('school', 'year', 'term', 'exam_type', 'school_section')

    def __str__(self):
        return f"Headteacher Remarks - {self.term} {self.year} ({self.exam_type})"

#----------------------------------------EXAM MODEL--------------------------------------------------------------------#    
class Exam(SchoolScopedModel):
    SECTION_CHOICES = [
        ('PRIMARY', 'Primary'),
        ('JSS', 'Junior Secondary'),
    ]
    EXAM_CHOICES = [
        ('Opener Assessment', 'Opener Assessment'),
        ('Mid Term Assessment', 'Mid Term Assessment'),
        ('End Term Assessment', 'End Term Assessment'),
    ]

    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('closed', 'Closed'),
    ]

    name = models.CharField(max_length=50)
    school_section = models.CharField(
        max_length=10,
        choices=SECTION_CHOICES,
        default='JSS',
        help_text="Which section this exam belongs to"
    )
    sub_section = models.CharField(
        max_length=10,
        choices=[('LOWER', 'Lower Primary'), ('UPPER', 'Upper Primary')],
        help_text='Lower Primary (1-3) or Upper Primary (4-6). NULL for JSS.',
        null=True,
        blank=True,
    )
    exam_report_mode = models.CharField(
        max_length=20,
        choices=[
            ('UNMERGED', 'Unmerged (Individual Subjects)'),
            ('INTEGRATED_KNEC', 'Integrated KNEC (Merged Papers)'),
        ],
        default='UNMERGED',
    )
    term = models.CharField(max_length=20, choices=Student.TERM_CHOICES)
    year = models.IntegerField(default=current_year)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    created_at = models.DateTimeField(auto_now_add=True)
    integrity_checksum = models.CharField(max_length=64, editable=False, blank=True, default="")

    class Meta:
        unique_together = ('school', 'name', 'term', 'year', 'school_section')
        ordering = ['-year', 'term', 'name']

    def save(self, *args, **kwargs):
        if self.pk and self.integrity_checksum and not verify_exam_checksum(self):
            logger.critical(
                "Exam integrity violation detected: exam_id=%s school_id=%s",
                self.pk,
                self.school_id,
            )
            raise ValidationError("Exam record failed integrity verification.")
        self.integrity_checksum = compute_exam_checksum(self)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} - {self.term} {self.year}"
        
# -------------------- Assessment Lock Model --------------------
class AssessmentLock(SchoolScopedModel):
    SECTION_CHOICES = [
        ('PRIMARY', 'Primary'),
        ('JSS', 'Junior Secondary'),
    ]
    EXAM_CHOICES = [
        ('Opener Assessment', 'Opener Assessment'),
        ('Mid Term Assessment', 'Mid Term Assessment'),
        ('End Term Assessment', 'End Term Assessment'),
    ]
    
    year = models.IntegerField(default=2026)
    term = models.CharField(max_length=20, choices=[('Term 1', 'Term 1'), ('Term 2', 'Term 2'), ('Term 3', 'Term 3')])
    exam_type = models.CharField(max_length=50)
    grade = models.CharField(max_length=20, choices=[('Grade 1', 'Grade 1'), ('Grade 2', 'Grade 2'), ('Grade 3', 'Grade 3'), ('Grade 4', 'Grade 4'), ('Grade 5', 'Grade 5'), ('Grade 6', 'Grade 6'), ('Grade 7', 'Grade 7'), ('Grade 8', 'Grade 8'), ('Grade 9', 'Grade 9')])
    school_section = models.CharField(
        max_length=10,
        choices=SECTION_CHOICES,
        default='JSS',
        help_text="Which section this lock applies to"
    )
    sub_section = models.CharField(
        max_length=10,
        choices=[('LOWER', 'Lower Primary'), ('UPPER', 'Upper Primary')],
        help_text='Lower Primary (1-3) or Upper Primary (4-6). NULL for JSS.',
        null=True,
        blank=True,
    )
    is_locked = models.BooleanField(default=False, help_text="If checked, teachers cannot alter marks for this setting")

    class Meta:
        unique_together = ('school', 'year', 'term', 'exam_type', 'grade', 'school_section')

    def __str__(self):
        return f"{self.grade} - {self.exam_type} ({self.term} {self.year}) -> {'LOCKED' if self.is_locked else 'OPEN'}"

# ==============================================================================
# 📢 GLOBAL PLATFORM COMMUNICATIONS (EDUNEXUS SYSTEM CONTROLS)
# ==============================================================================
class SystemBroadcast(models.Model):
    """
    Platform-wide announcements created by the EduNexus Superuser.
    These flow dynamically to the frontend dashboards of subscribing schools.
    """
    AUDIENCE_CHOICES = [
        ('all', 'All Users (Everyone)'),
        ('admins', 'School Admins Only'),
        ('teachers', 'Teachers Only'),
    ]
    
    title = models.CharField(max_length=150, verbose_name="Broadcast Headline")
    message = models.TextField(verbose_name="Notification Details")
    target_audience = models.CharField(max_length=15, choices=AUDIENCE_CHOICES, default='all')
    is_active = models.BooleanField(default=True, help_text="Uncheck this to instantly pull the alert off all school screens.")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} - Target: {self.get_target_audience_display()}"

    class Meta:
        verbose_name = "System Broadcast"
        verbose_name_plural = "System Broadcasts"


class SecurityAuditLog(models.Model):
    """Immutable security audit trail for sensitive mutations."""

    ACTION_CHOICES = [
        ("create", "Create"),
        ("update", "Update"),
        ("delete", "Delete"),
    ]

    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="security_audit_actions",
    )
    actor_id_snapshot = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    action = models.CharField(max_length=10, choices=ACTION_CHOICES, db_index=True)
    target_model = models.CharField(max_length=120, db_index=True)
    target_id = models.CharField(max_length=64, db_index=True)
    target_fields = models.JSONField(default=list, blank=True)
    old_values = models.JSONField(default=dict, blank=True)
    new_values = models.JSONField(default=dict, blank=True)
    school_id_snapshot = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    record_hash = models.CharField(max_length=64, editable=False, db_index=True)

    class Meta:
        ordering = ["-timestamp"]
        verbose_name = "Security Audit Log"
        verbose_name_plural = "Security Audit Logs"
        indexes = [
            models.Index(fields=["target_model", "target_id"]),
            models.Index(fields=["school_id_snapshot", "timestamp"]),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Security audit logs are immutable.")
        if not self.timestamp:
            from django.utils import timezone
            self.timestamp = timezone.now()
        if not self.record_hash:
            self.record_hash = self._compute_record_hash()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Security audit logs cannot be deleted.")

    def _compute_record_hash(self):
        import json

        from .security.integrity import compute_audit_record_hash

        payload = json.dumps(
            {
                "timestamp": self.timestamp.isoformat() if self.timestamp else "",
                "actor_id_snapshot": self.actor_id_snapshot,
                "client_ip": self.client_ip,
                "action": self.action,
                "target_model": self.target_model,
                "target_id": self.target_id,
                "target_fields": self.target_fields,
                "old_values": self.old_values,
                "new_values": self.new_values,
                "school_id_snapshot": self.school_id_snapshot,
            },
            sort_keys=True,
            default=str,
        )
        return compute_audit_record_hash(payload)


# -------------------- Class Teacher Assignment Model --------------------
class ClassTeacherAssignment(models.Model):
    school = models.ForeignKey(
        School,
        on_delete=models.PROTECT,
        related_name="%(class)s_records",
        null=True,
        blank=True,
    )
    teacher = models.ForeignKey(
        Teacher,
        on_delete=models.CASCADE,
        related_name='class_teacher_assignments',
    )
    stream = models.ForeignKey(
        'Stream',
        on_delete=models.CASCADE,
        related_name='class_teacher_assignments',
    )
    school_section = models.CharField(
        max_length=10,
        choices=[('PRIMARY', 'Primary'), ('JSS', 'Junior Secondary')],
        default='PRIMARY',
        help_text='Which section this assignment belongs to',
    )
    year = models.IntegerField(default=current_year)
    term = models.CharField(max_length=20)

    class Meta:
        ordering = ['-year', 'term']
        unique_together = ('school', 'stream', 'year', 'term')

    def __str__(self):
        return f"{self.teacher} - {self.stream} ({self.year} {self.term})"


# -------------------- Subject Merge Group Model --------------------
class SubjectMergeGroup(models.Model):
    sub_section = models.CharField(
        max_length=10,
        choices=[('LOWER', 'Lower Primary'), ('UPPER', 'Upper Primary')],
    )
    merge_group_code = models.CharField(max_length=20)
    merge_group_name = models.CharField(max_length=100)
    component_code = models.CharField(max_length=10)
    component_name = models.CharField(max_length=100)
    display_order = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ['sub_section', 'merge_group_code', 'display_order']
        unique_together = ('sub_section', 'merge_group_code', 'component_code')

    def __str__(self):
        return f"{self.merge_group_name} ({self.component_code})"


# -------------------- Notification Model --------------------
class Notification(models.Model):
    NOTIFICATION_TYPE_CHOICES = [
        ('exam', 'Exam'),
        ('sms', 'SMS'),
        ('system', 'System'),
        ('alert', 'Alert'),
    ]
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
    )
    title = models.CharField(max_length=200)
    message = models.TextField()
    notification_type = models.CharField(
        max_length=10,
        choices=NOTIFICATION_TYPE_CHOICES,
        default='system',
    )
    is_read = models.BooleanField(default=False)
    link = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} ({self.notification_type})"
