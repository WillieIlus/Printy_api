"""Reusable role checks and canonical role normalization."""

from __future__ import annotations

from django.db import transaction

from accounts.models import User, UserRole

CANONICAL_SUPER_ADMIN_ROLE = "super_admin"
CANONICAL_CLIENT_ROLE = "client"
CANONICAL_PARTNER_ROLE = "partner"
CANONICAL_PRODUCTION_ROLE = "production"
CANONICAL_ROLE_PRIORITY = (
    CANONICAL_SUPER_ADMIN_ROLE,
    CANONICAL_PRODUCTION_ROLE,
    CANONICAL_PARTNER_ROLE,
    CANONICAL_CLIENT_ROLE,
)
ROLE_ROUTE_MAP = {
    CANONICAL_SUPER_ADMIN_ROLE: "/dashboard/admin",
    CANONICAL_CLIENT_ROLE: "/dashboard/client",
    CANONICAL_PARTNER_ROLE: "/dashboard/partner",
    CANONICAL_PRODUCTION_ROLE: "/dashboard/production",
}
ROLE_ALIASES = {
    User.Role.SUPER_ADMIN: CANONICAL_SUPER_ADMIN_ROLE,
    User.Role.ADMIN: CANONICAL_SUPER_ADMIN_ROLE,
    "superuser": CANONICAL_SUPER_ADMIN_ROLE,
    "super_admin": CANONICAL_SUPER_ADMIN_ROLE,
    "admin": CANONICAL_SUPER_ADMIN_ROLE,
    User.Role.STAFF: CANONICAL_SUPER_ADMIN_ROLE,
    "staff": CANONICAL_SUPER_ADMIN_ROLE,
    User.Role.CLIENT: CANONICAL_CLIENT_ROLE,
    CANONICAL_CLIENT_ROLE: CANONICAL_CLIENT_ROLE,
    "customer": CANONICAL_CLIENT_ROLE,
    "buyer": CANONICAL_CLIENT_ROLE,
    User.Role.BROKER: CANONICAL_PARTNER_ROLE,
    User.Role.PARTNER: CANONICAL_PARTNER_ROLE,
    CANONICAL_PARTNER_ROLE: CANONICAL_PARTNER_ROLE,
    User.Role.SHOP_OWNER: CANONICAL_PRODUCTION_ROLE,
    User.Role.PRODUCTION: CANONICAL_PRODUCTION_ROLE,
    CANONICAL_PRODUCTION_ROLE: CANONICAL_PRODUCTION_ROLE,
    User.Role.PRINTER: CANONICAL_PRODUCTION_ROLE,
    "printer": CANONICAL_PRODUCTION_ROLE,
    "production_shop": CANONICAL_PRODUCTION_ROLE,
}


def normalize_role_value(value: str | None) -> str | None:
    if not value:
        return None
    return ROLE_ALIASES.get(str(value).strip().lower())


def get_supported_role_values() -> set[str]:
    return {
        User.Role.SUPER_ADMIN,
        User.Role.ADMIN,
        User.Role.CLIENT,
        User.Role.PARTNER,
        User.Role.PRODUCTION,
        User.Role.BROKER,
        User.Role.SHOP_OWNER,
        User.Role.PRINTER,
        User.Role.STAFF,
        CANONICAL_SUPER_ADMIN_ROLE,
        CANONICAL_CLIENT_ROLE,
        CANONICAL_PARTNER_ROLE,
        CANONICAL_PRODUCTION_ROLE,
        "printer",
        "admin",
        "superuser",
        "buyer",
        "customer",
        "production_shop",
    }


def get_public_assignable_roles() -> set[str]:
    return {CANONICAL_CLIENT_ROLE, CANONICAL_PARTNER_ROLE, CANONICAL_PRODUCTION_ROLE}


def has_role(user: User, *roles: str) -> bool:
    normalized_targets = {normalize_role_value(role) for role in roles}
    normalized_targets.discard(None)
    return bool(user and getattr(user, "is_authenticated", False) and normalized_targets.intersection(resolve_user_roles(user)))


def is_client(user: User) -> bool:
    return CANONICAL_CLIENT_ROLE in resolve_user_roles(user)


