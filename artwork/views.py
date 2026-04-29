from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from .models import UploadedArtwork
from .services import analyze_pdf


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
            'detected': None,
            'suggestions': [],
            'warnings': [],
            'confidence': None,
        }
        if file_type == 'pdf':
            analysis = analyze_pdf(artwork.file.path)
        else:
            analysis['warnings'] = ['Automatic PDF analysis was skipped for this file type.']

        detected = analysis.get('detected') or {}
        artwork.detected_pages = detected.get('pages')
        artwork.detected_width_mm = detected.get('width_mm')
        artwork.detected_height_mm = detected.get('height_mm')
        artwork.analysis = analysis
        artwork.save()

        return Response(
            {
                'artwork_id': artwork.id,
                'file_url': request.build_absolute_uri(artwork.file.url),
                'upload_status': 'uploaded',
                'analysis_status': analysis.get('analysis_status', 'skipped'),
                'analysis_error': analysis.get('analysis_error'),
                'detected': analysis.get('detected'),
                'suggestions': analysis.get('suggestions', []),
                'warnings': analysis.get('warnings', []),
                'confidence': analysis.get('confidence'),
            },
            status=status.HTTP_201_CREATED,
        )


class ArtworkDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, pk):
        try:
            artwork = UploadedArtwork.objects.get(pk=pk)
        except UploadedArtwork.DoesNotExist:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            'artwork_id': artwork.id,
            'file_url': request.build_absolute_uri(artwork.file.url),
            'file_type': artwork.file_type,
            'upload_status': 'uploaded',
            'analysis_status': artwork.analysis.get('analysis_status', 'skipped'),
            'analysis_error': artwork.analysis.get('analysis_error'),
            'detected': artwork.analysis.get('detected'),
            'suggestions': artwork.analysis.get('suggestions', []),
            'warnings': artwork.analysis.get('warnings', []),
            'confidence': artwork.analysis.get('confidence'),
        })


def _file_ext(filename: str) -> str:
    if '.' not in filename:
        return ''
    return filename.rsplit('.', 1)[-1].lower()
