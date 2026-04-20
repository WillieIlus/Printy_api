from urllib.parse import parse_qs, urlparse

from django.core import mail
from django.test import TestCase
from django.test.utils import override_settings
from rest_framework.test import APIClient

from allauth.account.models import EmailAddress

from .models import User, UserProfile
from shops.models import Shop, ShopMembership


class AccountProfileAPITestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="owner@test.com",
            password="pass12345",
            name="Owner User",
        )
        self.client.force_authenticate(user=self.user)

    def test_users_me_updates_user_and_profile_fields(self):
        response = self.client.patch(
            "/api/users/me/",
            {
                "first_name": "Amina",
                "last_name": "Otieno",
                "role": "shop_owner",
                "preferred_language": "sw",
                "phone": "+254700000000",
                "bio": "Print production lead",
                "address": "Muthithi Road",
                "city": "Westlands",
                "state": "Nairobi",
                "country": "Kenya",
                "postal_code": "00100",
                "social_links": [
                    {"platform": "website", "url": "https://printy.ke"},
                    {"platform": "instagram", "url": "https://instagram.com/printy"},
                ],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        profile = UserProfile.objects.get(user=self.user)

        self.assertEqual(self.user.first_name, "Amina")
        self.assertEqual(self.user.last_name, "Otieno")
        self.assertEqual(self.user.name, "Amina Otieno")
        self.assertEqual(self.user.role, "shop_owner")
        self.assertEqual(self.user.preferred_language, "sw")
        self.assertEqual(profile.phone, "+254700000000")
        self.assertEqual(profile.city, "Westlands")
        self.assertEqual(profile.social_links.count(), 2)
        self.assertEqual(response.json()["social_links"][0]["platform"], "website")

    def test_profiles_me_patch_persists_nested_social_links(self):
        response = self.client.patch(
            "/api/profiles/me/",
            {
                "bio": "Offset and digital specialist",
                "phone": "+254711111111",
                "address": "Madonna House, 2nd Floor",
                "city": "Westlands",
                "state": "Nairobi",
                "country": "Kenya",
                "postal_code": "00800",
                "social_links": [
                    {"platform": "linkedin", "url": "https://linkedin.com/in/printy"},
                ],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual(profile.bio, "Offset and digital specialist")
        self.assertEqual(profile.social_links.count(), 1)
        self.assertEqual(response.json()["phone"], "+254711111111")

    def test_profile_social_link_routes_allow_create_and_delete(self):
        profile = UserProfile.objects.create(user=self.user)

        create_response = self.client.post(
            f"/api/profiles/{profile.id}/social-links/",
            {"platform": "facebook", "url": "https://facebook.com/printy"},
            format="json",
        )
        self.assertEqual(create_response.status_code, 201)

        link_id = create_response.json()["id"]
        delete_response = self.client.delete(f"/api/social-links/{link_id}/")
        self.assertEqual(delete_response.status_code, 204)
        self.assertEqual(profile.social_links.count(), 0)

    def test_shop_creation_promotes_client_to_shop_owner(self):
        self.assertEqual(self.user.role, User.Role.CLIENT)

        Shop.objects.create(name="Role Sync Shop", slug="role-sync-shop", owner=self.user)

        self.user.refresh_from_db()
        self.assertEqual(self.user.role, User.Role.SHOP_OWNER)

    def test_active_membership_promotes_client_to_staff(self):
        owner = User.objects.create_user(
            email="owner2@test.com",
            password="pass12345",
            role=User.Role.SHOP_OWNER,
        )
        shop = Shop.objects.create(name="Staff Shop", slug="staff-shop", owner=owner)

        ShopMembership.objects.create(
            shop=shop,
            user=self.user,
            role=ShopMembership.Role.STAFF,
            is_active=True,
        )

        self.user.refresh_from_db()
        self.assertEqual(self.user.role, User.Role.STAFF)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    FRONTEND_URL="https://printy.ke",
)
class EmailVerificationFlowAPITestCase(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _extract_confirmation_key(self, message_body: str) -> str:
        for token in message_body.split():
            if "key=" not in token:
                continue
            parsed = urlparse(token.strip())
            key = parse_qs(parsed.query).get("key", [None])[0]
            if key:
                return key
        self.fail("Could not extract confirmation key from email body.")

    def test_register_sends_verification_email_and_blocks_login_until_confirmed(self):
        register_response = self.client.post(
            "/api/auth/register/",
            {
                "email": "new-user@test.com",
                "password": "Pass12345",
                "name": "New User",
                "role": "client",
            },
            format="json",
        )

        self.assertEqual(register_response.status_code, 201)
        self.assertEqual(len(mail.outbox), 1)

        email_address = EmailAddress.objects.get(email="new-user@test.com")
        self.assertFalse(email_address.verified)

        login_response = self.client.post(
            "/api/auth/token/",
            {"email": "new-user@test.com", "password": "Pass12345"},
            format="json",
        )

        self.assertEqual(login_response.status_code, 400)
        self.assertIn("not verified", login_response.json()["message"].lower())

    def test_resend_alias_and_confirm_alias_complete_verification_flow(self):
        self.client.post(
            "/api/auth/register/",
            {
                "email": "verify-me@test.com",
                "password": "Pass12345",
                "name": "Verify Me",
                "role": "client",
            },
            format="json",
        )
        self.assertEqual(len(mail.outbox), 1)

        resend_response = self.client.post(
            "/api/auth/email/resend/",
            {"email": "verify-me@test.com"},
            format="json",
        )

        self.assertEqual(resend_response.status_code, 200)
        self.assertEqual(resend_response.json()["sent"], True)
        self.assertEqual(len(mail.outbox), 2)

        key = self._extract_confirmation_key(mail.outbox[-1].body)
        confirm_response = self.client.post(
            "/api/auth/email/verify/",
            {"key": key},
            format="json",
        )

        self.assertEqual(confirm_response.status_code, 200)
        self.assertEqual(confirm_response.json()["verified"], True)

        email_address = EmailAddress.objects.get(email="verify-me@test.com")
        self.assertTrue(email_address.verified)

        login_response = self.client.post(
            "/api/auth/token/",
            {"email": "verify-me@test.com", "password": "Pass12345"},
            format="json",
        )

        self.assertEqual(login_response.status_code, 200)
        self.assertIn("access", login_response.json())

    def test_resend_for_missing_email_is_safe_and_does_not_leak(self):
        response = self.client.post(
            "/api/auth/email/resend/",
            {"email": "missing-user@test.com"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sent"], False)
        self.assertEqual(len(mail.outbox), 0)

    def test_missing_api_route_returns_json_not_html(self):
        response = self.client.post("/api/auth/email/not-a-real-route/", {}, format="json")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["code"], "NOT_FOUND")