def is_broker(user: User) -> bool:
    return CANONICAL_PARTNER_ROLE in resolve_user_roles(user)


def is_shop_owner(user: User) -> bool:
    return CANONICAL_PRODUCTION_ROLE in resolve_user_roles(user)


def is_staff_member(user: User) -> bool:
    return bool(user and user.is_authenticated and getattr(user, "role", None) == User.Role.STAFF)


def is_platform_staff(user: User) -> bool:
    return bool(user and user.is_authenticated and user.is_staff)


def _owned_shop_count(user: User) -> int:
    if not user or not getattr(user, "is_authenticated", False):
        return 0
    owned_shops = getattr(user, "owned_shops", None)
    if owned_shops is None:
        return 0
    try:
        return owned_shops.count()
    except Exception:
        return 0


def _has_active_shop_membership(user: User) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    memberships = getattr(user, "shop_memberships", None)
    if memberships is None:
        return False
    try:
        return memberships.filter(is_active=True).exists()
    except Exception:
        return False


def resolve_user_roles(user: User) -> list[str]:
    if not user or not getattr(user, "is_authenticated", False):
        return []

    roles: set[str] = set()
    persisted_roles = getattr(user, "user_roles", None)
    if persisted_roles is not None:
        try:
            for value in persisted_roles.filter(is_active=True).values_list("role", flat=True):
                normalized = normalize_role_value(value)
                if normalized:
                    roles.add(normalized)
        except Exception:
            pass

    normalized_role = normalize_role_value(getattr(user, "role", None))
    if normalized_role:
        roles.add(normalized_role)

    if bool(getattr(user, "partner_profile_enabled", False)):
        roles.add(CANONICAL_PARTNER_ROLE)

    if _owned_shop_count(user) > 0 or _has_active_shop_membership(user):
        roles.add(CANONICAL_PRODUCTION_ROLE)

    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        roles.add(CANONICAL_SUPER_ADMIN_ROLE)

    if not roles:
        roles.add(CANONICAL_CLIENT_ROLE)

    return [role for role in CANONICAL_ROLE_PRIORITY if role in roles]


def resolve_primary_role(user: User) -> str:
    roles = resolve_user_roles(user)
    return roles[0] if roles else CANONICAL_CLIENT_ROLE


def resolve_home_route(user: User) -> str:
    return ROLE_ROUTE_MAP.get(resolve_primary_role(user), ROLE_ROUTE_MAP[CANONICAL_CLIENT_ROLE])


def role_flags_for_user(user: User) -> dict[str, bool]:
    roles = set(resolve_user_roles(user))
    is_super_admin = CANONICAL_SUPER_ADMIN_ROLE in roles
    return {
        "can_access_admin_dashboard": is_super_admin,
        "can_access_client_dashboard": is_super_admin or CANONICAL_CLIENT_ROLE in roles,
        "can_access_partner_dashboard": is_super_admin or CANONICAL_PARTNER_ROLE in roles,
        "can_access_production_dashboard": is_super_admin or CANONICAL_PRODUCTION_ROLE in roles,
    }


def get_assignable_roles():
    return get_supported_role_values()


def set_account_role(user: User, role: str) -> User:
    normalized_role = normalize_role_value(role) or role
    if normalized_role not in get_supported_role_values():
        raise ValueError(f"Unsupported role: {role}")
    stored_role = _legacy_role_for_canonical_role(normalized_role)
    if user.role != stored_role:
        user.role = stored_role
        user.save(update_fields=["role", "updated_at"])
    return user


def _legacy_role_for_canonical_role(role: str) -> str:
    if role == CANONICAL_SUPER_ADMIN_ROLE:
        return User.Role.SUPER_ADMIN
    if role == CANONICAL_PARTNER_ROLE:
        return User.Role.PARTNER
    if role == CANONICAL_PRODUCTION_ROLE:
        return User.Role.PRODUCTION
    return User.Role.CLIENT


def _active_user_role_record(user: User, role: str) -> UserRole | None:
    try:
        return user.user_roles.get(role=role)
    except UserRole.DoesNotExist:
        return None


