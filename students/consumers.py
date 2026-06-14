"""
WebSocket consumers for real-time communication.

Provides UploadProgressConsumer for streaming CSV upload progress
to the frontend via WebSocket.
"""

import json

from channels.generic.websocket import AsyncWebsocketConsumer


class UploadProgressConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.upload_id = self.scope["url_route"]["kwargs"]["upload_id"]
        self.group_name = f"upload_{self.upload_id}"

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data):
        pass

    async def upload_progress(self, event):
        """
        Called when the Celery worker sends a progress_update group message.
        event["data"] is a dict like:
            { "processed": 200, "total": 500, "created": 180, "updated": 20, "errors": [...], "status": "processing" }
        """
        await self.send(text_data=json.dumps(event["data"]))

    async def upload_complete(self, event):
        """
        Called when processing is fully done.
        event["data"] is the final summary dict.
        """
        await self.send(text_data=json.dumps(event["data"]))
