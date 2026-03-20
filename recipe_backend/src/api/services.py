from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.models import Favorite, Recipe, Review, ShoppingListItem, User


def _splitlines(items: List[str]) -> str:
    return "\n".join([i.strip() for i in items if i.strip()])


def _csv(items: Optional[List[str]]) -> Optional[str]:
    if items is None:
        return None
    cleaned = [i.strip().lower() for i in items if i and i.strip()]
    return ",".join(cleaned) if cleaned else None


def _parse_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass(frozen=True)
class RecipeSearchRequest:
    """Search/filter request.

    Contract:
      - q: free-text, matched against title/description
      - cuisine/diet: exact match (case-insensitive stored)
      - exclude_allergens: recipes containing any of these allergens will be excluded
      - include_allergens: recipes must contain all of these allergens (rare; provided for completeness)
      - status: defaults to 'approved' for public browsing
    """

    q: Optional[str]
    cuisine: Optional[str]
    diet: Optional[str]
    exclude_allergens: Optional[List[str]]
    include_allergens: Optional[List[str]]
    status: str
    page: int
    page_size: int


# PUBLIC_INTERFACE
def recipe_search_flow(db: Session, req: RecipeSearchRequest) -> Tuple[List[Recipe], int, dict]:
    """Search/browse recipes (canonical flow).

    Returns:
      (recipes, total_count, aggregates_by_recipe_id)

    aggregates_by_recipe_id maps recipe_id -> {"avg_rating": float, "review_count": int}
    """
    if req.page < 1 or req.page_size < 1 or req.page_size > 100:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid pagination")

    query = db.query(Recipe).filter(Recipe.status == req.status)

    if req.q:
        q = f"%{req.q.lower()}%"
        query = query.filter(
            or_(
                func.lower(Recipe.title).like(q),
                func.lower(Recipe.description).like(q),
            )
        )

    if req.cuisine:
        query = query.filter(func.lower(Recipe.cuisine) == req.cuisine.lower())
    if req.diet:
        query = query.filter(func.lower(Recipe.diet) == req.diet.lower())

    # allergen matching is stored as comma-separated lowercased values
    if req.exclude_allergens:
        for a in req.exclude_allergens:
            a_l = a.strip().lower()
            if a_l:
                query = query.filter(or_(Recipe.allergens.is_(None), func.position(a_l, Recipe.allergens) == 0))

    if req.include_allergens:
        for a in req.include_allergens:
            a_l = a.strip().lower()
            if a_l:
                query = query.filter(and_(Recipe.allergens.is_not(None), func.position(a_l, Recipe.allergens) > 0))

    total = query.count()
    items = (
        query.order_by(Recipe.created_at.desc())
        .offset((req.page - 1) * req.page_size)
        .limit(req.page_size)
        .all()
    )

    recipe_ids = [r.id for r in items]
    aggregates = {}
    if recipe_ids:
        rows = (
            db.query(
                Review.recipe_id.label("recipe_id"),
                func.avg(Review.rating).label("avg_rating"),
                func.count(Review.id).label("review_count"),
            )
            .filter(Review.recipe_id.in_(recipe_ids))
            .group_by(Review.recipe_id)
            .all()
        )
        for row in rows:
            aggregates[int(row.recipe_id)] = {
                "avg_rating": float(row.avg_rating or 0.0),
                "review_count": int(row.review_count or 0),
            }

    return items, total, aggregates


# PUBLIC_INTERFACE
def recipe_get_detail_flow(db: Session, recipe_id: int, *, include_unapproved: bool = False) -> Tuple[Recipe, float, int]:
    """Fetch recipe detail with rating aggregates."""
    recipe = db.query(Recipe).filter(Recipe.id == recipe_id).one_or_none()
    if recipe is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recipe not found")

    if not include_unapproved and recipe.status != "approved":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recipe not found")

    agg = (
        db.query(func.avg(Review.rating), func.count(Review.id))
        .filter(Review.recipe_id == recipe_id)
        .one()
    )
    avg = float(agg[0] or 0.0)
    count = int(agg[1] or 0)
    return recipe, avg, count


# PUBLIC_INTERFACE
def recipe_create_flow(db: Session, *, author: Optional[User], data: dict, user_submitted: bool) -> Recipe:
    """Create recipe (canonical flow)."""
    now = datetime.utcnow()
    recipe = Recipe(
        title=data["title"].strip(),
        description=(data.get("description") or "").strip(),
        cuisine=(data.get("cuisine") or None),
        diet=(data.get("diet") or None),
        allergens=_csv(data.get("allergens")),
        prep_time_minutes=data.get("prep_time_minutes"),
        cook_time_minutes=data.get("cook_time_minutes"),
        servings=data.get("servings"),
        ingredients=_splitlines(data["ingredients"]),
        steps=_splitlines(data["steps"]),
        image_url=(str(data["image_url"]) if data.get("image_url") else None),
        is_user_submitted=bool(user_submitted),
        status="pending" if user_submitted else "approved",
        author_id=author.id if author else None,
        created_at=now,
        updated_at=now,
    )
    db.add(recipe)
    db.commit()
    db.refresh(recipe)
    return recipe


