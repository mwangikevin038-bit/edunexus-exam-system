"""
Forms for the students app.

Provides forms for student registration, mark entry, and password changes.
"""

import re

from django import forms
from django.contrib.auth.forms import PasswordChangeForm

from .models import Mark, Student, Subject
from .security.sanitization import SecureFormMixin

# Import canonical choices from models
GRADE_CHOICES = Student.CLASS_CHOICES
TERM_CHOICES = Student.TERM_CHOICES
RELIGION_CHOICES = Student.RELIGION_CHOICES
GENDER_CHOICES = Student.GENDER_CHOICES

# Dynamic year choices
import datetime
YEAR_CHOICES = [(r, str(r)) for r in range(2024, datetime.date.today().year + 1)]


def get_stream_choices(school=None, grade_name=None):
    """Return stream choices dynamically from the Stream model for a school, optionally filtered by grade."""
    from .models import Stream, Grade
    qs = Stream.all_objects
    if school:
        qs = qs.filter(school=school)
    if grade_name:
        try:
            grade = Grade.all_objects.get(school=school, name=grade_name)
            qs = qs.filter(grade=grade)
        except Grade.DoesNotExist:
            pass
    streams = qs.values_list("name", flat=True).distinct().order_by("name")
    return [(s, s) for s in streams]


