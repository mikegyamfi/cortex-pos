from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Count, Sum

from apps.core.decorators import role_required, OWNER, MANAGER
from .models import Location
from .forms import LocationForm


@login_required
@role_required(OWNER, MANAGER)
def location_list(request):
    """Warehouses & Shops overview with staff and stock counts."""
    locations = Location.objects.all().annotate(
        staff_count=Count('staff', distinct=True),
        stock_units=Sum('stock_batches__quantity'),
    ).order_by('name')
    return render(request, 'location/location_list.html', {'locations': locations})


@login_required
@role_required(OWNER)
def location_create(request):
    if request.method == 'POST':
        form = LocationForm(request.POST)
        if form.is_valid():
            loc = form.save()
            messages.success(request, f"Location '{loc.name}' created.")
            return redirect('location:list')
    else:
        form = LocationForm()
    return render(request, 'location/location_form.html', {'form': form, 'title': 'Add Location'})


@login_required
@role_required(OWNER)
def location_edit(request, pk):
    location = get_object_or_404(Location, pk=pk)
    if request.method == 'POST':
        form = LocationForm(request.POST, instance=location)
        if form.is_valid():
            form.save()
            messages.success(request, f"Location '{location.name}' updated.")
            return redirect('location:list')
    else:
        form = LocationForm(instance=location)
    return render(request, 'location/location_form.html', {'form': form, 'title': f'Edit {location.name}'})