# PUBLIC_INTERFACE
def recipe_update_flow(db: Session, *, recipe_id: int, actor: User, data: dict) -> Recipe:
    """Update recipe (admin OR author-only if user submitted)."""
    recipe = db.query(Recipe).filter(Recipe.id == recipe_id).one_or_none()
    if recipe is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recipe not found")

    if not actor.is_admin:
        if recipe.author_id != actor.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
        if recipe.status != "pending":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only pending submissions can be edited")

    for field in [
        "title",
        "description",
        "cuisine",
        "diet",
        "prep_time_minutes",
        "cook_time_minutes",
        "servings",
    ]:
        if field in data and data[field] is not None:
            setattr(recipe, field, data[field].strip() if isinstance(data[field], str) else data[field])

    if "allergens" in data and data["allergens"] is not None:
        recipe.allergens = _csv(data["allergens"])

    if "ingredients" in data and data["ingredients"] is not None:
        recipe.ingredients = _splitlines(data["ingredients"])

    if "steps" in data and data["steps"] is not None:
        recipe.steps = _splitlines(data["steps"])

    if "image_url" in data and data["image_url"] is not None:
        recipe.image_url = str(data["image_url"])

    recipe.updated_at = datetime.utcnow()
    db.add(recipe)
    db.commit()
    db.refresh(recipe)
    return recipe


# PUBLIC_INTERFACE
def favorites_add_flow(db: Session, *, user: User, recipe_id: int) -> Favorite:
    """Add a recipe to user's favorites."""
    # Ensure recipe exists and is approved
    recipe = db.query(Recipe).filter(Recipe.id == recipe_id, Recipe.status == "approved").one_or_none()
    if recipe is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recipe not found")

    fav = Favorite(user_id=user.id, recipe_id=recipe_id)
    db.add(fav)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Treat as idempotent
        fav = db.query(Favorite).filter(Favorite.user_id == user.id, Favorite.recipe_id == recipe_id).one()
    db.refresh(fav)
    return fav


# PUBLIC_INTERFACE
def favorites_remove_flow(db: Session, *, user: User, recipe_id: int) -> None:
    """Remove a recipe from favorites (idempotent)."""
    deleted = db.query(Favorite).filter(Favorite.user_id == user.id, Favorite.recipe_id == recipe_id).delete()
    if deleted:
        db.commit()
    else:
        db.rollback()


# PUBLIC_INTERFACE
def shopping_list_add_item_flow(db: Session, *, user: User, ingredient: str, quantity: Optional[str], recipe_id: Optional[int]) -> ShoppingListItem:
    """Add a shopping list item."""
    item = ShoppingListItem(
        user_id=user.id,
        ingredient=ingredient.strip(),
        quantity=quantity.strip() if quantity else None,
        recipe_id=recipe_id,
        checked=False,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


# PUBLIC_INTERFACE
def shopping_list_update_item_flow(db: Session, *, user: User, item_id: int, data: dict) -> ShoppingListItem:
    """Update a shopping list item."""
    item = db.query(ShoppingListItem).filter(ShoppingListItem.id == item_id, ShoppingListItem.user_id == user.id).one_or_none()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")

    if "ingredient" in data and data["ingredient"] is not None:
        item.ingredient = data["ingredient"].strip()
    if "quantity" in data and data["quantity"] is not None:
        item.quantity = data["quantity"].strip()
    if "checked" in data and data["checked"] is not None:
        item.checked = bool(data["checked"])

    db.add(item)
    db.commit()
    db.refresh(item)
    return item


# PUBLIC_INTERFACE
def shopping_list_delete_item_flow(db: Session, *, user: User, item_id: int) -> None:
    """Delete a shopping list item (idempotent)."""
    deleted = db.query(ShoppingListItem).filter(ShoppingListItem.id == item_id, ShoppingListItem.user_id == user.id).delete()
    if deleted:
        db.commit()
    else:
        db.rollback()


# PUBLIC_INTERFACE
def reviews_add_or_update_flow(db: Session, *, user: User, recipe_id: int, rating: int, comment: Optional[str]) -> Review:
    """Create or update user's review for a recipe."""
    recipe = db.query(Recipe).filter(Recipe.id == recipe_id, Recipe.status == "approved").one_or_none()
    if recipe is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recipe not found")

    existing = db.query(Review).filter(Review.recipe_id == recipe_id, Review.user_id == user.id).one_or_none()
    now = datetime.utcnow()
    if existing:
        existing.rating = rating
        existing.comment = comment
        existing.updated_at = now
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return existing

    review = Review(recipe_id=recipe_id, user_id=user.id, rating=rating, comment=comment, created_at=now, updated_at=now)
    db.add(review)
    db.commit()
    db.refresh(review)
    return review


# PUBLIC_INTERFACE
def reviews_list_flow(db: Session, *, recipe_id: int, page: int, page_size: int) -> Tuple[List[Review], int]:
    """List reviews for a recipe."""
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid pagination")

    q = db.query(Review).filter(Review.recipe_id == recipe_id).order_by(Review.created_at.desc())
    total = q.count()
    items = q.offset((page - 1) * page_size).limit(page_size).all()
    return items, total


# PUBLIC_INTERFACE
def admin_moderate_recipe_flow(db: Session, *, recipe_id: int, status_value: str, moderation_reason: Optional[str]) -> Recipe:
    """Admin moderation flow for submissions."""
    if status_value not in {"pending", "approved", "rejected"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid status")

    recipe = db.query(Recipe).filter(Recipe.id == recipe_id).one_or_none()
    if recipe is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recipe not found")

    recipe.status = status_value
    recipe.moderation_reason = moderation_reason.strip() if moderation_reason else None
    recipe.updated_at = datetime.utcnow()
    db.add(recipe)
    db.commit()
    db.refresh(recipe)
    return recipe
