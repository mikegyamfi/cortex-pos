from django import forms
from .models import Product, Category, Brand, Unit, ProductImage


class ProductForm(forms.ModelForm):
    """
    The Master Form for creating/editing products.
    Includes support for the essential Barcode/SKU fields.
    """

    class Meta:
        model = Product
        fields = [
            'location',
            'name', 'category', 'brand', 'unit',
            'sku', 'barcode',
            'cost_price', 'selling_price', 'wholesale_price',
            'tax_rate', 'low_stock_threshold',
            'is_perishable', 'is_returnable',
            'weight_kg', 'dimensions', 'shelf_location',
            'description'
        ]
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
            'category': forms.Select(attrs={'class': 'select2 form-control'}),
            'brand': forms.Select(attrs={'class': 'select2'}),
            'unit': forms.Select(attrs={'class': 'select2'}),
        }

    def clean_barcode(self):
        barcode = self.cleaned_data.get('barcode')
        if barcode:
            # Barcodes are unique PER SHOP, so scope the check to this product's
            # shop (set on the instance before validation, or chosen on the form).
            loc = self.cleaned_data.get('location') or getattr(self.instance, 'location', None)
            qs = Product.objects.filter(barcode=barcode, location=loc).exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError("This barcode is already used in this shop.")
        return barcode


class ProductImageForm(forms.ModelForm):
    class Meta:
        model = ProductImage
        fields = ['image', 'is_primary']


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['name', 'parent', 'description']


class BrandForm(forms.ModelForm):
    class Meta:
        model = Brand
        fields = ['name', 'website', 'logo']
