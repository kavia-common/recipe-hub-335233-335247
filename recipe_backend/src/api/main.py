from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import Depends, FastAPI, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.api import schemas
from src.api.auth import create_access_token, get_current_user, hash_password, require_admin, verify_password
from src.api.db import get_db, get_engine, init_engine, load_database_config
from src.api.models import Base, Favorite, Recipe, Review, ShoppingListItem, User
from src.api.services import (
    RecipeSearchRequest,
    admin_moderate_recipe_flow,
    favorites_add_flow,
    favorites_remove_flow,
    recipe_create_flow,
    recipe_get_detail_flow,
    recipe_search_flow,
    recipe_update_flow,
    reviews_add_or_update_flow,
    reviews_list_flow,
    shopping_list_add_item_flow,
    shopping_list_delete_item_flow,
    shopping_list_update_item_flow,
)

logger = logging.getLogger("recipe_backend")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

openapi_tags = [
    {"name": "Health", "description": "Service health and diagnostics."},
    {"name": "Auth", "description": "Signup/login and user identity."},
    {"name": "Recipes", "description": "Browse/search/filter and recipe details."},
    {"name": "Favorites", "description": "User favorites management."},
    {"name": "ShoppingList", "description": "Shopping list CRUD for the current user."},
    {"name": "Submissions", "description": "User-submitted recipes (create/edit pending)."},
    {"name": "Reviews", "description": "Ratings and reviews for recipes."},
    {"name": "Admin", "description": "Admin moderation for user-submitted content."},
]

