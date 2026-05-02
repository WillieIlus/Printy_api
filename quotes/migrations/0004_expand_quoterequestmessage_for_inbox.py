from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("shops", "0002_shop_opening_hours_text_shop_public_email_and_more"),
        ("quotes", "0003_alter_quoterequest_status"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="quoterequestmessage",
            name="direction",
            field=models.CharField(choices=[("inbound", "Inbound"), ("outbound", "Outbound")], default="inbound", help_text="Inbox/outbox direction for the receiving user.", max_length=20, verbose_name="direction"),
        ),
        migrations.AddField(
            model_name="quoterequestmessage",
            name="email_error",
            field=models.CharField(blank=True, default="", help_text="Safe delivery error summary.", max_length=255, verbose_name="email error"),
        ),
        migrations.AddField(
            model_name="quoterequestmessage",
            name="email_sent",
            field=models.BooleanField(default=False, help_text="Whether an email copy was sent successfully.", verbose_name="email sent"),
        ),
        migrations.AddField(
            model_name="quoterequestmessage",
            name="email_status",
            field=models.CharField(choices=[("not_sent", "Not sent"), ("sent", "Sent"), ("failed", "Failed"), ("bounced", "Bounced")], default="not_sent", help_text="Delivery state for the optional email copy.", max_length=20, verbose_name="email status"),
        ),
        migrations.AddField(
            model_name="quoterequestmessage",
            name="message_type",
            field=models.CharField(choices=[("quote_request_created", "Quote request created"), ("quote_response_sent", "Quote response sent"), ("quote_question", "Quote question"), ("quote_accepted", "Quote accepted"), ("quote_rejected", "Quote rejected"), ("system_notice", "System notice"), ("email_delivery_failed", "Email delivery failed")], default="system_notice", help_text="Normalized message event type for inbox/outbox.", max_length=40, verbose_name="message type"),
        ),
        migrations.AddField(
            model_name="quoterequestmessage",
            name="read_at",
            field=models.DateTimeField(blank=True, help_text="When the recipient opened this message.", null=True, verbose_name="read at"),
        ),
        migrations.AddField(
            model_name="quoterequestmessage",
            name="recipient",
            field=models.ForeignKey(blank=True, help_text="User who should see this inbox message.", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="received_quote_request_messages", to=settings.AUTH_USER_MODEL, verbose_name="recipient"),
        ),
        migrations.AddField(
            model_name="quoterequestmessage",
            name="recipient_email",
            field=models.EmailField(blank=True, help_text="Email copy recipient, if applicable.", max_length=254, verbose_name="recipient email"),
        ),
        migrations.AddField(
            model_name="quoterequestmessage",
            name="recipient_role",
            field=models.CharField(choices=[("client", "Client"), ("shop_owner", "Shop owner"), ("admin", "Admin"), ("system", "System")], default="system", help_text="Who this message is intended for.", max_length=20, verbose_name="recipient role"),
        ),
        migrations.AddField(
            model_name="quoterequestmessage",
            name="sent_at",
            field=models.DateTimeField(blank=True, help_text="When the message event was emitted.", null=True, verbose_name="sent at"),
        ),
        migrations.AddField(
            model_name="quoterequestmessage",
            name="shop",
            field=models.ForeignKey(blank=True, help_text="Shop this message belongs to.", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="quote_request_messages", to="shops.shop", verbose_name="shop"),
        ),
        migrations.AddField(
            model_name="quoterequestmessage",
            name="subject",
            field=models.CharField(blank=True, default="", help_text="Short subject line for message lists.", max_length=255, verbose_name="subject"),
        ),
        migrations.AddIndex(
            model_name="quoterequestmessage",
            index=models.Index(fields=["recipient", "read_at"], name="qmsg_recipient_read_idx"),
        ),
        migrations.AddIndex(
            model_name="quoterequestmessage",
            index=models.Index(fields=["quote_request", "direction"], name="qmsg_request_direction_idx"),
        ),
        migrations.AddIndex(
            model_name="quoterequestmessage",
            index=models.Index(fields=["shop", "recipient_role"], name="qmsg_shop_role_idx"),
        ),
    ]
