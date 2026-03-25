from django import forms
from .models import StockBatch, StockTransfer, StockTransferItem, StockAdjustment
from apps.location.models import Location
from apps.products.models import Product


class StockReceiveForm(forms.ModelForm):
    class Meta:
        model = StockBatch
        # Added 'location' to fields so it can be handled by the form
        fields = ['location', 'product', 'supplier', 'batch_number', 'quantity', 'cost_price', 'expiry_date',
                  'manufactured_date']
        widgets = {
            'expiry_date': forms.DateInput(attrs={'type': 'date'}),
            'manufactured_date': forms.DateInput(attrs={'type': 'date'}),
            'product': forms.Select(attrs={'class': 'select2'}),
            'supplier': forms.Select(attrs={'class': 'select2'}),
            'location': forms.Select(attrs={'class': 'select2'}),
        }

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # OWNER LOGIC: Can select any location
        if user.role == 'OWNER':
            self.fields['location'].queryset = Location.objects.filter(is_active=True)
            self.fields['location'].required = True
            self.fields['location'].label = "Receive Into Location"
        else:
            # STAFF LOGIC: Location is hidden and fixed
            # We remove the field from the visible form, it will be handled in the view
            if 'location' in self.fields:
                del self.fields['location']


class StockTransferForm(forms.ModelForm):
    class Meta:
        model = StockTransfer
        fields = ['source_location', 'destination_location', 'driver_name', 'vehicle_number', 'estimated_arrival',
                  'notes']
        widgets = {
            'estimated_arrival': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        }

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # OWNER LOGIC: Can control Source AND Destination
        if user.role == 'OWNER':
            self.fields['source_location'].queryset = Location.objects.filter(is_active=True)
            self.fields['destination_location'].queryset = Location.objects.filter(is_active=True)
        else:
            # STAFF LOGIC: Source is always their location (Hidden)
            if 'source_location' in self.fields:
                del self.fields['source_location']

            # Filter Destination to not be their own location
            if user.assigned_location:
                self.fields['destination_location'].queryset = Location.objects.exclude(id=user.assigned_location.id)


class StockTransferItemForm(forms.ModelForm):
    product = forms.ModelChoiceField(
        queryset=Product.objects.all(),
        widget=forms.Select(attrs={'class': 'product-select'})
    )

    class Meta:
        model = StockTransferItem
        fields = ['product', 'quantity_requested']


class StockAdjustmentForm(forms.ModelForm):
    class Meta:
        model = StockAdjustment
        fields = ['batch', 'adjusted_quantity', 'reason', 'notes']
        widgets = {
            'notes': forms.Textarea(attrs={'rows': 2}),
            'batch': forms.Select(attrs={'class': 'select2'}),
        }

    def __init__(self, location, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # If location is None (Owner viewing all), maybe show all batches?
        # Ideally adjustments should be location-specific.
        if location:
            self.fields['batch'].queryset = StockBatch.objects.filter(location=location, quantity__gt=0)
            self.fields['batch'].label_from_instance = lambda \
                obj: f"{obj.product.name} | Batch: {obj.batch_number or 'N/A'} | Qty: {obj.quantity}"


