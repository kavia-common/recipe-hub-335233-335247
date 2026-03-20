from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, HttpUrl


class HealthResponse(BaseModel):
    message: str = Field(..., description="Health status message")


class TokenResponse(BaseModel):
    access_token: str = Field(..., description="JWT access token")
    token_type: str = Field("bearer", description="Token type, always 'bearer'")


class UserPublic(BaseModel):
    id: int = Field(..., description="User ID")
    email: str = Field(..., description="User email")
    display_name: str = Field(..., description="Display name")
    is_admin: bool = Field(..., description="Whether the user is an admin")


class SignupRequest(BaseModel):
    email: str = Field(..., description="User email address")
    password: str = Field(..., min_length=8, description="User password (min 8 characters)")
    display_name: str = Field(..., min_length=1, max_length=100, description="Display name")


class LoginRequest(BaseModel):
    email: str = Field(..., description="User email address")
    password: str = Field(..., description="User password")


class RecipeBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=200, description="Recipe title")
    description: str = Field("", description="Short recipe description")
    cuisine: Optional[str] = Field(None, max_length=80, description="Cuisine (e.g., Italian)")
    diet: Optional[str] = Field(None, max_length=80, description="Diet tag (e.g., Vegan)")
    allergens: Optional[List[str]] = Field(None, description="List of allergens (e.g., ['nuts','dairy'])")
    prep_time_minutes: Optional[int] = Field(None, ge=0, description="Prep time in minutes")
    cook_time_minutes: Optional[int] = Field(None, ge=0, description="Cook time in minutes")
    servings: Optional[int] = Field(None, ge=1, description="Servings count")
    ingredients: List[str] = Field(..., min_length=1, description="Ingredient lines")
    steps: List[str] = Field(..., min_length=1, description="Preparation steps")
    image_url: Optional[HttpUrl] = Field(None, description="Public URL for recipe photo")


class RecipeCreateRequest(RecipeBase):
    pass


class RecipeUpdateRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    cuisine: Optional[str] = Field(None, max_length=80)
    diet: Optional[str] = Field(None, max_length=80)
    allergens: Optional[List[str]] = None
    prep_time_minutes: Optional[int] = Field(None, ge=0)
    cook_time_minutes: Optional[int] = Field(None, ge=0)
    servings: Optional[int] = Field(None, ge=1)
    ingredients: Optional[List[str]] = Field(None, min_length=1)
    steps: Optional[List[str]] = Field(None, min_length=1)
    image_url: Optional[HttpUrl] = None


class RecipePublic(BaseModel):
    id: int
    title: str
    description: str
    cuisine: Optional[str]
    diet: Optional[str]
    allergens: List[str]
    prep_time_minutes: Optional[int]
    cook_time_minutes: Optional[int]
    servings: Optional[int]
    ingredients: List[str]
    steps: List[str]
    image_url: Optional[str]

    is_user_submitted: bool
    status: str
    moderation_reason: Optional[str]
    author_id: Optional[int]

    created_at: datetime
    updated_at: datetime

    avg_rating: float = Field(..., description="Average rating for recipe")
    review_count: int = Field(..., description="Number of reviews")


class RecipeListResponse(BaseModel):
    items: List[RecipePublic]
    total: int
    page: int
    page_size: int


class FavoriteResponse(BaseModel):
    recipe_id: int
    created_at: datetime


class ShoppingListItemCreateRequest(BaseModel):
    ingredient: str = Field(..., min_length=1, max_length=300, description="Ingredient name or line item")
    quantity: Optional[str] = Field(None, max_length=100, description="Optional quantity, e.g. '2 cups'")
    recipe_id: Optional[int] = Field(None, description="Optional originating recipe id")


class ShoppingListItemUpdateRequest(BaseModel):
    ingredient: Optional[str] = Field(None, min_length=1, max_length=300)
    quantity: Optional[str] = Field(None, max_length=100)
    checked: Optional[bool] = None


class ShoppingListItemPublic(BaseModel):
    id: int
    ingredient: str
    quantity: Optional[str]
    recipe_id: Optional[int]
    checked: bool
    created_at: datetime


class ReviewCreateRequest(BaseModel):
    rating: int = Field(..., ge=1, le=5, description="Rating from 1 to 5")
    comment: Optional[str] = Field(None, description="Optional review comment")


class ReviewPublic(BaseModel):
    id: int
    recipe_id: int
    user_id: int
    rating: int
    comment: Optional[str]
    created_at: datetime
    updated_at: datetime


class ReviewListResponse(BaseModel):
    items: List[ReviewPublic]
    total: int


class ModerationUpdateRequest(BaseModel):
    status: str = Field(..., description="New status: pending, approved, rejected")
    moderation_reason: Optional[str] = Field(None, description="Optional reason shown to author")
