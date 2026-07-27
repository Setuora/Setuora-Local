from fastapi import HTTPException, status

from app.models import Product, Role, User, has_any_role


PRODUCT_QR_MANAGER_ROLES = {Role.ADMIN, Role.SUPER_ADMIN}


def user_can_manage_purchase_qr_permission(user: User) -> bool:
    return has_any_role(user.role, PRODUCT_QR_MANAGER_ROLES)


def user_can_generate_product_qr(user: User, product: Product) -> bool:
    if user_can_manage_purchase_qr_permission(user):
        return True
    return bool(product.purchase_qr_print_allowed)


def require_product_qr_generation(user: User, product: Product) -> None:
    if not user_can_generate_product_qr(user, product):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Purchase QR printing is not enabled for this product",
        )
