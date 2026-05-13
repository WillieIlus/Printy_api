from django.test import TestCase
from rest_framework.test import APIRequestFactory

from accounts.models import User
from production.models import Customer
from production.serializers import ProductionOrderWriteSerializer
from quotes.models import QuoteRequest, ShopQuote
from shops.models import Shop


class ProductionRelationshipFoundationTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.owner = User.objects.create_user(
            email="production-owner@test.com",
            password="pass12345",
            role=User.Role.SHOP_OWNER,
        )
        self.client_user = User.objects.create_user(
            email="production-client@test.com",
            password="pass12345",
            role=User.Role.CLIENT,
        )
        self.shop = Shop.objects.create(name="Production Shop", slug="production-shop", owner=self.owner)
        self.quote_request = QuoteRequest.objects.create(
            shop=self.shop,
            created_by=self.client_user,
            customer_name="Client One",
            customer_email="client-one@test.com",
            customer_phone="+254700123456",
            status=QuoteRequest.SUBMITTED,
        )
        self.shop_quote = ShopQuote.objects.create(
            quote_request=self.quote_request,
            shop=self.shop,
            created_by=self.owner,
            status=ShopQuote.ACCEPTED,
            total="2500.00",
        )

    def test_create_job_from_quote_sets_legacy_relationship_foundation_defaults(self):
        request = self.factory.post("/api/jobs/", {"shop_quote": self.shop_quote.id}, format="json")
        request.user = self.owner
        serializer = ProductionOrderWriteSerializer(
            data={
                "shop_quote": self.shop_quote.id,
                "status": "pending",
                "quantity": 10,
            },
            context={"request": request},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        job = serializer.save()

        self.assertIsNotNone(job.customer)
        self.assertEqual(job.customer.relationship_owner_type, Customer.RelationshipOwnerType.UNKNOWN)
        self.assertEqual(job.customer.acquisition_source, Customer.AcquisitionSource.LEGACY_QUOTE)

    def test_customer_relationship_reference_reports_selected_owner(self):
        customer = Customer.objects.create(
            shop=self.shop,
            name="Owned Client",
            relationship_owner_type=Customer.RelationshipOwnerType.SHOP,
            relationship_owner_shop=self.shop,
            acquisition_source=Customer.AcquisitionSource.SHOP,
        )

        self.assertEqual(customer.relationship_owner_reference(), f"shop:{self.shop.id}")
