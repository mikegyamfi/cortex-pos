from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from apps.core.models import BaseRetailModel, TimeStampedModel


class Category(BaseRetailModel):
    """
    Hierarchical category system (e.g., Electronics -> Laptops -> Gaming).
    """
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    parent = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='subcategories'
    )
    description = models.TextField(blank=True)
    icon = models.ImageField(upload_to='categories/icons/', null=True, blank=True)

    class Meta:
        verbose_name_plural = "Categories"

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            slug = base_slug
            counter = 1
            while Category.objects.filter(slug=slug).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def __str__(self):
        full_path = [self.name]
        k = self.parent
        while k is not None:
            full_path.append(k.name)
            k = k.parent
        return ' -> '.join(full_path[::-1])


class Brand(BaseRetailModel):
    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(unique=True)
    logo = models.ImageField(upload_to='brands/logos/', null=True, blank=True)
    website = models.URLField(blank=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Unit(models.Model):
    """
    Units of measurement (e.g., pcs, kg, box, liter).
    """
    name = models.CharField(max_length=50)  # e.g., Kilogram
    symbol = models.CharField(max_length=10)  # e.g., kg

    def __str__(self):
        return f"{self.name} ({self.symbol})"


class Product(BaseRetailModel):
    """
    The central product definition.
    Stock is NOT stored here; it is stored in the Inventory app.
    """
    name = models.CharField(max_length=255, db_index=True)
    slug = models.SlugField(unique=True, max_length=255)
    description = models.TextField(blank=True)

    # Classification
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, related_name='products')
    brand = models.ForeignKey(Brand, on_delete=models.SET_NULL, null=True, blank=True, related_name='products')
    unit = models.ForeignKey(Unit, on_delete=models.SET_NULL, null=True, blank=True)

    # Identifiers (Scanning & Internal)
    sku = models.CharField(max_length=100, unique=True, help_text="Internal Stock Keeping Unit")
    barcode = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="Scannable Barcode (UPC, EAN, etc.)"
    )

    # Pricing
    cost_price = models.DecimalField(max_digits=12, decimal_places=2, help_text="Average or last cost price")
    selling_price = models.DecimalField(max_digits=12, decimal_places=2, db_index=True)
    wholesale_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                          help_text="Discounted price for bulk buyers")

    # Tax & Settings
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00,
                                   help_text="Percentage tax applicable (e.g., 12.5)")
    is_returnable = models.BooleanField(default=True)
    is_perishable = models.BooleanField(default=False, help_text="Does this product have an expiry date?")

    # Inventory Alerts
    low_stock_threshold = models.PositiveIntegerField(default=10, help_text="Global alert level")

    # Physical Attributes (Comprehensive/Nullable)
    weight_kg = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    dimensions = models.CharField(max_length=100, blank=True, help_text="L x W x H")
    manufacturer_part_number = models.CharField(max_length=100, blank=True)
    shelf_location = models.CharField(max_length=100, blank=True, help_text="General shelf location hint")

    def save(self, *args, **kwargs):
        if not self.slug:
            # Ensure uniqueness by appending the already unique SKU
            self.slug = slugify(f"{self.name}-{self.sku}")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.sku})"


class ProductImage(TimeStampedModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='products/images/')
    is_primary = models.BooleanField(default=False)
    alt_text = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-is_primary', '-created_at']

    def __str__(self):
        return f"Image for {self.product.name}"


