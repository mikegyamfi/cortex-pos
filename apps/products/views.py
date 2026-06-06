from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Q, Sum
from django.core.paginator import Paginator

from .models import Product, Category, Brand
from .forms import ProductForm, ProductImageForm, CategoryForm, BrandForm
from ..core.decorators import role_required, INVENTORY_STAFF, MANAGEMENT
from ..inventory.models import StockBatch
from ..location.models import Location


@login_required
@role_required(*INVENTORY_STAFF)
def product_list(request):
    """
    The Central Catalog View.
    Features comprehensive search and filtering.
    """
    # 1. Base Query
    products = Product.objects.all().select_related('location', 'category', 'brand', 'unit').prefetch_related('images')

    # Multi-tenant scoping: a shop only sees its own products. Owners see all.
    if request.user.role != 'OWNER':
        products = products.filter(location=request.user.assigned_location)

    # 2. Filtering
    query = request.GET.get('q', '')
    category_id = request.GET.get('category')
    brand_id = request.GET.get('brand')
    status = request.GET.get('status')
    location_id = request.GET.get('location')

    if query:
        products = products.filter(
            Q(name__icontains=query) |
            Q(sku__icontains=query) |
            Q(barcode__icontains=query)
        )

    if category_id:
        products = products.filter(category_id=category_id)

    if brand_id:
        products = products.filter(brand_id=brand_id)

    if status == 'active':
        products = products.filter(is_active=True)
    elif status == 'inactive':
        products = products.filter(is_active=False)

    # Owner-Only Filter: show products belonging to a specific shop
    if request.user.role == 'OWNER' and location_id:
        products = products.filter(location_id=location_id)

    # 3. Sorting
    products = products.order_by('-created_at')

    # 4. Pagination
    paginator = Paginator(products, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # 5. Context Data for Filter Dropdowns
    context = {
        'page_obj': page_obj,
        'query': query,
        'categories': Category.objects.filter(is_active=True),
        'brands': Brand.objects.filter(is_active=True),
        'locations': Location.objects.filter(is_active=True) if request.user.role == 'OWNER' else [],

        # Keep filter state in UI
        'selected_category': int(category_id) if category_id else None,
        'selected_brand': int(brand_id) if brand_id else None,
        'selected_status': status,
        'selected_location': int(location_id) if location_id else None,
    }

    return render(request, 'products/product_list.html', context)


@login_required
@role_required(*INVENTORY_STAFF)
def product_detail(request, pk):
    """
    Detailed view of a product including stock levels across all locations.
    """
    product = get_object_or_404(Product, pk=pk)

    # Multi-tenant scoping: only the owning shop (or an owner) may view it.
    if request.user.role != 'OWNER' and product.location_id != request.user.assigned_location_id:
        messages.error(request, "That product belongs to another shop.")
        return redirect('products:product_list')

    # Get Stock Summary per Location
    # This shows "Shop A: 50 units", "Warehouse: 100 units"
    stock_summary = StockBatch.objects.filter(
        product=product,
        quantity__gt=0
    ).values(
        'location__name', 'location__location_type'
    ).annotate(
        total_qty=Sum('quantity')
    ).order_by('location__name')

    # Calculate Global Total
    total_stock = sum(item['total_qty'] for item in stock_summary)

    # Get recent batches (raw data)
    recent_batches = StockBatch.objects.filter(product=product).order_by('-received_date')[:5]

    context = {
        'product': product,
        'stock_summary': stock_summary,
        'total_stock': total_stock,
        'recent_batches': recent_batches,
    }
    return render(request, 'products/product_detail.html', context)


@login_required
@role_required(*MANAGEMENT)
def product_create(request):
    """
    Create a new product definition.
    """
    is_owner = request.user.role == 'OWNER'
    if request.method == 'POST':
        form = ProductForm(request.POST, request.FILES)
        if not is_owner:
            form.fields.pop('location', None)  # managers can't reassign shops
            form.instance.location = request.user.assigned_location  # set before validation
        if form.is_valid():
            product = form.save()
            messages.success(request, f"Product '{product.name}' created successfully.")
            return redirect('products:product_list')
    else:
        form = ProductForm()
        if not is_owner:
            form.fields.pop('location', None)

    return render(request, 'products/product_form.html', {'form': form, 'category_form': CategoryForm(), 'title': 'Add New Product'})


@login_required
@role_required(*MANAGEMENT)
def product_edit(request, pk):
    """
    Update existing product details.
    """
    product = get_object_or_404(Product, pk=pk)
    is_owner = request.user.role == 'OWNER'

    # Managers may only edit their own shop's products.
    if not is_owner and product.location_id != request.user.assigned_location_id:
        messages.error(request, "That product belongs to another shop.")
        return redirect('products:product_list')

    if request.method == 'POST':
        form = ProductForm(request.POST, request.FILES, instance=product)
        if not is_owner:
            form.fields.pop('location', None)
            form.instance.location = request.user.assigned_location
        if form.is_valid():
            form.save()
            messages.success(request, f"Product '{product.name}' updated.")
            return redirect('products:product_list')
    else:
        form = ProductForm(instance=product)
        if not is_owner:
            form.fields.pop('location', None)

    return render(request, 'products/product_form.html', {'form': form, 'title': f'Edit {product.name}'})


@login_required
@role_required(*MANAGEMENT)
def quick_category_create(request):
    """
    HTMX or Modal view to add a category on the fly while creating a product.
    """
    if request.method == 'POST':
        form = CategoryForm(request.POST)
        if form.is_valid():
            category = form.save()
            # If HTMX, return a partial; if standard, redirect
            messages.success(request, f"Category '{category.name}' added.")
            return redirect('products:product_create')

    return render(request, 'products/partials/category_form.html', {'form': CategoryForm()})