app = FastAPI(
    title="Recipe Hub Backend API",
    description=(
        "Backend for Recipe Hub: authentication, recipes browse/search/filter, recipe detail, "
        "favorites, shopping list, submissions, reviews/ratings, and admin moderation."
    ),
    version="1.0.0",
    openapi_tags=openapi_tags,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # frontend origin can be restricted via env later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _on_startup() -> None:
    """Initialize DB engine and create tables."""
    cfg = load_database_config()
    init_engine(cfg.db_url)
    # Create tables if missing (simple bootstrap; for production use migrations).
    Base.metadata.create_all(bind=get_engine())
    logger.info("Startup complete: database initialized.")


def _recipe_to_public(recipe: Recipe, avg_rating: float, review_count: int) -> schemas.RecipePublic:
    return schemas.RecipePublic(
        id=recipe.id,
        title=recipe.title,
        description=recipe.description,
        cuisine=recipe.cuisine,
        diet=recipe.diet,
        allergens=(recipe.allergens.split(",") if recipe.allergens else []),
        prep_time_minutes=recipe.prep_time_minutes,
        cook_time_minutes=recipe.cook_time_minutes,
        servings=recipe.servings,
        ingredients=[x for x in recipe.ingredients.split("\n") if x.strip()],
        steps=[x for x in recipe.steps.split("\n") if x.strip()],
        image_url=recipe.image_url,
        is_user_submitted=recipe.is_user_submitted,
        status=recipe.status,
        moderation_reason=recipe.moderation_reason,
        author_id=recipe.author_id,
        created_at=recipe.created_at,
        updated_at=recipe.updated_at,
        avg_rating=avg_rating,
        review_count=review_count,
    )


@app.get("/", response_model=schemas.HealthResponse, tags=["Health"], summary="Health check")
def health_check() -> schemas.HealthResponse:
    """Health check endpoint."""
    return schemas.HealthResponse(message="Healthy")


# PUBLIC_INTERFACE
@app.post(
    "/auth/signup",
    response_model=schemas.TokenResponse,
    tags=["Auth"],
    summary="Signup",
    description="Create a new user account and return an access token.",
)
def signup(payload: schemas.SignupRequest, db: Session = Depends(get_db)) -> schemas.TokenResponse:
    """Signup a user.

    Errors:
      - 409 if email already exists
    """
    existing = db.query(User).filter(User.email == payload.email.lower().strip()).one_or_none()
    if existing is not None:
        return Response(status_code=status.HTTP_409_CONFLICT, content="Email already registered")

    user = User(
        email=payload.email.lower().strip(),
        password_hash=hash_password(payload.password),
        display_name=payload.display_name.strip(),
        is_admin=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token(subject=user.email, user_id=user.id, is_admin=user.is_admin)
    return schemas.TokenResponse(access_token=token)


# PUBLIC_INTERFACE
@app.post(
    "/auth/login",
    response_model=schemas.TokenResponse,
    tags=["Auth"],
    summary="Login",
    description="Login with email/password and return an access token.",
)
def login(payload: schemas.LoginRequest, db: Session = Depends(get_db)) -> schemas.TokenResponse:
    """Login a user."""
    user = db.query(User).filter(User.email == payload.email.lower().strip()).one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        return Response(status_code=status.HTTP_401_UNAUTHORIZED, content="Invalid credentials")

    token = create_access_token(subject=user.email, user_id=user.id, is_admin=user.is_admin)
    return schemas.TokenResponse(access_token=token)


# PUBLIC_INTERFACE
@app.get(
    "/me",
    response_model=schemas.UserPublic,
    tags=["Auth"],
    summary="Get current user",
)
def me(user: User = Depends(get_current_user)) -> schemas.UserPublic:
    """Return the current authenticated user's public profile."""
    return schemas.UserPublic(id=user.id, email=user.email, display_name=user.display_name, is_admin=user.is_admin)


# PUBLIC_INTERFACE
@app.get(
    "/recipes",
    response_model=schemas.RecipeListResponse,
    tags=["Recipes"],
    summary="Browse/search recipes",
    description="Browse approved recipes with optional search, cuisine/diet filters, and allergen exclusion.",
)
def list_recipes(
    q: Optional[str] = Query(None, description="Free-text search against title/description"),
    cuisine: Optional[str] = Query(None, description="Cuisine filter"),
    diet: Optional[str] = Query(None, description="Diet filter"),
    exclude_allergens: Optional[str] = Query(None, description="Comma-separated allergens to exclude (any match excludes)"),
    include_allergens: Optional[str] = Query(None, description="Comma-separated allergens that must be present (all)"),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(20, ge=1, le=100, description="Page size (max 100)"),
    db: Session = Depends(get_db),
) -> schemas.RecipeListResponse:
    """List recipes."""
    req = RecipeSearchRequest(
        q=q,
        cuisine=cuisine,
        diet=diet,
        exclude_allergens=[a.strip() for a in (exclude_allergens or "").split(",") if a.strip()] or None,
        include_allergens=[a.strip() for a in (include_allergens or "").split(",") if a.strip()] or None,
        status="approved",
        page=page,
        page_size=page_size,
    )
    items, total, aggregates = recipe_search_flow(db, req)
    out = []
    for r in items:
        agg = aggregates.get(r.id, {"avg_rating": 0.0, "review_count": 0})
        out.append(_recipe_to_public(r, float(agg["avg_rating"]), int(agg["review_count"])))
    return schemas.RecipeListResponse(items=out, total=total, page=page, page_size=page_size)


# PUBLIC_INTERFACE
@app.get(
    "/recipes/{recipe_id}",
    response_model=schemas.RecipePublic,
    tags=["Recipes"],
    summary="Get recipe detail",
)
def get_recipe(recipe_id: int, db: Session = Depends(get_db)) -> schemas.RecipePublic:
    """Get recipe detail (approved only)."""
    recipe, avg, count = recipe_get_detail_flow(db, recipe_id, include_unapproved=False)
    return _recipe_to_public(recipe, avg, count)


# PUBLIC_INTERFACE
@app.post(
    "/submissions",
    response_model=schemas.RecipePublic,
    tags=["Submissions"],
    summary="Submit a recipe",
    description="Submit a new recipe for moderation. Created with status=pending.",
    status_code=status.HTTP_201_CREATED,
)
def submit_recipe(
    payload: schemas.RecipeCreateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.RecipePublic:
    """Submit a recipe (pending)."""
    recipe = recipe_create_flow(
        db,
        author=user,
        data=payload.model_dump(),
        user_submitted=True,
    )
    # Unapproved submission returns but with 0 rating.
    return _recipe_to_public(recipe, 0.0, 0)


# PUBLIC_INTERFACE
@app.patch(
    "/submissions/{recipe_id}",
    response_model=schemas.RecipePublic,
    tags=["Submissions"],
    summary="Edit a pending submission",
)
def edit_submission(
    recipe_id: int,
    payload: schemas.RecipeUpdateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.RecipePublic:
    """Edit a pending submission (author or admin)."""
    recipe = recipe_update_flow(db, recipe_id=recipe_id, actor=user, data=payload.model_dump(exclude_unset=True))
    # Submission might still be pending, so don't enforce approved here.
    recipe_detail, avg, count = recipe_get_detail_flow(db, recipe.id, include_unapproved=True)
    return _recipe_to_public(recipe_detail, avg, count)


# PUBLIC_INTERFACE
@app.post(
    "/favorites/{recipe_id}",
    response_model=schemas.FavoriteResponse,
    tags=["Favorites"],
    summary="Add favorite",
)
def add_favorite(recipe_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> schemas.FavoriteResponse:
    """Add a recipe to favorites."""
    fav = favorites_add_flow(db, user=user, recipe_id=recipe_id)
    return schemas.FavoriteResponse(recipe_id=fav.recipe_id, created_at=fav.created_at)


# PUBLIC_INTERFACE
@app.delete(
    "/favorites/{recipe_id}",
    tags=["Favorites"],
    summary="Remove favorite",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_favorite(recipe_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    """Remove a recipe from favorites (idempotent)."""
    favorites_remove_flow(db, user=user, recipe_id=recipe_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# PUBLIC_INTERFACE
@app.get(
    "/favorites",
    response_model=list[schemas.RecipePublic],
    tags=["Favorites"],
    summary="List favorites",
)
def list_favorites(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[schemas.RecipePublic]:
    """List user's favorite recipes."""
    rows = (
        db.query(Recipe)
        .join(Favorite, Favorite.recipe_id == Recipe.id)
        .filter(Favorite.user_id == user.id, Recipe.status == "approved")
        .order_by(Favorite.created_at.desc())
        .all()
    )
    # aggregates in batch
    ids = [r.id for r in rows]
    aggregates = {}
    if ids:
        agg_rows = (
            db.query(Review.recipe_id, func.avg(Review.rating), func.count(Review.id))
            .filter(Review.recipe_id.in_(ids))
            .group_by(Review.recipe_id)
            .all()
        )
        for rid, avg, cnt in agg_rows:
            aggregates[int(rid)] = {"avg_rating": float(avg or 0.0), "review_count": int(cnt or 0)}

    out: list[schemas.RecipePublic] = []
    for r in rows:
        agg = aggregates.get(r.id, {"avg_rating": 0.0, "review_count": 0})
        out.append(_recipe_to_public(r, float(agg["avg_rating"]), int(agg["review_count"])))
    return out


# PUBLIC_INTERFACE
@app.get(
    "/shopping-list",
    response_model=list[schemas.ShoppingListItemPublic],
    tags=["ShoppingList"],
    summary="List shopping list items",
)
def shopping_list_list(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[schemas.ShoppingListItemPublic]:
    """List current user's shopping list."""
    items = (
        db.query(ShoppingListItem)
        .filter(ShoppingListItem.user_id == user.id)
        .order_by(ShoppingListItem.checked.asc(), ShoppingListItem.created_at.desc())
        .all()
    )
    return [
        schemas.ShoppingListItemPublic(
            id=i.id,
            ingredient=i.ingredient,
            quantity=i.quantity,
            recipe_id=i.recipe_id,
            checked=i.checked,
            created_at=i.created_at,
        )
        for i in items
    ]


# PUBLIC_INTERFACE
@app.post(
    "/shopping-list",
    response_model=schemas.ShoppingListItemPublic,
    tags=["ShoppingList"],
    summary="Add shopping list item",
    status_code=status.HTTP_201_CREATED,
)
def shopping_list_add(
    payload: schemas.ShoppingListItemCreateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.ShoppingListItemPublic:
    """Add a shopping list item."""
    item = shopping_list_add_item_flow(
        db,
        user=user,
        ingredient=payload.ingredient,
        quantity=payload.quantity,
        recipe_id=payload.recipe_id,
    )
    return schemas.ShoppingListItemPublic(
        id=item.id,
        ingredient=item.ingredient,
        quantity=item.quantity,
        recipe_id=item.recipe_id,
        checked=item.checked,
        created_at=item.created_at,
    )


# PUBLIC_INTERFACE
@app.patch(
    "/shopping-list/{item_id}",
    response_model=schemas.ShoppingListItemPublic,
    tags=["ShoppingList"],
    summary="Update shopping list item",
)
def shopping_list_update(
    item_id: int,
    payload: schemas.ShoppingListItemUpdateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.ShoppingListItemPublic:
    """Update shopping list item."""
    item = shopping_list_update_item_flow(db, user=user, item_id=item_id, data=payload.model_dump(exclude_unset=True))
    return schemas.ShoppingListItemPublic(
        id=item.id,
        ingredient=item.ingredient,
        quantity=item.quantity,
        recipe_id=item.recipe_id,
        checked=item.checked,
        created_at=item.created_at,
    )


# PUBLIC_INTERFACE
@app.delete(
    "/shopping-list/{item_id}",
    tags=["ShoppingList"],
    summary="Delete shopping list item",
    status_code=status.HTTP_204_NO_CONTENT,
)
def shopping_list_delete(item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    """Delete shopping list item (idempotent)."""
    shopping_list_delete_item_flow(db, user=user, item_id=item_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# PUBLIC_INTERFACE
@app.post(
    "/recipes/{recipe_id}/reviews",
    response_model=schemas.ReviewPublic,
    tags=["Reviews"],
    summary="Create or update review",
)
def add_or_update_review(
    recipe_id: int,
    payload: schemas.ReviewCreateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.ReviewPublic:
    """Create or update the current user's review for a recipe."""
    review = reviews_add_or_update_flow(db, user=user, recipe_id=recipe_id, rating=payload.rating, comment=payload.comment)
    return schemas.ReviewPublic(
        id=review.id,
        recipe_id=review.recipe_id,
        user_id=review.user_id,
        rating=review.rating,
        comment=review.comment,
        created_at=review.created_at,
        updated_at=review.updated_at,
    )


# PUBLIC_INTERFACE
@app.get(
    "/recipes/{recipe_id}/reviews",
    response_model=schemas.ReviewListResponse,
    tags=["Reviews"],
    summary="List reviews",
)
def list_reviews(
    recipe_id: int,
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Page size"),
    db: Session = Depends(get_db),
) -> schemas.ReviewListResponse:
    """List reviews for a recipe."""
    items, total = reviews_list_flow(db, recipe_id=recipe_id, page=page, page_size=page_size)
    out = [
        schemas.ReviewPublic(
            id=i.id,
            recipe_id=i.recipe_id,
            user_id=i.user_id,
            rating=i.rating,
            comment=i.comment,
            created_at=i.created_at,
            updated_at=i.updated_at,
        )
        for i in items
    ]
    return schemas.ReviewListResponse(items=out, total=total)


# PUBLIC_INTERFACE
@app.get(
    "/admin/submissions",
    response_model=schemas.RecipeListResponse,
    tags=["Admin"],
    summary="List submissions (admin)",
    description="List all user-submitted recipes with optional status filter.",
)
def admin_list_submissions(
    status_filter: Optional[str] = Query(None, description="Filter by status: pending/approved/rejected"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> schemas.RecipeListResponse:
    """Admin: list submissions."""
    q = db.query(Recipe).filter(Recipe.is_user_submitted.is_(True))
    if status_filter:
        q = q.filter(Recipe.status == status_filter)
    total = q.count()
    items = q.order_by(Recipe.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()

    ids = [r.id for r in items]
    aggregates = {}
    if ids:
        agg_rows = (
            db.query(Review.recipe_id, func.avg(Review.rating), func.count(Review.id))
            .filter(Review.recipe_id.in_(ids))
            .group_by(Review.recipe_id)
            .all()
        )
        for rid, avg, cnt in agg_rows:
            aggregates[int(rid)] = {"avg_rating": float(avg or 0.0), "review_count": int(cnt or 0)}

    out = []
    for r in items:
        agg = aggregates.get(r.id, {"avg_rating": 0.0, "review_count": 0})
        out.append(_recipe_to_public(r, float(agg["avg_rating"]), int(agg["review_count"])))
    return schemas.RecipeListResponse(items=out, total=total, page=page, page_size=page_size)


# PUBLIC_INTERFACE
@app.patch(
    "/admin/recipes/{recipe_id}/moderate",
    response_model=schemas.RecipePublic,
    tags=["Admin"],
    summary="Moderate a recipe (admin)",
    description="Approve/reject a user-submitted recipe with an optional reason.",
)
def admin_moderate(
    recipe_id: int,
    payload: schemas.ModerationUpdateRequest,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> schemas.RecipePublic:
    """Admin: moderate a recipe."""
    recipe = admin_moderate_recipe_flow(db, recipe_id=recipe_id, status_value=payload.status, moderation_reason=payload.moderation_reason)
    recipe_detail, avg, count = recipe_get_detail_flow(db, recipe.id, include_unapproved=True)
    return _recipe_to_public(recipe_detail, avg, count)
