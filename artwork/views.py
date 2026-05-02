from pathlib import Path

from django.core.files import File
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from .models import UploadedArtwork
from .services.pdf_analysis import analyze_pdf


class ArtworkUploadView(APIView):
    permission_classes = [AllowAny]
    parser_classes = [MultiPartParser]

    def post(self, request):
        file = request.FILES.get('file')
        if not file:
            return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)

        file_type = _file_ext(file.name)
        artwork = UploadedArtwork.objects.create(file=file, file_type=file_type)

        analysis: dict = {
            'analysis_status': 'skipped',
            'analysis_error': None,
            'preview_path': None,
            'detected': None,
            'suggested_product': None,
            'suggestions': [],
            'warnings': [],
            'confidence': None,
            'pages': None,
            'width_mm': None,
            'height_mm': None,
        }
        if file_type == 'pdf':
            analysis = analyze_pdf(artwork.file.path)
        else:
            analysis['warnings'] = ['Automatic PDF analysis was skipped for this file type.']

        detected = analysis.get('detected') or {}
        artwork.detected_pages = detected.get('pages')
        artwork.detected_width_mm = detected.get('width_mm')
        artwork.detected_height_mm = detected.get('height_mm')
        artwork.analysis_status = analysis.get('analysis_status', 'pending')
        artwork.analysis_warnings = analysis.get('warnings', [])
        artwork.analysis_error = analysis.get('analysis_error')
        artwork.analysis = analysis

        preview_path = analysis.get('preview_path')
        if preview_path and Path(preview_path).exists():
            with open(preview_path, 'rb') as preview_file:
                artwork.preview_image.save(
                    f'{artwork.id}_preview.jpg',
                    File(preview_file),
                    save=False,
                )
            Path(preview_path).unlink(missing_ok=True)
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
        'detected': analysis.get('detected'),
        'suggested_product': analysis.get('suggested_product'),
        'suggestions': analysis.get('suggestions', []),
        'warnings': artwork.analysis_warnings or analysis.get('warnings', []),
        'confidence': analysis.get('confidence'),
    }
