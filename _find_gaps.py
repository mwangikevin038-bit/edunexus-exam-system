from students.models import Student

current = sorted({int(s) for s in Student.all_objects.filter(school_section='JSS').values_list('admission_no', flat=True) if s and str(s).isdigit()})
gaps = []
for i in range(len(current) - 1):
    if current[i+1] - current[i] > 1:
        gaps.append((current[i], current[i+1]))
print(f'Gaps in JSS admission numbers: {gaps}')

# Find the largest contiguous gap of 110+ numbers
print(f'Min: {current[0]}, Max: {current[-1]}')
# Check 700-999 range
free_in_700_999 = [n for n in range(700, 1000) if n not in set(current)]
print(f'Free numbers in 700-999: {len(free_in_700_999)}')
print(f'First 110 free in 700+: {free_in_700_999[:110] if len(free_in_700_999) >= 110 else "NOT ENOUGH"}')
