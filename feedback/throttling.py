from rest_framework.throttling import UserRateThrottle


class BetaFeedbackThrottle(UserRateThrottle):
    """10 feedback submissions per minute per user."""

    rate = "10/min"
