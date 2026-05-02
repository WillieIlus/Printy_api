from django.db import models


class UploadedArtwork(models.Model):
    file = models.FileField(upload_to='artwork/')
    preview_image = models.ImageField(upload_to='artwork/previews/', null=True, blank=True)
    file_type = models.CharField(max_length=20, blank=True)
    detected_pages = models.IntegerField(null=True, blank=True)
    detected_width_mm = models.FloatField(null=True, blank=True)
    detected_height_mm = models.FloatField(null=True, blank=True)
    analysis_status = models.CharField(max_length=20, default='pending')
    analysis_warnings = models.JSONField(default=list, blank=True)
    analysis_error = models.TextField(blank=True, null=True)
    analysis = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