def sync_legacy_role(user: User) -> User:
    primary_role = resolve_primary_role(user)
    legacy_role = _legacy_role_for_canonical_role(primary_role)
    fields_to_update: list[str] = []
    if user.role != legacy_role:
        user.role = legacy_role
        fields_to_update.append("role")
    if primary_role == CANONICAL_PARTNER_ROLE and not user.partner_profile_enabled:
        user.partner_profile_enabled = True
        fields_to_update.append("partner_profile_enabled")
    if fields_to_update:
        fields_to_update.append("updated_at")
        user.save(update_fields=fields_to_update)
    return user


@transaction.atomic
def assign_role(user: User, role: str, source: str = "", assigned_by: User | None = None) -> UserRole:
    normalized_role = normalize_role_value(role)
    if normalized_role not in {
        CANONICAL_CLIENT_ROLE,
        CANONICAL_PARTNER_ROLE,
        CANONICAL_PRODUCTION_ROLE,
        CANONICAL_SUPER_ADMIN_ROLE,
    }:
        raise ValueError(f"Unsupported role: {role}")
    user_role, created = UserRole.objects.get_or_create(
        user=user,
        role=normalized_role,
        defaults={
            "is_active": True,
            "source": source or "",
            "assigned_by": assigned_by,
        },
    )
    if not created:
        fields_to_update: list[str] = []
        if not user_role.is_active:
            user_role.is_active = True
            fields_to_update.append("is_active")
        if source and user_role.source != source:
            user_role.source = source
            fields_to_update.append("source")
        if assigned_by is not None and user_role.assigned_by_id != assigned_by.id:
            user_role.assigned_by = assigned_by
            fields_to_update.append("assigned_by")
        if fields_to_update:
            fields_to_update.append("updated_at")
            user_role.save(update_fields=fields_to_update)
    sync_legacy_role(user)
    return user_role


@transaction.atomic
def remove_role(user: User, role: str) -> None:
    normalized_role = normalize_role_value(role)
    if not normalized_role:
        return
    user_role = _active_user_role_record(user, normalized_role)
    if user_role and user_role.is_active:
        user_role.is_active = False
        user_role.save(update_fields=["is_active", "updated_at"])
    sync_legacy_role(user)


def user_has_role(user: User, role: str) -> bool:
    normalized_role = normalize_role_value(role)
    return bool(normalized_role and normalized_role in resolve_user_roles(user))


def promote_to_shop_owner(user: User) -> User:
    assign_role(user, CANONICAL_PRODUCTION_ROLE, source="shop_owner_signal")
    return set_account_role(user, CANONICAL_PRODUCTION_ROLE)


def get_account_capabilities(user: User) -> dict[str, bool]:
    from accounts.services.capabilities import resolve_capabilities
    return resolve_capabilities(user)


def resolve_dashboard_role(user: User) -> str:
    """Backward-compatible alias for the primary dashboard role."""
    return resolve_primary_role(user)


def build_auth_role_payload(user: User) -> dict[str, object]:
    flags = role_flags_for_user(user)
    roles = resolve_user_roles(user)
    primary_role = resolve_primary_role(user)
    return {
        "roles": roles,
        "primary_role": primary_role,
        "dashboard_role": primary_role,
        "home_route": resolve_home_route(user),
        **flags,
    }


def is_super_admin(user: User) -> bool:
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and CANONICAL_SUPER_ADMIN_ROLE in resolve_user_roles(user)
    )


def user_can_manage_clients(user: User) -> bool:
    from accounts.services.capabilities import has_capability
    return has_capability(user, "can_manage_clients")


def user_can_source_jobs(user: User) -> bool:
    from accounts.services.capabilities import has_capability
    return has_capability(user, "can_source_jobs")


def user_can_receive_assignments(user: User) -> bool:
    from accounts.services.capabilities import has_capability
    return has_capability(user, "can_receive_assignments")


def user_can_manage_production(user: User) -> bool:
    from accounts.services.capabilities import has_capability
    return has_capability(user, "can_manage_production")


def user_can_receive_payouts(user: User) -> bool:
    from accounts.services.capabilities import has_capability
    return has_capability(user, "can_receive_payouts")
