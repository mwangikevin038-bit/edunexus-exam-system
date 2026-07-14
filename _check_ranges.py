from students.models import Student

for grade, stream in [('Grade 7', 'Yellow'), ('Grade 7', 'Blue'), ('Grade 8', 'Main'), ('Grade 9', 'Main')]:
    nums = list(Student.all_objects.filter(class_name=grade, stream=stream).order_by('admission_no').values_list('admission_no', flat=True))
    if not nums:
        print(f'{grade} {stream}: (empty)')
        continue
    nums_int = sorted({int(n) for n in nums if n and str(n).isdigit()})
    print(f'{grade} {stream}: {len(nums)} students, adm range {nums_int[0]}-{nums_int[-1]}, count={len(nums_int)}')
    # check overlaps with Grade 9's target range
    g9_targets = set(range(105, 220)) | {224, 308, 329, 342, 363, 441, 446, 447}
    overlap = set(nums_int) & g9_targets
    if overlap:
        print(f'  OVERLAP with Grade 9 target numbers: {sorted(overlap)}')
