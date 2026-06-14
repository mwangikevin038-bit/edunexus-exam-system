"""
Context processors for the students app.

Provides the school_context processor that attaches the current school
to every request for template access via {{ request.school }}.
"""

from .school_scope import get_current_school

def school_context(request):
    """
    Automatically attaches the active logged-in school to the 'request' object
    so {{ request.school }} works on every page across the system.
    """
    if request.user.is_authenticated:
        # Fetch the active school using your project's built-in utility
        active_school = get_current_school()
        
        # Attach it directly to the request object so your templates can read it
        request.school = active_school
        
    else:
        request.school = None

    # Return an empty dictionary because we are altering the 'request' object itself
    return {}
