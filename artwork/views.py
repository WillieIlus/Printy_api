import logging

from django.core.files.base import ContentFile
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import UploadedArtwork
from .services.pdf_analysis import analyze_pdf


logger = logging.getLogger("artwork.upload")


class ArtworkUploadView(APIView):
    permission_classes = [AllowAny]
    parser_classes = [MultiPartParser]

    def post(self, request):
        file = request.FILES.get('file')
        if not file:
            return Response(
                {
                    'upload_status': 'failed',
                    'error': 'No file provided',
                    'error_code': 'no_file',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        file_type = _file_ext(file.name)
        try:
            artwork = UploadedArtwork.objects.create(file=file, file_type=file_type)
        except Exception:
            logger.exception("Artwork upload failed while saving %s", getattr(file, 'name', '<unknown>'))
            return Response(
                {
                    'upload_status': 'failed',
                    'error': 'Upload failed',
                    'error_code': 'upload_failed',
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        analysis: dict = {
            'analysis_status': 'skipped',
            'analysis_error': None,
            'analysis_error_code': None,
            'analysis_technical_detail': None,
            'detected': None,
            'suggested_product': None,
            'suggestions': [],
            'warnings': [],
            'confidence': None,
            'pages': None,
            'width_mm': None,
            'height_mm': None,
            'preview_format': None,
        }
        if file_type == 'pdf':
            analysis = analyze_pdf(artwork.file)
        else:
            analysis['warnings'] = ['Automatic PDF analysis was skipped for this file type.']

        preview_bytes = analysis.pop('_preview_bytes', None)
        detected = analysis.get('detected') or {}
        artwork.detected_pages = detected.get('pages')
        artwork.detected_width_mm = detected.get('width_mm')
        artwork.detected_height_mm = detected.get('height_mm')
        artwork.analysis_status = analysis.get('analysis_status', 'pending')
        artwork.analysis_warnings = analysis.get('warnings', [])
        artwork.analysis_error = analysis.get('analysis_error')
        artwork.analysis = analysis

        if preview_bytes:
            try:
                artwork.preview_image.save(
                    f'{artwork.id}_preview.jpg',
                    ContentFile(preview_bytes),
                    save=False,
                )
            except Exception:
                logger.exception("Artwork preview save failed for artwork %s", artwork.id)
                artwork.analysis_warnings = [
                    *artwork.analysis_warnings,
                    'Preview image generation failed.',
                ]
                artwork.analysis['warnings'] = artwork.analysis_warnings
                artwork.analysis['analysis_warnings'] = artwork.analysis_warnings

        artwork.save()

        return Response(
            _serialize_artwork(request, artwork),
            status=status.HTTP_201_CREATED,
        )


class ArtworkDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, pk):
        try:
            artwork = UploadedArtwork.objects.get(pk=pk)
        except UploadedArtwork.DoesNotExist:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

        return Response(_serialize_artwork(request, artwork))


def _file_ext(filename: str) -> str:
    if '.' not in filename:
        return ''
    return filename.rsplit('.', 1)[-1].lower()


def _serialize_artwork(request, artwork: UploadedArtwork) -> dict:
    analysis = artwork.analysis or {}
    preview_image = (
        request.build_absolute_uri(artwork.preview_image.url)
        if artwork.preview_image
        else None
    )
    return {
        'artwork_id': artwork.id,
        'file_url': request.build_absolute_uri(artwork.file.url),
        'file_type': artwork.file_type,
        'preview_image': preview_image,
        'upload_status': 'uploaded',
        'detected_pages': artwork.detected_pages,
        'detected_width_mm': artwork.detected_width_mm,
        'detected_height_mm': artwork.detected_height_mm,
        'analysis_status': artwork.analysis_status or analysis.get('analysis_status', 'pending'),
        'analysis_warnings': artwork.analysis_warnings or analysis.get('warnings', []),
        'analysis_error': artwork.analysis_error or analysis.get('analysis_error'),
        'analysis_error_code': analysis.get('analysis_error_code'),
        'analysis_technical_detail': analysis.get('analysis_technical_detail'),
        'detected': analysis.get('detected'),
        'suggested_product': analysis.get('suggested_product'),
        'suggestions': analysis.get('suggestions', []),
        'warnings': artwork.analysis_warnings or analysis.get('warnings', []),
        'confidence': analysis.get('confidence'),
    }
