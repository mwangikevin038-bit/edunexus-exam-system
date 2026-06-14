"""
WebSocket URL routing for the students app.

Defines WebSocket endpoints for real-time features.
"""

from django.urls import re_path

from students import consumers

websocket_urlpatterns = [
    re_path(r"ws/upload-progress/(?P<upload_id>[0-9a-f-]+)/$", consumers.UploadProgressConsumer.as_asgi()),
]
