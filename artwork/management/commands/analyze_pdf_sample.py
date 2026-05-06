from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from artwork.services.pdf_analysis import analyze_pdf


class Command(BaseCommand):
    help = "Analyze a sample PDF and print page count, first-page size, and points-to-mm conversion."

    def add_arguments(self, parser):
        parser.add_argument("pdf_path", type=str, help="Path to the PDF file to analyze.")

    def handle(self, *args, **options):
        pdf_path = Path(options["pdf_path"]).expanduser()
        if not pdf_path.exists():
            raise CommandError(f"PDF not found: {pdf_path}")

        result = analyze_pdf(pdf_path)
        detected = result.get("detected") or {}

        self.stdout.write(f"analysis_status: {result.get('analysis_status')}")
        self.stdout.write(f"analysis_error_code: {result.get('analysis_error_code')}")
        self.stdout.write(f"analysis_error: {result.get('analysis_error')}")
        self.stdout.write(f"page_count: {result.get('pages')}")
        self.stdout.write(f"first_page_width_mm: {result.get('width_mm')}")
        self.stdout.write(f"first_page_height_mm: {result.get('height_mm')}")
        self.stdout.write(f"detected_unit: {detected.get('unit')}")
        self.stdout.write(f"points_to_mm: {detected.get('points_to_mm')}")