# 1. Form for Registering Students (Updated for Option A)
class StudentForm(SecureFormMixin, forms.ModelForm):
    # Manually declare Guardian fields so they still render on your registration page
    guardian_name = forms.CharField(
        max_length=100, 
        required=True, 
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Guardian Full Name'})
    )
    guardian_phone = forms.CharField(
        max_length=15, 
        required=True, 
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': '07XXXXXXXX'})
    )

    class Meta:
        model = Student
        # Keeps your original layout structure intact for the HTML template
        fields = [
            'admission_no', 'assessment_no', 'name', 'religion', 'gender', 'guardian_name', 
            'class_name', 'guardian_phone', 'stream', 'term', 'year'
        ]
        widgets = {
            'admission_no': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '001'}),
            'assessment_no': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Assessment No'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Full legal name'}),
            'religion': forms.Select(attrs={'class': 'form-control'}, choices=RELIGION_CHOICES),
            'gender': forms.Select(attrs={'class': 'form-control'}, choices=GENDER_CHOICES),
            'class_name': forms.Select(attrs={'class': 'form-control'}, choices=GRADE_CHOICES),
            'stream': forms.Select(attrs={'class': 'form-control'}),
            'term': forms.Select(attrs={'class': 'form-control'}, choices=TERM_CHOICES),
            'year': forms.Select(attrs={'class': 'form-control'}, choices=YEAR_CHOICES),
        }

    def __init__(self, *args, school=None, school_section=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['stream'].choices = get_stream_choices(school)
        self._school_section = school_section
        if school_section in ('PRIMARY', 'LOWER_PRIMARY'):
            self.fields['religion'].required = True
            self.fields['religion'].empty_label = '-- Select Religion (CRE / IRE) --'
        else:
            self.fields['religion'].required = False

    def save(self, commit=True):
        instance = super().save(commit=False)
        class_name = instance.class_name
        if class_name in ('Grade 1', 'Grade 2', 'Grade 3'):
            instance.sub_section = 'LOWER'
        elif class_name in ('Grade 4', 'Grade 5', 'Grade 6'):
            instance.sub_section = 'UPPER'
        else:
            instance.sub_section = None
        if commit:
            instance.save()
        return instance


class StudentEditForm(SecureFormMixin, forms.ModelForm):
    guardian_name = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )
    guardian_phone = forms.CharField(
        max_length=15,
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )

    class Meta:
        model = Student
        fields = [
            'admission_no', 'assessment_no', 'name', 'religion', 'gender',
            'class_name', 'stream', 'term', 'year'
        ]
        widgets = {
            'admission_no': forms.TextInput(attrs={'class': 'form-control'}),
            'assessment_no': forms.TextInput(attrs={'class': 'form-control'}),
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'religion': forms.Select(attrs={'class': 'form-control'}, choices=RELIGION_CHOICES),
            'gender': forms.Select(attrs={'class': 'form-control'}, choices=GENDER_CHOICES),
            'class_name': forms.Select(attrs={'class': 'form-control'}, choices=GRADE_CHOICES),
            'stream': forms.Select(attrs={'class': 'form-control'}),
            'term': forms.Select(attrs={'class': 'form-control'}, choices=TERM_CHOICES),
            'year': forms.Select(attrs={'class': 'form-control'}, choices=YEAR_CHOICES),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        grade_name = None
        if self.instance and self.instance.pk:
            grade_name = self.instance.class_name
        if self.data and 'class_name' in self.data:
            grade_name = self.data['class_name']
        self.fields['stream'].choices = get_stream_choices(school, grade_name)


# 2. Form for Single Mark Entry
class MarkEntryForm(SecureFormMixin, forms.ModelForm):
    class Meta:
        model = Mark
        fields = ['student', 'subject', 'score', 'term', 'year']
        widgets = {
            'student': forms.Select(attrs={'class': 'form-control'}),
            'subject': forms.Select(attrs={'class': 'form-control'}),
            'score': forms.NumberInput(attrs={'class': 'form-control', 'min': '0', 'max': '100'}),
            'term': forms.Select(attrs={'class': 'form-control'}, choices=TERM_CHOICES),
            'year': forms.Select(attrs={'class': 'form-control'}, choices=YEAR_CHOICES),
        }


# 3. Form for selecting Class/Subject before Bulk Entry
class MarkFilterForm(SecureFormMixin, forms.Form):
    year = forms.ChoiceField(choices=YEAR_CHOICES, widget=forms.Select(attrs={'class': 'form-control'}))
    grade = forms.ChoiceField(choices=GRADE_CHOICES, widget=forms.Select(attrs={'class': 'form-control'}))
    stream = forms.ChoiceField(choices=[], widget=forms.Select(attrs={'class': 'form-control'}))
    subject = forms.ModelChoiceField(queryset=Subject.objects.none(), widget=forms.Select(attrs={'class': 'form-control'}))
    term = forms.ChoiceField(choices=TERM_CHOICES, widget=forms.Select(attrs={'class': 'form-control'}))

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['stream'].choices = get_stream_choices(school)
        if school:
            self.fields['subject'].queryset = Subject.objects.filter(school=school, is_active=True).order_by('grade', 'code')


class StrongPasswordChangeForm(PasswordChangeForm):
    """Custom password change form with strong password validation."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['old_password'].widget.attrs.update({
            'class': 'form-control',
            'placeholder': 'Enter current password',
        })
        self.fields['new_password1'].widget.attrs.update({
            'class': 'form-control',
            'placeholder': 'Enter new password',
        })
        self.fields['new_password2'].widget.attrs.update({
            'class': 'form-control',
            'placeholder': 'Confirm new password',
        })

    def clean_new_password1(self):
        password = self.cleaned_data.get('new_password1')
        errors = []

        if len(password) < 8:
            errors.append("Password must be at least 8 characters long.")
        if not re.search(r'[A-Z]', password):
            errors.append("Password must contain at least one uppercase letter.")
        if not re.search(r'[a-z]', password):
            errors.append("Password must contain at least one lowercase letter.")
        if not re.search(r'\d', password):
            errors.append("Password must contain at least one digit.")
        if not re.search(r'[!@#$%^&*(),.?\":{}|<>]', password):
            errors.append("Password must contain at least one special character (!@#$%^&* etc.).")
        if re.search(r'(.)\1{2,}', password):
            errors.append("Password must not contain 3 or more repeated characters.")
        if password.lower() in ['password', '12345678', 'qwerty']:
            errors.append("Password is too common. Please choose a stronger password.")

        if errors:
            raise forms.ValidationError(errors)
        return password
