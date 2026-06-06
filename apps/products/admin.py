from django import forms
from django.contrib import admin
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.shortcuts import render

from apps.location.models import Location
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


class AssignShopForm(forms.Form):
    """Intermediate form: pick the shop to bind the selected products to."""
    location = forms.ModelChoiceField(
        queryset=Location.objects.filter(is_active=True),
        label="Shop / Location",
    )


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    inlines = [ProductImageInline]
    list_display = ('name', 'sku', 'barcode', 'location', 'category', 'selling_price', 'is_active')
    list_filter = ('location', 'category', 'brand', 'is_active', 'is_perishable')
    search_fields = ('name', 'sku', 'barcode', 'description')
    list_editable = ('selling_price', 'is_active')
    list_select_related = ('location', 'category')
    actions = ['assign_to_shop']

    @admin.action(description="Bind selected products to a shop…")
    def assign_to_shop(self, request, queryset):
        # Step 2: the intermediate form was submitted -> apply.
        if request.POST.get('apply'):
            form = AssignShopForm(request.POST)
            if form.is_valid():
                location = form.cleaned_data['location']
                updated = queryset.update(location=location)
                self.message_user(request, f"Bound {updated} product(s) to {location.name}.")
                return None  # back to the changelist

        # Step 1: show the intermediate page listing the chosen products.
        form = AssignShopForm()
        return render(request, 'admin/products/assign_shop.html', {
            **self.admin_site.each_context(request),
            'title': 'Bind products to a shop',
            'products': queryset,
            'form': form,
            'action_checkbox_name': ACTION_CHECKBOX_NAME,
            'selected': queryset.values_list('pk', flat=True),
            'opts': self.model._meta,
        })
