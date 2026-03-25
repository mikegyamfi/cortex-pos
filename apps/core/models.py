import uuid
from django.db import models


class TimeStampedModel(models.Model):
    """
    An abstract base class model that provides self-updating
    'created_at' and 'updated_at' fields.
    """
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class UUIDModel(models.Model):
    """
    An abstract base class model that provides a UUID primary key.
    Useful for public-facing IDs to prevent ID enumeration.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class ActivatableModel(models.Model):
    """
    An abstract base class model that provides an 'is_active' field.
    """
    is_active = models.BooleanField(default=True)

    class Meta:
        abstract = True


class BaseRetailModel(TimeStampedModel, ActivatableModel):
    """
    A combined base model for most retail entities.
    """

    class Meta:
        abstract = True
