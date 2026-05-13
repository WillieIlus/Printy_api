"""Reusable role checks for permissions and services."""

from accounts.models import User
from accounts.services.capabilities import has_capability, resolve_capabilities


def has_role(user: User, *roles: str) -> bool:
    return bool(user and user.is_authenticated and user.role in roles)


def is_client(user: User) -> bool:
    return has_role(user, User.Role.CLIENT)


def is_broker(user: User) -> bool:
    return has_role(user, User.Role.BROKER)


def is_shop_owner(user: User) -> bool:
    return has_role(user, User.Role.SHOP_OWNER)


def is_staff_member(user: User) -> bool:
    return has_role(user, User.Role.STAFF)


def is_platform_staff(user: User) -> bool:
    return bool(user and user.is_authenticated and user.is_staff)


def get_assignable_roles():
    return {User.Role.CLIENT, User.Role.BROKER, User.Role.SHOP_OWNER, User.Role.STAFF}


def set_account_role(user: User, role: str) -> User:
    if role not in get_assignable_roles():
        raise ValueError(f"Unsupported role: {role}")
    if user.role != role:
        user.role = role
        user.save(update_fields=["role", "updated_at"])
    return user


def promote_to_shop_owner(user: User) -> User:
    return set_account_role(user, User.Role.SHOP_OWNER)


def get_account_capabilities(user: User) -> dict[str, bool]:
    return resolve_capabilities(user)


def user_can_manage_clients(user: User) -> bool:
    return has_capability(user, "can_manage_clients")


def user_can_source_jobs(user: User) -> bool:
    return has_capability(user, "can_source_jobs")


def user_can_receive_assignments(user: User) -> bool:
    return has_capability(user, "can_receive_assignments")


def user_can_manage_production(user: User) -> bool:
    return has_capability(user, "can_manage_production")


def user_can_receive_payouts(user: User) -> bool:
    return has_capability(user, "can_receive_payouts")
