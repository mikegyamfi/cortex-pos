from django.contrib import admin
from .models import Category, Brand, Unit, Product, ProductImage


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'parent', 'slug')
    search_fields = ('name',)
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ('name', 'website')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ('name', 'symbol')


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    inlines = [ProductImageInline]
    list_display = ('name', 'sku', 'barcode', 'category', 'brand', 'selling_price', 'is_active')
    list_filter = ('category', 'brand', 'is_active', 'is_perishable')
    search_fields = ('name', 'sku', 'barcode', 'description')
    prepopulated_fields = {'slug': ('name',)}
    list_editable = ('selling_price', 'is_active')
